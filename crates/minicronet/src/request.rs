use std::{
    collections::VecDeque,
    future::Future,
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
    copy_bytes, copy_string, ffi_bytes, lock, result, timeout_ms, Engine, Error, WAIT_SLICE,
};

// Backpressure ceiling per request. Reaching it answers on_body with
// MN_READ_PAUSE, so the Core stops issuing reads instead of holding a callback
// thread; consumers resume it after taking a chunk.
const BODY_BUFFER_LIMIT: usize = 1024 * 1024;

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Header {
    pub name: String,
    pub value: String,
}

impl Header {
    pub fn new(name: impl Into<String>, value: impl Into<String>) -> Self {
        Self {
            name: name.into(),
            value: value.into(),
        }
    }
}

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub enum Upload {
    #[default]
    None,
    Fixed(Vec<u8>),
    Chunked,
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub enum CacheMode {
    #[default]
    Default,
    Validate,
    Bypass,
    NoStore,
    Force,
    OnlyIfCached,
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub enum RedirectMode {
    #[default]
    Follow,
    Manual,
    Error,
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub enum RequestPriority {
    #[default]
    Default,
    Highest,
    Medium,
    Low,
    Lowest,
    Idle,
}

#[derive(Clone, Debug)]
pub struct RequestConfig {
    pub url: String,
    pub method: String,
    pub headers: Vec<Header>,
    pub timeout: Option<Duration>,
    pub upload: Upload,
    pub cache: CacheMode,
    pub redirect: RedirectMode,
    pub priority: RequestPriority,
}

impl RequestConfig {
    pub fn get(url: impl Into<String>) -> Self {
        Self {
            url: url.into(),
            method: "GET".into(),
            headers: Vec::new(),
            timeout: None,
            upload: Upload::None,
            cache: CacheMode::Default,
            redirect: RedirectMode::Follow,
            priority: RequestPriority::Default,
        }
    }
}

/// One response header field, owned.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ResponseHeader {
    pub name: String,
    pub value: String,
}

/// Everything the response callback delivered about the head.
#[derive(Clone, Debug)]
pub struct ResponseHead {
    pub status_code: i32,
    /// The reason phrase as the Core extracted it; empty when the peer sent none.
    pub reason_phrase: String,
    /// Parsed fields, duplicates and arrival order preserved.
    pub header_fields: Vec<ResponseHeader>,
    /// The raw block, for callers that reproduce bytes rather than fields.
    pub raw_headers: Vec<u8>,
}

/// Copies a Core-owned UTF-8 string that is valid for the current call only.
fn copy_utf8(data: *const i8, length: usize) -> String {
    if data.is_null() || length == 0 {
        return String::new();
    }
    let bytes = unsafe { std::slice::from_raw_parts(data.cast::<u8>(), length) };
    String::from_utf8_lossy(bytes).into_owned()
}

/// Copies the Core's header array, whose strings live only for the call.
fn copy_header_fields(data: *const sys::mn_header_t, count: usize) -> Vec<ResponseHeader> {
    if data.is_null() || count == 0 {
        return Vec::new();
    }
    let raw = unsafe { std::slice::from_raw_parts(data, count) };
    raw.iter()
        .map(|field| ResponseHeader {
            name: copy_utf8(field.name, field.name_length),
            value: copy_utf8(field.value, field.value_length),
        })
        .collect()
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct RequestError {
    pub error: Error,
    pub net_error: i32,
}

impl std::fmt::Display for RequestError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            formatter,
            "{} (Chromium net error {})",
            self.error, self.net_error
        )
    }
}

impl std::error::Error for RequestError {}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Redirect {
    pub status_code: i32,
    pub headers: Vec<u8>,
    pub new_url: String,
    pub new_method: String,
}

/// The protocol a response arrived over.
///
/// Mirrors `mn_http_version_t`. `Unknown` is produced by Chromium itself, so it
/// is reported rather than guessed away.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum HttpVersion {
    Unknown,
    Http09,
    Http10,
    Http11,
    Http2,
    Http3,
}

impl From<sys::mn_http_version_t> for HttpVersion {
    fn from(value: sys::mn_http_version_t) -> Self {
        match value.0 {
            1 => Self::Http09,
            2 => Self::Http10,
            3 => Self::Http11,
            4 => Self::Http2,
            5 => Self::Http3,
            _ => Self::Unknown,
        }
    }
}

pub struct Response {
    pub status_code: i32,
    pub headers: Vec<u8>,
    /// The reason phrase the peer sent, empty when it sent none.
    pub reason_phrase: String,
    /// Parsed header fields, duplicates and arrival order preserved.
    pub header_fields: Vec<ResponseHeader>,
    /// The protocol the connection actually used.
    ///
    /// `Unknown` is a real answer, not a placeholder: Chromium reports
    /// `kUNKNOWN` when it genuinely does not know (a response served from the
    /// HTTP cache), and reporting HTTP/1.1 there would be a lie.
    pub http_version: HttpVersion,
    pub body: ResponseStream,
}

