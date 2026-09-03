use std::{
    collections::VecDeque,
    future::Future,
    panic::{catch_unwind, AssertUnwindSafe},
    pin::Pin,
    ptr::NonNull,
    slice,
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, Condvar, Mutex,
    },
    task::{Context, Poll, Waker},
    time::Duration,
};

use futures_core::Stream;
use minicronet_sys as sys;

use crate::{ffi_bytes, lock, timeout_ms, Engine, Error};

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

#[derive(Debug)]
pub struct Response {
    pub status_code: i32,
    pub headers: Vec<u8>,
    pub body: ResponseStream,
}

#[derive(Default)]
struct RequestState {
    response: Option<(i32, Vec<u8>)>,
    response_taken: bool,
    body: VecDeque<Vec<u8>>,
    body_bytes: usize,
    // Set when on_body answered MN_READ_PAUSE. The consumer clears it and calls
    // `Request::resume_read` once the queue drops back under the ceiling.
    read_paused: bool,
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

impl RequestShared {
    fn new() -> Self {
        Self {
            state: Mutex::new(RequestState::default()),
            changed: Condvar::new(),
            event_callback: Mutex::new(None),
            callback_ref_live: AtomicBool::new(true),
        }
    }

    fn notify(&self, state: &mut RequestState) {
        if let Some(waker) = state.response_waker.take() {
            waker.wake();
        }
        if let Some(waker) = state.body_waker.take() {
            waker.wake();
        }
        self.changed.notify_all();
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
            callback();
        }
    }

    fn callback_panicked(&self) {
        let mut state = lock(&self.state);
        if state.terminal.is_none() {
            state.terminal = Some(Err(RequestError {
                error: Error::CallbackPanic,
                net_error: 0,
            }));
            self.notify(&mut state);
            drop(state);
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

    pub fn wait_for_redirect(&self, timeout: Duration) -> Result<Option<Redirect>, Error> {
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
            Err(error.error)
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
            state = self
                .shared
                .changed
                .wait(state)
                .unwrap_or_else(std::sync::PoisonError::into_inner);
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
            state = self
                .shared
                .changed
                .wait(state)
                .unwrap_or_else(std::sync::PoisonError::into_inner);
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
        if let Some((status_code, headers)) = state.response.take() {
            state.response_taken = true;
            return Some(Ok(Response {
                status_code,
                headers,
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

fn result(value: sys::mn_result_t) -> Result<(), Error> {
    if value == sys::mn_result_t::MN_OK {
        Ok(())
    } else {
        Err(value.into())
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

fn copy_bytes(data: *const u8, length: usize) -> Vec<u8> {
    if length == 0 {
        Vec::new()
    } else {
        unsafe { slice::from_raw_parts(data, length) }.to_vec()
    }
}

fn copy_string(data: *const i8, length: usize) -> String {
    String::from_utf8_lossy(&copy_bytes(data.cast(), length)).into_owned()
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
    headers: *const i8,
    headers_length: usize,
) {
    let shared = unsafe { shared(user_data) };
    if catch_unwind(AssertUnwindSafe(|| {
        let mut state = lock(&shared.state);
        state.response = Some((status_code, copy_bytes(headers.cast(), headers_length)));
        shared.notify(&mut state);
        drop(state);
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
        state.body.push_back(copy_bytes(data, data_length));
        state.body_bytes = state.body_bytes.saturating_add(data_length);
        // Never pause after a terminal result: nothing would resume the request.
        let pause = state.body_bytes >= BODY_BUFFER_LIMIT && state.terminal.is_none();
        state.read_paused |= pause;
        shared.notify(&mut state);
        drop(state);
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
        shared.notify(&mut state);
        drop(state);
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
        shared.notify(&mut state);
        drop(state);
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
    fn response_and_body_preserve_callback_order() {
        let shared = Arc::new(RequestShared::new());
        let user_data = Arc::into_raw(Arc::clone(&shared)).cast_mut().cast();
        unsafe {
            on_response(
                user_data,
                std::ptr::null_mut(),
                200,
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
        let (status_code, headers) = state.response.take().unwrap();
        assert_eq!(status_code, 200);
        assert_eq!(headers, b"h\n");
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
}
