use std::{
    collections::VecDeque,
    panic::{catch_unwind, AssertUnwindSafe},
    pin::Pin,
    ptr::NonNull,
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, Condvar, Mutex,
    },
    task::{Context, Poll, Waker},
    time::Duration,
};

use futures_core::Stream;
use minicronet_sys as sys;

use crate::{
    copy_bytes, copy_string, ffi_bytes, lock, result, timeout_ms, Engine, Error, Header, WAIT_SLICE,
};

const WS_EVENT_BUFFER_LIMIT: usize = 4 * 1024 * 1024;
const WS_EVENT_COUNT_LIMIT: usize = 1024;

#[derive(Clone, Debug)]
pub struct WebSocketConfig {
    pub url: String,
    pub origin: String,
    pub protocols: Vec<String>,
    pub headers: Vec<Header>,
    pub timeout: Option<Duration>,
}

impl WebSocketConfig {
    pub fn new(url: impl Into<String>, origin: impl Into<String>) -> Self {
        Self {
            url: url.into(),
            origin: origin.into(),
            protocols: Vec::new(),
            headers: Vec::new(),
            timeout: None,
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum MessageType {
    Text,
    Binary,
    Continuation,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct CloseInfo {
    pub was_clean: bool,
    pub code: u16,
    pub reason: String,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct WebSocketFailure {
    pub error: Error,
    pub net_error: i32,
    pub response_code: i32,
    pub message: String,
}

impl std::fmt::Display for WebSocketFailure {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            formatter,
            "{} (Chromium net error {}, response {})",
            self.error, self.net_error, self.response_code
        )
    }
}

impl std::error::Error for WebSocketFailure {}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum WebSocketEvent {
    Open {
        protocol: String,
        extensions: String,
    },
    Message {
        message_type: MessageType,
        final_fragment: bool,
        data: Vec<u8>,
    },
    Closing,
    Closed(CloseInfo),
    Failure(WebSocketFailure),
}

#[derive(Default)]
struct WebSocketState {
    events: VecDeque<WebSocketEvent>,
    event_bytes: usize,
    terminal: bool,
    waker: Option<Waker>,
}

struct WebSocketShared {
    state: Mutex<WebSocketState>,
    changed: Condvar,
    event_callback: Mutex<Option<Arc<dyn Fn() + Send + Sync>>>,
    callback_ref_live: AtomicBool,
}

/// Wakers taken under the state lock and woken after it is released.
///
/// `Waker::wake` runs arbitrary consumer code; invoking it with the state
/// mutex held deadlocks a consumer whose wake path re-enters that mutex.
struct Wakeups {
    event: Option<Waker>,
}

impl Wakeups {
    fn wake(self) {
        if let Some(waker) = self.event {
            waker.wake();
        }
    }
}

impl WebSocketShared {
    fn new() -> Self {
        Self {
            state: Mutex::new(WebSocketState::default()),
            changed: Condvar::new(),
            event_callback: Mutex::new(None),
            callback_ref_live: AtomicBool::new(true),
        }
    }

    fn push(&self, event: WebSocketEvent, terminal: bool) {
        let mut state = lock(&self.state);
        if state.terminal {
            return;
        }
        let incoming_size = event_size(&event);
        if state.events.len() >= WS_EVENT_COUNT_LIMIT
            || state.event_bytes.saturating_add(incoming_size) > WS_EVENT_BUFFER_LIMIT
        {
            // Discard the backlog, then account for the replacement event:
            // leaving event_bytes at 0 while one event is queued made the
            // counter disagree with the queue it describes.
            state.events.clear();
            let failure = WebSocketEvent::Failure(WebSocketFailure {
                error: Error::BufferLimit,
                net_error: 0,
                response_code: 0,
                message: "WebSocket event buffer limit exceeded".into(),
            });
            state.event_bytes = event_size(&failure);
            state.events.push_back(failure);
            state.terminal = true;
            let wakeups = Wakeups {
                event: state.waker.take(),
            };
            self.changed.notify_all();
            drop(state);
            wakeups.wake();
            self.schedule_event();
            return;
        }
        state.event_bytes = state.event_bytes.saturating_add(incoming_size);
        state.events.push_back(event);
        state.terminal |= terminal;
        let wakeups = Wakeups {
            event: state.waker.take(),
        };
        self.changed.notify_all();
        drop(state);
        wakeups.wake();
        self.schedule_event();
    }

    fn schedule_event(&self) {
        // The hook must run with no Rust lock held. Bindings acquire a host
        // runtime lock inside it (the Python GIL), while a consumer thread that
        // already holds that lock may call `clear_event_callback`. Cloning into
        // a binding and releasing the guard first keeps that ordering one-way.
        let callback = lock(&self.event_callback).clone();
        if let Some(callback) = callback {
            // This runs on a Core callback thread. A panic here would unwind
            // into the C ABI, and `extern "C"` cannot unwind: the runtime turns
            // that into "panic in a function that cannot unwind" and aborts the
            // host process. Record the panic instead, and deliberately do not
            // re-invoke the hook -- it is the thing that just failed.
            if catch_unwind(AssertUnwindSafe(|| callback())).is_err() {
                self.record_callback_panic();
                // Drop the broken hook so later callbacks stop re-running it.
                let broken = lock(&self.event_callback).take();
                drop(broken);
            }
        }
    }

    /// Marks the socket failed because a Rust callback panicked, returning
    /// whether this call is the one that recorded the failure.
    ///
    /// This never re-invokes the event hook: the caller is either the hook's
    /// own panic guard or a path whose hook call already failed.
    fn record_callback_panic(&self) -> bool {
        let mut state = lock(&self.state);
        if state.terminal {
            return false;
        }
        let failure = WebSocketEvent::Failure(WebSocketFailure {
            error: Error::CallbackPanic,
            net_error: 0,
            response_code: -1,
            message: "Rust callback panicked".into(),
        });
        // Keep `event_bytes` describing the queue it accounts for: this path
        // queues an event without going through `push`, so it has to add the
        // replacement event's size itself.
        state.event_bytes = state.event_bytes.saturating_add(event_size(&failure));
        state.events.push_back(failure);
        state.terminal = true;
        let wakeups = Wakeups {
            event: state.waker.take(),
        };
        self.changed.notify_all();
        drop(state);
        wakeups.wake();
        true
    }

    /// Records a panic raised outside the event hook, then wakes the consumer.
    /// The hook is still invoked here because it is not what failed.
    fn callback_panicked(&self) {
        if self.record_callback_panic() {
            self.schedule_event();
        }
    }
}

struct WebSocketInner {
    raw: NonNull<sys::mn_websocket_t>,
    shared: Arc<WebSocketShared>,
    user_data: *const WebSocketShared,
    started: AtomicBool,
}

unsafe impl Send for WebSocketInner {}
unsafe impl Sync for WebSocketInner {}

impl Drop for WebSocketInner {
    fn drop(&mut self) {
        unsafe { sys::mn_websocket_release(self.raw.as_ptr()) };
        if !self.started.load(Ordering::Acquire) {
            release_callback_ref(self.user_data);
        }
    }
}

#[derive(Clone)]
pub struct WebSocket(Arc<WebSocketInner>);

impl WebSocket {
    pub(crate) fn new(engine: &Engine, config: WebSocketConfig) -> Result<Self, Error> {
        let shared = Arc::new(WebSocketShared::new());
        let user_data = Arc::into_raw(Arc::clone(&shared));
        let protocols = config.protocols;
        let raw_protocols: Vec<_> = protocols
            .iter()
            .map(|protocol| sys::mn_string_t {
                data: protocol.as_ptr().cast(),
                length: protocol.len(),
            })
            .collect();
        let headers = config.headers;
        let raw_headers: Vec<_> = headers
            .iter()
            .map(|header| sys::mn_header_t {
                name: header.name.as_ptr().cast(),
                name_length: header.name.len(),
                value: header.value.as_ptr().cast(),
                value_length: header.value.len(),
            })
            .collect();
        let callbacks = sys::mn_websocket_callbacks_t {
            size: std::mem::size_of::<sys::mn_websocket_callbacks_t>() as u32,
            version: sys::MN_ABI_VERSION,
            user_data: user_data.cast_mut().cast(),
            on_open: Some(on_open),
            on_message: Some(on_message),
            on_closing: Some(on_closing),
            on_closed: Some(on_closed),
            on_failure: Some(on_failure),
        };
        let raw_config = sys::mn_websocket_config_t {
            size: std::mem::size_of::<sys::mn_websocket_config_t>() as u32,
            version: sys::MN_ABI_VERSION,
            url: config.url.as_ptr().cast(),
            url_length: config.url.len(),
            origin: config.origin.as_ptr().cast(),
            origin_length: config.origin.len(),
            protocols: if raw_protocols.is_empty() {
                std::ptr::null()
            } else {
                raw_protocols.as_ptr()
            },
            protocol_count: raw_protocols.len(),
            headers: if raw_headers.is_empty() {
                std::ptr::null()
            } else {
                raw_headers.as_ptr()
            },
            header_count: raw_headers.len(),
            callbacks,
            timeout_ms: timeout_ms(config.timeout),
        };
        let mut raw = std::ptr::null_mut();
        let result = unsafe { sys::mn_websocket_create(engine.raw(), &raw_config, &mut raw) };
        if result != sys::mn_result_t::MN_OK {
            release_callback_ref(user_data);
            return Err(result.into());
        }
        let Some(raw) = NonNull::new(raw) else {
            release_callback_ref(user_data);
            return Err(Error::InitializationFailed);
        };
        Ok(Self(Arc::new(WebSocketInner {
            raw,
            shared,
            user_data,
            started: AtomicBool::new(false),
        })))
    }

    pub fn start(&self) -> Result<WebSocketEvents, Error> {
        if self
            .0
            .started
            .compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
            .is_err()
        {
            return Err(Error::InvalidState);
        }
        let result = unsafe { sys::mn_websocket_start(self.0.raw.as_ptr()) };
        if result != sys::mn_result_t::MN_OK {
            self.0.started.store(false, Ordering::Release);
            return Err(result.into());
        }
        Ok(WebSocketEvents {
            shared: Arc::clone(&self.0.shared),
        })
    }

    /// Installs a lightweight event-loop notification hook.
    pub fn set_event_callback(&self, callback: Arc<dyn Fn() + Send + Sync>) {
        *lock(&self.0.shared.event_callback) = Some(callback);
    }

    pub fn clear_event_callback(&self) {
        let callback = lock(&self.0.shared.event_callback).take();
        drop(callback);
    }

    pub fn send_text(&self, text: &str) -> Result<(), Error> {
        self.send(
            sys::mn_websocket_message_type_t::MN_WEBSOCKET_MESSAGE_TEXT,
            text.as_bytes(),
        )
    }

    pub fn send_binary(&self, data: &[u8]) -> Result<(), Error> {
        self.send(
            sys::mn_websocket_message_type_t::MN_WEBSOCKET_MESSAGE_BINARY,
            data,
        )
    }

    pub fn close(&self, code: u16, reason: &str) -> Result<(), Error> {
        result(unsafe {
            sys::mn_websocket_close(
                self.0.raw.as_ptr(),
                code,
                reason.as_ptr().cast(),
                reason.len(),
            )
        })
    }

    pub fn cancel(&self) -> Result<(), Error> {
        result(unsafe { sys::mn_websocket_cancel(self.0.raw.as_ptr()) })
    }

    pub fn is_finished(&self) -> bool {
        lock(&self.0.shared.state).terminal
    }

    fn send(
        &self,
        message_type: sys::mn_websocket_message_type_t,
        data: &[u8],
    ) -> Result<(), Error> {
        result(unsafe {
            sys::mn_websocket_send(
                self.0.raw.as_ptr(),
                message_type,
                ffi_bytes(data),
                data.len(),
            )
        })
    }
}

pub struct WebSocketEvents {
    shared: Arc<WebSocketShared>,
}

impl WebSocketEvents {
    pub fn try_next(&mut self) -> Option<WebSocketEvent> {
        let mut state = lock(&self.shared.state);
        let event = state.events.pop_front();
        if let Some(ref event) = event {
            state.event_bytes = state.event_bytes.saturating_sub(event_size(event));
        }
        event
    }

    pub fn is_finished(&self) -> bool {
        lock(&self.shared.state).terminal
    }

    pub fn blocking_next(&mut self) -> Option<WebSocketEvent> {
        let mut state = lock(&self.shared.state);
        loop {
            if let Some(event) = state.events.pop_front() {
                state.event_bytes = state.event_bytes.saturating_sub(event_size(&event));
                return Some(event);
            }
            if state.terminal {
                return None;
            }
            let (next, _) = self
                .shared
                .changed
                .wait_timeout(state, WAIT_SLICE)
                .unwrap_or_else(|poisoned| poisoned.into_inner());
            state = next;
        }
    }
}

impl Stream for WebSocketEvents {
    type Item = WebSocketEvent;

    fn poll_next(self: Pin<&mut Self>, context: &mut Context<'_>) -> Poll<Option<Self::Item>> {
        let mut state = lock(&self.shared.state);
        if let Some(event) = state.events.pop_front() {
            state.event_bytes = state.event_bytes.saturating_sub(event_size(&event));
            return Poll::Ready(Some(event));
        }
        if state.terminal {
            return Poll::Ready(None);
        }
        state.waker = Some(context.waker().clone());
        Poll::Pending
    }
}

fn event_size(event: &WebSocketEvent) -> usize {
    match event {
        WebSocketEvent::Message { data, .. } => data.len(),
        WebSocketEvent::Open {
            protocol,
            extensions,
        } => protocol.len().saturating_add(extensions.len()),
        WebSocketEvent::Closed(CloseInfo { reason, .. }) => reason.len(),
        WebSocketEvent::Failure(WebSocketFailure { message, .. }) => message.len(),
        WebSocketEvent::Closing => 0,
    }
}

unsafe fn shared<'a>(user_data: *mut std::ffi::c_void) -> &'a WebSocketShared {
    unsafe { &*user_data.cast::<WebSocketShared>() }
}

fn release_callback_ref(user_data: *const WebSocketShared) {
    let shared = unsafe { &*user_data };
    if shared.callback_ref_live.swap(false, Ordering::AcqRel) {
        unsafe { Arc::decrement_strong_count(user_data) };
    }
}

unsafe extern "C" fn on_open(
    user_data: *mut std::ffi::c_void,
    _websocket: *mut sys::mn_websocket_t,
    protocol: *const i8,
    protocol_length: usize,
    extensions: *const i8,
    extensions_length: usize,
) {
    let shared = unsafe { shared(user_data) };
    if catch_unwind(AssertUnwindSafe(|| {
        shared.push(
            WebSocketEvent::Open {
                protocol: copy_string(protocol, protocol_length),
                extensions: copy_string(extensions, extensions_length),
            },
            false,
        );
    }))
    .is_err()
    {
        shared.callback_panicked();
    }
}

unsafe extern "C" fn on_message(
    user_data: *mut std::ffi::c_void,
    _websocket: *mut sys::mn_websocket_t,
    message_type: sys::mn_websocket_message_type_t,
    final_fragment: i32,
    data: *const u8,
    data_length: usize,
) {
    let shared = unsafe { shared(user_data) };
    if catch_unwind(AssertUnwindSafe(|| {
        let message_type = match message_type {
            sys::mn_websocket_message_type_t::MN_WEBSOCKET_MESSAGE_TEXT => MessageType::Text,
            sys::mn_websocket_message_type_t::MN_WEBSOCKET_MESSAGE_BINARY => MessageType::Binary,
            _ => MessageType::Continuation,
        };
        shared.push(
            WebSocketEvent::Message {
                message_type,
                final_fragment: final_fragment != 0,
                data: copy_bytes(data, data_length),
            },
            false,
        );
    }))
    .is_err()
    {
        shared.callback_panicked();
    }
}

unsafe extern "C" fn on_closing(
    user_data: *mut std::ffi::c_void,
    _websocket: *mut sys::mn_websocket_t,
) {
    let shared = unsafe { shared(user_data) };
    if catch_unwind(AssertUnwindSafe(|| {
        shared.push(WebSocketEvent::Closing, false);
    }))
    .is_err()
    {
        shared.callback_panicked();
    }
}

unsafe extern "C" fn on_closed(
    user_data: *mut std::ffi::c_void,
    _websocket: *mut sys::mn_websocket_t,
    was_clean: i32,
    code: u16,
    reason: *const i8,
    reason_length: usize,
) {
    let shared = unsafe { shared(user_data) };
    if catch_unwind(AssertUnwindSafe(|| {
        shared.push(
            WebSocketEvent::Closed(CloseInfo {
                was_clean: was_clean != 0,
                code,
                reason: copy_string(reason, reason_length),
            }),
            true,
        );
    }))
    .is_err()
    {
        shared.callback_panicked();
    }
    release_callback_ref(user_data.cast());
}

unsafe extern "C" fn on_failure(
    user_data: *mut std::ffi::c_void,
    _websocket: *mut sys::mn_websocket_t,
    result: sys::mn_result_t,
    net_error: i32,
    response_code: i32,
    message: *const i8,
    message_length: usize,
) {
    let shared = unsafe { shared(user_data) };
    if catch_unwind(AssertUnwindSafe(|| {
        shared.push(
            WebSocketEvent::Failure(WebSocketFailure {
                error: result.into(),
                net_error,
                response_code,
                message: copy_string(message, message_length),
            }),
            true,
        );
    }))
    .is_err()
    {
        shared.callback_panicked();
    }
    release_callback_ref(user_data.cast());
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn terminal_event_is_delivered_once() {
        let shared = Arc::new(WebSocketShared::new());
        let user_data = Arc::into_raw(Arc::clone(&shared)).cast_mut().cast();
        unsafe {
            on_open(
                user_data,
                std::ptr::null_mut(),
                std::ptr::null(),
                0,
                std::ptr::null(),
                0,
            );
            on_closed(
                user_data,
                std::ptr::null_mut(),
                1,
                1000,
                b"done".as_ptr().cast(),
                4,
            );
        }
        let mut events = WebSocketEvents { shared };
        assert!(matches!(
            events.blocking_next(),
            Some(WebSocketEvent::Open { .. })
        ));
        assert!(matches!(
            events.blocking_next(),
            Some(WebSocketEvent::Closed(_))
        ));
        assert!(events.blocking_next().is_none());
    }

    #[test]
    fn event_hook_runs_without_the_callback_lock_held() {
        let shared = Arc::new(WebSocketShared::new());
        let observed = Arc::new(AtomicBool::new(false));
        let hook_shared = Arc::downgrade(&shared);
        let hook_observed = Arc::clone(&observed);
        *lock(&shared.event_callback) = Some(Arc::new(move || {
            let Some(shared) = hook_shared.upgrade() else {
                return;
            };
            // A binding hook acquires the host runtime lock here, so the caller
            // must no longer hold `event_callback`.
            let free = shared.event_callback.try_lock().is_ok();
            hook_observed.store(free, Ordering::Release);
        }));

        let user_data = Arc::into_raw(Arc::clone(&shared)).cast_mut().cast();
        unsafe {
            on_closing(user_data, std::ptr::null_mut());
        }
        release_callback_ref(user_data.cast());

        assert!(
            observed.load(Ordering::Acquire),
            "schedule_event held event_callback while invoking the hook"
        );
    }
}

#[cfg(test)]
mod abi_regressions {
    use super::*;
    use std::sync::atomic::AtomicUsize;
    use std::task::{RawWaker, RawWakerVTable, Waker};
    use std::time::Duration;

    fn reentrant_waker(shared: Arc<WebSocketShared>, hits: Arc<AtomicUsize>) -> Waker {
        unsafe fn clone(data: *const ()) -> RawWaker {
            let inner = unsafe { &*(data as *const (Arc<WebSocketShared>, Arc<AtomicUsize>)) };
            let boxed = Box::new((Arc::clone(&inner.0), Arc::clone(&inner.1)));
            RawWaker::new(Box::into_raw(boxed) as *const (), &VTABLE)
        }
        unsafe fn wake(data: *const ()) {
            let boxed =
                unsafe { Box::from_raw(data as *mut (Arc<WebSocketShared>, Arc<AtomicUsize>)) };
            boxed.1.fetch_add(1, Ordering::SeqCst);
            let _guard = lock(&boxed.0.state);
        }
        unsafe fn wake_by_ref(data: *const ()) {
            let inner = unsafe { &*(data as *const (Arc<WebSocketShared>, Arc<AtomicUsize>)) };
            inner.1.fetch_add(1, Ordering::SeqCst);
            let _guard = lock(&inner.0.state);
        }
        unsafe fn drop_fn(data: *const ()) {
            drop(unsafe { Box::from_raw(data as *mut (Arc<WebSocketShared>, Arc<AtomicUsize>)) });
        }
        static VTABLE: RawWakerVTable = RawWakerVTable::new(clone, wake, wake_by_ref, drop_fn);
        let boxed = Box::new((shared, hits));
        unsafe { Waker::from_raw(RawWaker::new(Box::into_raw(boxed) as *const (), &VTABLE)) }
    }

    #[test]
    fn push_does_not_wake_the_waker_under_the_state_lock() {
        let shared = Arc::new(WebSocketShared::new());
        let hits = Arc::new(AtomicUsize::new(0));
        {
            let mut state = lock(&shared.state);
            state.waker = Some(reentrant_waker(Arc::clone(&shared), Arc::clone(&hits)));
        }

        let (tx, rx) = std::sync::mpsc::channel();
        let worker = Arc::clone(&shared);
        std::thread::spawn(move || {
            worker.push(WebSocketEvent::Closing, false);
            let _ = tx.send(());
        });

        rx.recv_timeout(Duration::from_secs(5))
            .expect("push() woke the waker while the state lock was held");
        assert_eq!(hits.load(Ordering::SeqCst), 1, "the waker ran exactly once");
    }

    #[test]
    fn callback_panic_failure_event_is_accounted_for() {
        // record_callback_panic queues its Failure event without going through
        // push(), so it has to account for the bytes itself. It did not, and
        // event_bytes stayed at 0 while two events were queued -- the counter
        // then described a queue it no longer matched, which is the exact
        // invariant overflow_failure_event_is_accounted_for protects.
        let shared = Arc::new(WebSocketShared::new());
        *lock(&shared.event_callback) = Some(Arc::new(|| panic!("hook exploded")));

        shared.push(WebSocketEvent::Closing, false);

        let state = lock(&shared.state);
        let queued: usize = state.events.iter().map(event_size).sum();
        assert_eq!(
            state.event_bytes, queued,
            "event_bytes must describe the queue that remains"
        );
        assert!(state.terminal, "a callback panic is terminal");
    }

    #[test]
    fn overflow_failure_event_is_accounted_for() {
        // The overflow branch replaces the backlog with one Failure event. Its
        // bytes must land in event_bytes, or the counter describes a queue it
        // no longer matches.
        let shared = Arc::new(WebSocketShared::new());
        for _ in 0..WS_EVENT_COUNT_LIMIT {
            shared.push(
                WebSocketEvent::Message {
                    message_type: MessageType::Binary,
                    final_fragment: true,
                    data: vec![0u8; 64],
                },
                false,
            );
        }
        shared.push(WebSocketEvent::Closing, false);

        let state = lock(&shared.state);
        assert_eq!(state.events.len(), 1, "only the failure event survives");
        assert!(state.terminal, "overflow is terminal");
        let queued = state.events.front().expect("the failure event");
        assert_eq!(
            state.event_bytes,
            event_size(queued),
            "event_bytes must describe the queue that remains"
        );
        assert!(state.event_bytes > 0, "a queued event cannot weigh nothing");
    }

    #[test]
    fn a_panicking_event_hook_does_not_escape_the_callback() {
        // push() calls schedule_event on a Core callback thread. An unguarded
        // panic there unwinds into `extern "C"`, which cannot unwind, so the
        // runtime aborts the host process.
        let shared = Arc::new(WebSocketShared::new());
        let panics = Arc::new(AtomicUsize::new(0));
        let counter = Arc::clone(&panics);
        *lock(&shared.event_callback) = Some(Arc::new(move || {
            counter.fetch_add(1, Ordering::SeqCst);
            panic!("hook exploded");
        }));

        // Must return rather than abort.
        shared.push(WebSocketEvent::Closing, false);

        assert_eq!(panics.load(Ordering::SeqCst), 1, "the hook ran once");
        assert!(
            lock(&shared.event_callback).is_none(),
            "the broken hook is dropped so it cannot re-run on every callback"
        );
        let state = lock(&shared.state);
        assert!(state.terminal);
        match state.events.back() {
            Some(WebSocketEvent::Failure(failure)) => {
                assert_eq!(failure.error, Error::CallbackPanic)
            }
            other => panic!("expected a CallbackPanic failure, got {other:?}"),
        }
    }
}