#[derive(Default)]
struct RequestState {
    response: Option<ResponseHead>,
    response_taken: bool,
    body: VecDeque<Vec<u8>>,
    body_bytes: usize,
    // Set when on_body answered MN_READ_PAUSE. The consumer clears it and calls
    // `Request::resume_read` once the queue drops back under the ceiling.
    read_paused: bool,
    // Set when the body stream was dropped. Nothing will drain the queue again,
    // so on_body stops queueing and stops pausing; the Core reads to EOF and
    // completes instead of stalling on a pause no consumer can lift.
    body_abandoned: bool,
    redirects: VecDeque<Redirect>,
    terminal: Option<Result<i32, RequestError>>,
    terminal_error_taken: bool,
    response_waker: Option<Waker>,
    body_waker: Option<Waker>,
}

struct RequestShared {
    state: Mutex<RequestState>,
    changed: Condvar,
    event_callback: Mutex<Option<Arc<dyn Fn() + Send + Sync>>>,
    callback_ref_live: AtomicBool,
}

/// Wakers taken under the state lock and woken after it is released.
struct Wakeups {
    response: Option<Waker>,
    body: Option<Waker>,
}

impl Wakeups {
    fn wake(self) {
        if let Some(waker) = self.response {
            waker.wake();
        }
        if let Some(waker) = self.body {
            waker.wake();
        }
    }
}

impl RequestShared {
    fn new() -> Self {
        Self {
            state: Mutex::new(RequestState::default()),
            changed: Condvar::new(),
            event_callback: Mutex::new(None),
            callback_ref_live: AtomicBool::new(true),
        }
    }

    /// Takes the pending wakers and wakes the condvar waiters.
    ///
    /// The returned wakers MUST be woken only after the caller drops the state
    /// guard. `Waker::wake` runs arbitrary consumer code; invoking it with
    /// `state` held deadlocks any consumer whose wake path re-enters this
    /// mutex, which is exactly what an inline poll or a state-reading hook does.
    fn notify(&self, state: &mut RequestState) -> Wakeups {
        let wakeups = Wakeups {
            response: state.response_waker.take(),
            body: state.body_waker.take(),
        };
        self.changed.notify_all();
        wakeups
    }

    /// Marks the body queue abandoned so Core reads keep flowing to completion.
    ///
    /// Without this, dropping a stream while `on_body` had answered
    /// MN_READ_PAUSE stranded the request forever: the Core waits for a
    /// `resume_read` that only a live consumer would send, so the request never
    /// reaches its terminal callback. Measured against an 8 MiB body: the
    /// request was still unfinished 6 s after the stream was dropped.
    ///
    /// Returns whether the caller must issue `resume_read`, which it must do
    /// after releasing the state lock.
    fn abandon_body(&self) -> bool {
        let mut state = lock(&self.state);
        state.body_abandoned = true;
        // Free what the abandoned consumer will never take.
        state.body.clear();
        state.body_bytes = 0;
        let resume = clear_pause(&mut state);
        self.changed.notify_all();
        resume
    }

    fn schedule_event(&self) {
        // The callback only schedules work on the consumer's event loop. It
        // must not perform user work or wait for network progress here.
        //
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

    /// Marks the request failed because a Rust callback panicked, returning
    /// whether this call is the one that recorded the failure.
    ///
    /// This never re-invokes the event hook: the caller is either the hook's
    /// own panic guard or a path whose hook call already failed.
    fn record_callback_panic(&self) -> bool {
        let mut state = lock(&self.state);
        if state.terminal.is_some() {
            return false;
        }
        state.terminal = Some(Err(RequestError {
            error: Error::CallbackPanic,
            net_error: 0,
        }));
        let wakeups = self.notify(&mut state);
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

struct RequestInner {
    raw: NonNull<sys::mn_request_t>,
    shared: Arc<RequestShared>,
    user_data: *const RequestShared,
    started: AtomicBool,
}

unsafe impl Send for RequestInner {}
unsafe impl Sync for RequestInner {}

impl Drop for RequestInner {
    fn drop(&mut self) {
        unsafe { sys::mn_request_release(self.raw.as_ptr()) };
        if !self.started.load(Ordering::Acquire) {
            release_callback_ref(self.user_data);
        }
    }
}

#[derive(Clone)]
pub struct Request(Arc<RequestInner>);

impl Request {
    /// The Core's handle, for the ABI getters that take one.
    pub(crate) fn raw(&self) -> *mut sys::mn_request_t {
        self.0.raw.as_ptr()
    }

    /// The protocol the connection used, or `None` when the Core does not know.
    ///
    /// Readable from the response onwards; before that the Core answers
    /// `MN_ERROR_INVALID_STATE` and this returns `None`.
    /// The response head the callback delivered, or `None` before it arrives.
    ///
    /// Readable after completion as well: the state keeps it until the response
    /// is taken, which is why this clones rather than moving.
    pub fn response_head(&self) -> Option<ResponseHead> {
        lock(&self.0.shared.state).response.clone()
    }

    pub fn negotiated_protocol(&self) -> Option<HttpVersion> {
        let mut value = sys::mn_http_version_t::MN_HTTP_VERSION_UNKNOWN;
        let result = unsafe { sys::mn_request_negotiated_protocol(self.raw(), &mut value) };
        (result == sys::mn_result_t::MN_OK).then(|| HttpVersion::from(value))
    }

    pub(crate) fn new(engine: &Engine, config: RequestConfig) -> Result<Self, Error> {
        let shared = Arc::new(RequestShared::new());
        let user_data = Arc::into_raw(Arc::clone(&shared));
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
        let (upload_mode, body) = match &config.upload {
            Upload::None => (sys::mn_upload_mode_t::MN_UPLOAD_NONE, &[][..]),
            Upload::Fixed(body) => (sys::mn_upload_mode_t::MN_UPLOAD_FIXED, body.as_slice()),
            Upload::Chunked => (sys::mn_upload_mode_t::MN_UPLOAD_CHUNKED, &[][..]),
        };
        let callbacks = sys::mn_request_callbacks_t {
            size: std::mem::size_of::<sys::mn_request_callbacks_t>() as u32,
            version: sys::MN_ABI_VERSION,
            user_data: user_data.cast_mut().cast(),
            on_response: Some(on_response),
            on_body: Some(on_body),
            on_complete: Some(on_complete),
            on_redirect: Some(on_redirect),
        };
        let raw_config = sys::mn_request_config_t {
            size: std::mem::size_of::<sys::mn_request_config_t>() as u32,
            version: sys::MN_ABI_VERSION,
            url: config.url.as_ptr().cast(),
            url_length: config.url.len(),
            method: config.method.as_ptr().cast(),
            method_length: config.method.len(),
            headers: if raw_headers.is_empty() {
                std::ptr::null()
            } else {
                raw_headers.as_ptr()
            },
            header_count: raw_headers.len(),
            timeout_ms: timeout_ms(config.timeout),
            callbacks,
            body: ffi_bytes(body),
            body_length: body.len(),
            upload_mode,
            cache_mode: cache_mode(config.cache),
            redirect_mode: redirect_mode(config.redirect),
            priority: priority(config.priority),
        };
        let mut raw = std::ptr::null_mut();
        let result = unsafe { sys::mn_request_create(engine.raw(), &raw_config, &mut raw) };
        if result != sys::mn_result_t::MN_OK {
            release_callback_ref(user_data);
            return Err(result.into());
        }
        let Some(raw) = NonNull::new(raw) else {
            release_callback_ref(user_data);
            return Err(Error::InitializationFailed);
        };
        Ok(Self(Arc::new(RequestInner {
            raw,
            shared,
            user_data,
            started: AtomicBool::new(false),
        })))
    }

    pub fn start(&self) -> Result<ResponseFuture, Error> {
        if self
            .0
            .started
            .compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
            .is_err()
        {
            return Err(Error::InvalidState);
        }
        let result = unsafe { sys::mn_request_start(self.0.raw.as_ptr()) };
        if result != sys::mn_result_t::MN_OK {
            self.0.started.store(false, Ordering::Release);
            return Err(result.into());
        }
        Ok(ResponseFuture {
            shared: Arc::clone(&self.0.shared),
            request: self.clone(),
        })
    }

    /// Resumes Core body reads after the queue ceiling paused them. Streams call
    /// this automatically; it is idempotent and safe after completion.
    pub fn resume_read(&self) -> Result<(), Error> {
        result(unsafe { sys::mn_request_resume_read(self.0.raw.as_ptr()) })
    }

    /// Installs a lightweight notification hook for event-driven bindings.
    /// The hook is invoked from Core callback threads and must return quickly.
    pub fn set_event_callback(&self, callback: Arc<dyn Fn() + Send + Sync>) {
        *lock(&self.0.shared.event_callback) = Some(callback);
    }

    /// Removes the binding notification hook and releases captured loop
    /// objects after completion or cancellation.
    pub fn clear_event_callback(&self) {
        let callback = lock(&self.0.shared.event_callback).take();
        drop(callback);
    }

    pub fn cancel(&self) -> Result<(), Error> {
        result(unsafe { sys::mn_request_cancel(self.0.raw.as_ptr()) })
    }

    pub fn upload_write(&self, data: &[u8], final_chunk: bool) -> Result<(), Error> {
        result(unsafe {
            sys::mn_request_upload_write(
                self.0.raw.as_ptr(),
                ffi_bytes(data),
                data.len(),
                i32::from(final_chunk),
            )
        })
    }

    pub fn upload_finish(&self) -> Result<(), Error> {
        self.upload_write(&[], true)
    }

    pub fn follow_redirect(&self) -> Result<(), Error> {
        result(unsafe { sys::mn_request_follow_redirect(self.0.raw.as_ptr()) })
    }

    pub fn take_redirect(&self) -> Option<Redirect> {
        lock(&self.0.shared.state).redirects.pop_front()
    }

    pub fn wait_for_redirect(&self, timeout: Duration) -> Result<Option<Redirect>, RequestError> {
        let mut state = lock(&self.0.shared.state);
        if let Some(redirect) = state.redirects.pop_front() {
            return Ok(Some(redirect));
        }
        let (state_after_wait, wait_result) = self
            .0
            .shared
            .changed
            .wait_timeout(state, timeout)
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        state = state_after_wait;
        if let Some(redirect) = state.redirects.pop_front() {
            return Ok(Some(redirect));
        }
        if wait_result.timed_out() {
            Ok(None)
        } else if let Some(Err(error)) = state.terminal {
            // The whole RequestError, not its `.error` category: the category
            // alone is just "Proxy", and dropping net_error is what made a
            // manual-redirect failure print `Proxy` with no Chromium code,
            // while the follow-redirect path named the error properly.
            Err(error)
        } else {
            Ok(None)
        }
    }

    pub fn is_finished(&self) -> bool {
        lock(&self.0.shared.state).terminal.is_some()
    }
}

pub struct ResponseFuture {
    shared: Arc<RequestShared>,
    request: Request,
}

impl ResponseFuture {
    /// Takes the response without blocking. `None` means Core has not produced
    /// a response yet; the installed request event callback will wake callers.
    pub fn try_take(&mut self) -> Option<Result<Response, RequestError>> {
        let mut state = lock(&self.shared.state);
        take_response(&self.shared, &self.request, &mut state)
    }

    pub fn wait(self) -> Result<Response, RequestError> {
        let mut state = lock(&self.shared.state);
        loop {
            if let Some(response) = take_response(&self.shared, &self.request, &mut state) {
                return response;
            }
            // take_response returns None once response_taken is set, so a
            // second pass cannot deliver anything: leaving the loop here is
            // what keeps this from spinning forever if the terminal callback
            // somehow set the flag without a payload.
            if state.response_taken {
                return Err(RequestError {
                    error: Error::Network,
                    net_error: 0,
                });
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

impl Future for ResponseFuture {
    type Output = Result<Response, RequestError>;

    fn poll(self: Pin<&mut Self>, context: &mut Context<'_>) -> Poll<Self::Output> {
        let mut state = lock(&self.shared.state);
        if let Some(response) = take_response(&self.shared, &self.request, &mut state) {
            return Poll::Ready(response);
        }
        state.response_waker = Some(context.waker().clone());
        Poll::Pending
    }
}

pub struct ResponseStream {
    shared: Arc<RequestShared>,
    request: Request,
}

impl std::fmt::Debug for ResponseStream {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("ResponseStream")
            .finish_non_exhaustive()
    }
}

impl Drop for ResponseStream {
    fn drop(&mut self) {
        if self.shared.abandon_body() {
            // The Core treats a resume that arrives before the pause lands, or
            // after completion, as a no-op.
            let _ = self.request.resume_read();
        }
    }
}

impl ResponseStream {
    /// Takes one body event without blocking. `None` means no event is ready.
    pub fn try_next(&mut self) -> Option<Option<Result<Vec<u8>, RequestError>>> {
        let mut state = lock(&self.shared.state);
        let item = take_body(&mut state)?;
        let resume = clear_pause(&mut state);
        self.shared.changed.notify_all();
        drop(state);
        self.resume_if(resume);
        Some(item)
    }

    pub fn blocking_next(&mut self) -> Option<Result<Vec<u8>, RequestError>> {
        let mut state = lock(&self.shared.state);
        loop {
            if let Some(item) = take_body(&mut state) {
                let resume = clear_pause(&mut state);
                self.shared.changed.notify_all();
                drop(state);
                self.resume_if(resume);
                return item;
            }
            let (next, _) = self
                .shared
                .changed
                .wait_timeout(state, WAIT_SLICE)
                .unwrap_or_else(|poisoned| poisoned.into_inner());
            state = next;
        }
    }

    /// Resumes Core reads outside the state lock. The Core treats a resume that
    /// arrives before the pause lands, or after completion, as a no-op.
    fn resume_if(&self, resume: bool) {
        if resume {
            let _ = self.request.resume_read();
        }
    }
}

impl Stream for ResponseStream {
    type Item = Result<Vec<u8>, RequestError>;

    fn poll_next(self: Pin<&mut Self>, context: &mut Context<'_>) -> Poll<Option<Self::Item>> {
        let mut state = lock(&self.shared.state);
        if let Some(item) = take_body(&mut state) {
            let resume = clear_pause(&mut state);
            self.shared.changed.notify_all();
            drop(state);
            self.resume_if(resume);
            return Poll::Ready(item);
        }
        state.body_waker = Some(context.waker().clone());
        Poll::Pending
    }
}

fn take_response(
    shared: &Arc<RequestShared>,
    request: &Request,
    state: &mut RequestState,
) -> Option<Result<Response, RequestError>> {
    if !state.response_taken {
        if let Some(head) = state.response.take() {
            state.response_taken = true;
            // Read before the response is handed over: the Core keeps it
            // readable after completion, but this is where the request still
            // carries the state that proves it.
            let mut http_version = sys::mn_http_version_t::MN_HTTP_VERSION_UNKNOWN;
            let negotiated =
                unsafe { sys::mn_request_negotiated_protocol(request.raw(), &mut http_version) };
            let http_version = if negotiated == sys::mn_result_t::MN_OK {
                HttpVersion::from(http_version)
            } else {
                HttpVersion::Unknown
            };
            return Some(Ok(Response {
                status_code: head.status_code,
                headers: head.raw_headers,
                header_fields: head.header_fields,
                reason_phrase: head.reason_phrase,
                http_version,
                body: ResponseStream {
                    shared: Arc::clone(shared),
                    request: request.clone(),
                },
            }));
        }
        if let Some(Err(error)) = state.terminal {
            state.response_taken = true;
            return Some(Err(error));
        }
    }
    None
}

/// Clears a recorded pause once the queue is back under the ceiling. The caller
/// must release the state lock before resuming Core reads.
fn clear_pause(state: &mut RequestState) -> bool {
    if state.read_paused && state.body_bytes < BODY_BUFFER_LIMIT {
        state.read_paused = false;
        true
    } else {
        false
    }
}

fn take_body(state: &mut RequestState) -> Option<Option<Result<Vec<u8>, RequestError>>> {
    if let Some(body) = state.body.pop_front() {
        state.body_bytes = state.body_bytes.saturating_sub(body.len());
        return Some(Some(Ok(body)));
    }
    match state.terminal {
        Some(Ok(_)) => Some(None),
        Some(Err(error)) if !state.terminal_error_taken => {
            state.terminal_error_taken = true;
            Some(Some(Err(error)))
        }
        Some(Err(_)) => Some(None),
        None => None,
    }
}

fn cache_mode(value: CacheMode) -> sys::mn_cache_mode_t {
    match value {
        CacheMode::Default => sys::mn_cache_mode_t::MN_CACHE_DEFAULT,
        CacheMode::Validate => sys::mn_cache_mode_t::MN_CACHE_VALIDATE,
        CacheMode::Bypass => sys::mn_cache_mode_t::MN_CACHE_BYPASS,
        CacheMode::NoStore => sys::mn_cache_mode_t::MN_CACHE_NO_STORE,
        CacheMode::Force => sys::mn_cache_mode_t::MN_CACHE_FORCE,
        CacheMode::OnlyIfCached => sys::mn_cache_mode_t::MN_CACHE_ONLY_IF_CACHED,
    }
}

fn redirect_mode(value: RedirectMode) -> sys::mn_redirect_mode_t {
    match value {
        RedirectMode::Follow => sys::mn_redirect_mode_t::MN_REDIRECT_FOLLOW,
        RedirectMode::Manual => sys::mn_redirect_mode_t::MN_REDIRECT_MANUAL,
        RedirectMode::Error => sys::mn_redirect_mode_t::MN_REDIRECT_ERROR,
    }
}

fn priority(value: RequestPriority) -> sys::mn_request_priority_t {
    match value {
        RequestPriority::Default => sys::mn_request_priority_t::MN_REQUEST_PRIORITY_DEFAULT,
        RequestPriority::Highest => sys::mn_request_priority_t::MN_REQUEST_PRIORITY_HIGHEST,
        RequestPriority::Medium => sys::mn_request_priority_t::MN_REQUEST_PRIORITY_MEDIUM,
        RequestPriority::Low => sys::mn_request_priority_t::MN_REQUEST_PRIORITY_LOW,
        RequestPriority::Lowest => sys::mn_request_priority_t::MN_REQUEST_PRIORITY_LOWEST,
        RequestPriority::Idle => sys::mn_request_priority_t::MN_REQUEST_PRIORITY_IDLE,
    }
}

unsafe fn shared<'a>(user_data: *mut std::ffi::c_void) -> &'a RequestShared {
    unsafe { &*user_data.cast::<RequestShared>() }
}

fn release_callback_ref(user_data: *const RequestShared) {
    let shared = unsafe { &*user_data };
    if shared.callback_ref_live.swap(false, Ordering::AcqRel) {
        unsafe { Arc::decrement_strong_count(user_data) };
    }
}

unsafe extern "C" fn on_response(
    user_data: *mut std::ffi::c_void,
    _request: *mut sys::mn_request_t,
    status_code: i32,
    reason_phrase: *const i8,
    reason_phrase_length: usize,
    header_fields: *const sys::mn_header_t,
    header_field_count: usize,
    headers: *const i8,
    headers_length: usize,
) {
    let shared = unsafe { shared(user_data) };
    // Copy before the callback returns: the Core borrows all three for the call.
    let reason = copy_utf8(reason_phrase, reason_phrase_length);
    let fields = copy_header_fields(header_fields, header_field_count);
    let raw = copy_bytes(headers.cast(), headers_length);
    if catch_unwind(AssertUnwindSafe(|| {
        let mut state = lock(&shared.state);
        state.response = Some(ResponseHead {
            status_code,
            reason_phrase: reason,
            header_fields: fields,
            raw_headers: raw,
        });
        let wakeups = shared.notify(&mut state);
        drop(state);
        wakeups.wake();
        shared.schedule_event();
    }))
    .is_err()
    {
        shared.callback_panicked();
    }
}

unsafe extern "C" fn on_body(
    user_data: *mut std::ffi::c_void,
    _request: *mut sys::mn_request_t,
    data: *const u8,
    data_length: usize,
) -> sys::mn_read_disposition_t {
    let shared = unsafe { shared(user_data) };
    match catch_unwind(AssertUnwindSafe(|| {
        let mut state = lock(&shared.state);
        // An abandoned queue has no consumer, so storing the chunk would only
        // grow memory that nothing drains. Keep reading and let the Core finish.
        if !state.body_abandoned {
            state.body.push_back(copy_bytes(data, data_length));
            state.body_bytes = state.body_bytes.saturating_add(data_length);
        }
        // Never pause after a terminal result: nothing would resume the request.
        // An abandoned queue needs no separate check here: it never accumulates
        // bytes, so `body_bytes` stays 0 and this cannot fire for it.
        let pause = state.body_bytes >= BODY_BUFFER_LIMIT && state.terminal.is_none();
        state.read_paused |= pause;
        let wakeups = shared.notify(&mut state);
        drop(state);
        wakeups.wake();
        shared.schedule_event();
        pause
    })) {
        Ok(true) => sys::mn_read_disposition_t::MN_READ_PAUSE,
        Ok(false) => sys::mn_read_disposition_t::MN_READ_CONTINUE,
        Err(_) => {
            shared.callback_panicked();
            // Keep reading so a panicking consumer cannot strand the Core
            // request in a paused state that nothing will resume.
            sys::mn_read_disposition_t::MN_READ_CONTINUE
        }
    }
}

unsafe extern "C" fn on_redirect(
    user_data: *mut std::ffi::c_void,
    _request: *mut sys::mn_request_t,
    status_code: i32,
    headers: *const i8,
    headers_length: usize,
    new_url: *const i8,
    new_url_length: usize,
    new_method: *const i8,
    new_method_length: usize,
) {
    let shared = unsafe { shared(user_data) };
    if catch_unwind(AssertUnwindSafe(|| {
        let mut state = lock(&shared.state);
        state.redirects.push_back(Redirect {
            status_code,
            headers: copy_bytes(headers.cast(), headers_length),
            new_url: copy_string(new_url, new_url_length),
            new_method: copy_string(new_method, new_method_length),
        });
        let wakeups = shared.notify(&mut state);
        drop(state);
        wakeups.wake();
        shared.schedule_event();
    }))
    .is_err()
    {
        shared.callback_panicked();
    }
}

unsafe extern "C" fn on_complete(
    user_data: *mut std::ffi::c_void,
    _request: *mut sys::mn_request_t,
    result: sys::mn_result_t,
    net_error: i32,
) {
    let shared = unsafe { shared(user_data) };
    if catch_unwind(AssertUnwindSafe(|| {
        let mut state = lock(&shared.state);
        state.terminal = Some(if result == sys::mn_result_t::MN_OK {
            Ok(net_error)
        } else {
            Err(RequestError {
                error: result.into(),
                net_error,
            })
        });
        let wakeups = shared.notify(&mut state);
        drop(state);
        wakeups.wake();
        shared.schedule_event();
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
    fn wait_for_redirect_preserves_the_net_error() {
        // The manual-redirect path used to unwrap `RequestError` down to its
        // `.error` category, so a -111 tunnel failure surfaced as the bare word
        // "Proxy" while the follow-redirect path named ERR_TUNNEL_CONNECTION_FAILED.
        // The net_error field is the only place that code lives.
        let error = RequestError {
            error: Error::Proxy,
            net_error: -111,
        };
        assert_eq!(format!("{error}"), "Proxy (Chromium net error -111)");
    }

    #[test]
    fn response_and_body_preserve_callback_order() {
        let shared = Arc::new(RequestShared::new());
        // Two fields, one of them duplicated: the array must survive both.
        let fields = [
            sys::mn_header_t {
                name: b"Set-Cookie".as_ptr().cast(),
                name_length: 10,
                value: b"a=1".as_ptr().cast(),
                value_length: 3,
            },
            sys::mn_header_t {
                name: b"Set-Cookie".as_ptr().cast(),
                name_length: 10,
                value: b"b=2".as_ptr().cast(),
                value_length: 3,
            },
        ];
        let user_data = Arc::into_raw(Arc::clone(&shared)).cast_mut().cast();
        unsafe {
            on_response(
                user_data,
                std::ptr::null_mut(),
                200,
                b"OK".as_ptr().cast(),
                2,
                fields.as_ptr(),
                fields.len(),
                b"h\n".as_ptr().cast(),
                2,
            );
            on_body(user_data, std::ptr::null_mut(), b"body".as_ptr(), 4);
            on_complete(user_data, std::ptr::null_mut(), sys::mn_result_t::MN_OK, 0);
        }
        // Asserted against the shared state rather than through ResponseStream:
        // building one needs a live Core request handle, which these offline
        // tests do not have.
        let mut state = lock(&shared.state);
        let head = state.response.take().unwrap();
        assert_eq!(head.status_code, 200);
        assert_eq!(head.raw_headers, b"h\n");
        // The reason phrase is its own ABI parameter now.
        assert_eq!(head.reason_phrase, "OK");
        // Duplicates and order both survive: Set-Cookie depends on them.
        assert_eq!(head.header_fields.len(), 2);
        assert_eq!(head.header_fields[0].name, "Set-Cookie");
        assert_eq!(head.header_fields[0].value, "a=1");
        assert_eq!(head.header_fields[1].value, "b=2");
        assert_eq!(take_body(&mut state).unwrap().unwrap().unwrap(), b"body");
        assert!(take_body(&mut state).unwrap().is_none());
        drop(state);
        assert!(!shared.callback_ref_live.load(Ordering::Acquire));
    }

    #[test]
    fn body_pauses_at_the_ceiling_and_resumes_after_draining() {
        let shared = Arc::new(RequestShared::new());
        let user_data = Arc::into_raw(Arc::clone(&shared)).cast_mut().cast();
        let chunk = vec![0u8; BODY_BUFFER_LIMIT / 2];

        // The first half-ceiling chunk keeps Core reading; the second reaches the
        // ceiling and must pause instead of blocking this thread.
        let first =
            unsafe { on_body(user_data, std::ptr::null_mut(), chunk.as_ptr(), chunk.len()) };
        assert_eq!(first, sys::mn_read_disposition_t::MN_READ_CONTINUE);
        let second =
            unsafe { on_body(user_data, std::ptr::null_mut(), chunk.as_ptr(), chunk.len()) };
        assert_eq!(second, sys::mn_read_disposition_t::MN_READ_PAUSE);
        assert!(lock(&shared.state).read_paused);

        // Draining one chunk drops below the ceiling and asks for a resume once.
        let mut state = lock(&shared.state);
        assert!(take_body(&mut state).is_some());
        assert!(clear_pause(&mut state));
        assert!(!clear_pause(&mut state));
        assert!(!state.read_paused);
        drop(state);

        release_callback_ref(user_data.cast());
    }

    #[test]
    fn body_never_pauses_after_a_terminal_result() {
        let shared = Arc::new(RequestShared::new());
        let user_data = Arc::into_raw(Arc::clone(&shared)).cast_mut().cast();
        unsafe { on_complete(user_data, std::ptr::null_mut(), sys::mn_result_t::MN_OK, 0) };

        // A pause here would strand the Core request: no consumer resume can
        // follow a terminal callback.
        let chunk = vec![0u8; BODY_BUFFER_LIMIT + 1];
        let disposition =
            unsafe { on_body(user_data, std::ptr::null_mut(), chunk.as_ptr(), chunk.len()) };
        assert_eq!(disposition, sys::mn_read_disposition_t::MN_READ_CONTINUE);
        assert!(!lock(&shared.state).read_paused);
        assert!(!shared.callback_ref_live.load(Ordering::Acquire));
    }

    #[test]
    fn event_hook_runs_without_the_callback_lock_held() {
        let shared = Arc::new(RequestShared::new());
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
        unsafe { on_body(user_data, std::ptr::null_mut(), b"x".as_ptr(), 1) };
        release_callback_ref(user_data.cast());

        assert!(
            observed.load(Ordering::Acquire),
            "schedule_event held event_callback while invoking the hook"
        );
    }

    #[test]
    fn a_lost_wakeup_does_not_pin_a_waiter_forever() {
        // What this guards: a wakeup that never arrives even though the state
        // already holds the answer. Condvar::wait only re-checks its predicate
        // when woken, so a lost notify parks the waiter until the next unrelated
        // one -- and if none comes, forever. A bounded park re-checks on its
        // own, so the answer is picked up within one slice.
        //
        // What this does NOT claim: a request whose terminal callback never
        // arrives still waits, because the state never changes at all. That is
        // a caller asking for no deadline, and the Core owns that policy.
        //
        // The answer is written AFTER the waiter is parked, otherwise the loop
        // would find it on entry and never exercise the wait.
        let shared = Arc::new(RequestShared::new());
        let waiter = Arc::clone(&shared);
        let handle = std::thread::spawn(move || {
            let mut state = lock(&waiter.state);
            let started = std::time::Instant::now();
            loop {
                if state.terminal.is_some() {
                    return started.elapsed();
                }
                let (next, _) = waiter
                    .changed
                    .wait_timeout(state, crate::WAIT_SLICE)
                    .unwrap_or_else(|poisoned| poisoned.into_inner());
                state = next;
            }
        });

        // Let the waiter park, then publish the answer without notifying:
        // this is exactly the lost-wakeup shape.
        std::thread::sleep(std::time::Duration::from_millis(50));
        {
            let mut state = lock(&shared.state);
            state.terminal = Some(Ok(204));
        }

        let elapsed = handle.join().expect("join");
        assert!(
            elapsed < crate::WAIT_SLICE * 3,
            "a parked waiter must re-check on its own, but it took {:?}",
            elapsed
        );
    }
}

#[cfg(test)]
mod abi_regressions {
    use super::*;
    use std::sync::atomic::AtomicUsize;
    use std::task::{RawWaker, RawWakerVTable, Waker};
    use std::time::Duration;

    /// A waker whose wake path re-enters the state mutex, the way an inline
    /// poll does. Waking this while the state lock is held deadlocks.
    fn reentrant_waker(shared: Arc<RequestShared>, hits: Arc<AtomicUsize>) -> Waker {
        unsafe fn clone(data: *const ()) -> RawWaker {
            let inner = unsafe { &*(data as *const (Arc<RequestShared>, Arc<AtomicUsize>)) };
            let boxed = Box::new((Arc::clone(&inner.0), Arc::clone(&inner.1)));
            RawWaker::new(Box::into_raw(boxed) as *const (), &VTABLE)
        }
        unsafe fn wake(data: *const ()) {
            let boxed =
                unsafe { Box::from_raw(data as *mut (Arc<RequestShared>, Arc<AtomicUsize>)) };
            boxed.1.fetch_add(1, Ordering::SeqCst);
            let _guard = lock(&boxed.0.state);
        }
        unsafe fn wake_by_ref(data: *const ()) {
            let inner = unsafe { &*(data as *const (Arc<RequestShared>, Arc<AtomicUsize>)) };
            inner.1.fetch_add(1, Ordering::SeqCst);
            let _guard = lock(&inner.0.state);
        }
        unsafe fn drop_fn(data: *const ()) {
            drop(unsafe { Box::from_raw(data as *mut (Arc<RequestShared>, Arc<AtomicUsize>)) });
        }
        static VTABLE: RawWakerVTable = RawWakerVTable::new(clone, wake, wake_by_ref, drop_fn);
        let boxed = Box::new((shared, hits));
        unsafe { Waker::from_raw(RawWaker::new(Box::into_raw(boxed) as *const (), &VTABLE)) }
    }

    #[test]
    fn notify_does_not_wake_the_waker_under_the_state_lock() {
        let shared = Arc::new(RequestShared::new());
        let hits = Arc::new(AtomicUsize::new(0));
        {
            let mut state = lock(&shared.state);
            state.body_waker = Some(reentrant_waker(Arc::clone(&shared), Arc::clone(&hits)));
        }

        let (tx, rx) = std::sync::mpsc::channel();
        let worker = Arc::clone(&shared);
        std::thread::spawn(move || {
            let mut state = lock(&worker.state);
            let wakeups = worker.notify(&mut state);
            drop(state);
            wakeups.wake();
            let _ = tx.send(());
        });

        // Pre-fix this blocked forever: wake() re-entered the mutex the worker
        // still held.
        rx.recv_timeout(Duration::from_secs(5))
            .expect("notify() woke the waker while the state lock was held");
        assert_eq!(hits.load(Ordering::SeqCst), 1, "the waker ran exactly once");
    }

    #[test]
    fn every_supplied_deadline_survives_the_millisecond_field() {
        // The ABI reads 0 as "no deadline" (request.cc arms the timer only when
        // timeout_ms_ > 0), so no supplied duration may encode to it -- not a
        // sub-millisecond one, and not Duration::ZERO either. ZERO is the
        // strongest request to stop, so mapping it to "wait forever" inverted
        // the caller's meaning.
        assert_eq!(timeout_ms(Some(Duration::from_nanos(1))), 1);
        assert_eq!(timeout_ms(Some(Duration::from_micros(999))), 1);
        assert_eq!(timeout_ms(Some(Duration::from_micros(1000))), 1);
        assert_eq!(timeout_ms(Some(Duration::from_millis(1500))), 1500);
        assert_eq!(timeout_ms(Some(Duration::ZERO)), 1);
        // Only "no deadline supplied" is encoded as 0.
        assert_eq!(timeout_ms(None), 0);
        // The mapping is monotonic: Some(_) is always a real deadline.
        for nanos in [0u64, 1, 999_999, 1_000_000, 1_000_000_000] {
            assert!(timeout_ms(Some(Duration::from_nanos(nanos))) >= 1);
        }
    }

    #[test]
    fn a_panicking_event_hook_does_not_escape_the_callback() {
        // schedule_event runs on a Core callback thread. An unguarded panic
        // there unwinds into `extern "C"`, which cannot unwind, so the runtime
        // aborts the host process. Measured before the guard: SIGABRT in 6/6
        // runs of a program that only installed a panicking hook.
        let shared = Arc::new(RequestShared::new());
        let panics = Arc::new(AtomicUsize::new(0));
        let counter = Arc::clone(&panics);
        *lock(&shared.event_callback) = Some(Arc::new(move || {
            counter.fetch_add(1, Ordering::SeqCst);
            panic!("hook exploded");
        }));

        // Must return rather than abort.
        shared.schedule_event();

        assert_eq!(panics.load(Ordering::SeqCst), 1, "the hook ran once");
        assert!(
            lock(&shared.event_callback).is_none(),
            "the broken hook is dropped so it cannot re-run on every callback"
        );
        let state = lock(&shared.state);
        match state.terminal {
            Some(Err(error)) => assert_eq!(error.error, Error::CallbackPanic),
            _ => panic!("a panicking hook must record Error::CallbackPanic"),
        }
    }

    #[test]
    fn dropping_the_body_stream_never_strands_a_paused_request() {
        // A drop while on_body had answered MN_READ_PAUSE left the Core waiting
        // for a resume_read that only a live consumer sends, so the request
        // never completed. Against an 8 MiB body it was still unfinished after
        // 6 s. The fix marks the queue abandoned, which stops the pause.
        let shared = Arc::new(RequestShared::new());
        let user_data = Arc::into_raw(Arc::clone(&shared)).cast_mut().cast();
        let chunk = vec![0u8; BODY_BUFFER_LIMIT];
        let paused =
            unsafe { on_body(user_data, std::ptr::null_mut(), chunk.as_ptr(), chunk.len()) };
        assert_eq!(paused, sys::mn_read_disposition_t::MN_READ_PAUSE);
        assert!(lock(&shared.state).read_paused);

        // This is what ResponseStream::drop runs. Building a real ResponseStream
        // here would need a live Core handle (RequestInner::drop releases it),
        // so the Drop -> abandon_body wiring is covered end to end by the
        // 8 MiB integration probe instead of by this offline unit test.
        assert!(
            shared.abandon_body(),
            "an abandoned queue must report that Core reads need a resume"
        );
        assert!(lock(&shared.state).body_abandoned);

        // A later chunk must neither be buffered nor pause again.
        let later =
            unsafe { on_body(user_data, std::ptr::null_mut(), chunk.as_ptr(), chunk.len()) };
        assert_eq!(later, sys::mn_read_disposition_t::MN_READ_CONTINUE);
        let state = lock(&shared.state);
        assert!(
            !state.read_paused,
            "nothing can lift a pause with no consumer"
        );
        assert!(state.body.is_empty(), "an abandoned queue stores nothing");
        drop(state);

        release_callback_ref(user_data.cast());
    }
}
