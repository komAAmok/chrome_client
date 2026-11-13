//! Native primitives for the requests/curl_cffi-shaped Python facade.
//!
//! The Core owns all network work.  This module owns only handle lifetime,
//! configuration translation, and the asyncio notification bridge.
//!
//! Lifetime rule that matters most here: the asyncio bridge installs a Core
//! event callback that holds strong references to the event loop and to the
//! Python `notify` callable, and `notify` in turn holds this `PyRequest`.  That
//! cycle passes through Rust, so Python's garbage collector cannot break it.
//! Every terminal transition therefore clears the callback (see
//! `PyRequest::finish`), which is what keeps completed async requests from
//! leaking.

use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc, Mutex,
};
use std::time::{Duration, Instant};

use minicronet::{
    CacheMode, CloseInfo, Cookie, CookieSameSite, Engine, EngineConfig, Header, HttpCacheMode,
    HttpVersion, MessageType, Platform, ProtocolMode, Redirect, RedirectMode, RequestConfig,
    RequestPriority, ResponseFuture, ResponseStream, TlsVerifyMode, Upload, WebSocketConfig,
    WebSocketEvent, WebSocketEvents,
};
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;

fn native_error(error: impl std::fmt::Display) -> PyErr {
    PyRuntimeError::new_err(error.to_string())
}

type PyEvent = (String, i32, Vec<u8>, Vec<u8>, Option<String>);
type PyWebSocketEvent = (String, Vec<u8>, Option<u16>, Option<String>);
type PyRedirect = (i32, Vec<u8>, String, String);
type PyHead = (i32, Vec<u8>);
/// `(redirect, head)` with exactly one side set, or both `None` on timeout.
type PyManualWait = (Option<PyRedirect>, Option<PyHead>);

fn redirect_tuple(redirect: Redirect) -> PyRedirect {
    (
        redirect.status_code,
        redirect.headers,
        redirect.new_url,
        redirect.new_method,
    )
}

fn duration(seconds: Option<f64>) -> PyResult<Option<Duration>> {
    seconds
        .map(|value| {
            if value.is_sign_negative() || !value.is_finite() {
                return Err(native_error("timeout must be a finite non-negative number"));
            }
            // `Duration::from_secs_f64` panics above u64::MAX seconds
            // (~5.8e11 years). A huge-but-finite float therefore aborted the
            // process through a PanicException, which derives from
            // BaseException and so slips past every `except Exception`.
            // Rejecting it here keeps the failure a normal Python error.
            Duration::try_from_secs_f64(value)
                .map_err(|_| native_error("timeout must be a finite non-negative number"))
        })
        .transpose()
}

fn cache_mode(name: Option<&str>) -> PyResult<CacheMode> {
    Ok(match name.unwrap_or("default") {
        "default" => CacheMode::Default,
        "validate" => CacheMode::Validate,
        "bypass" => CacheMode::Bypass,
        "no_store" => CacheMode::NoStore,
        "force" => CacheMode::Force,
        "only_if_cached" => CacheMode::OnlyIfCached,
        other => return Err(native_error(format!("unknown cache mode {other:?}"))),
    })
}

fn request_priority(name: Option<&str>) -> PyResult<RequestPriority> {
    Ok(match name.unwrap_or("default") {
        "default" => RequestPriority::Default,
        "highest" => RequestPriority::Highest,
        "medium" => RequestPriority::Medium,
        "low" => RequestPriority::Low,
        "lowest" => RequestPriority::Lowest,
        "idle" => RequestPriority::Idle,
        other => return Err(native_error(format!("unknown priority {other:?}"))),
    })
}

fn redirect_mode(name: Option<&str>) -> PyResult<RedirectMode> {
    Ok(match name.unwrap_or("follow") {
        "follow" => RedirectMode::Follow,
        "manual" => RedirectMode::Manual,
        "error" => RedirectMode::Error,
        other => return Err(native_error(format!("unknown redirect mode {other:?}"))),
    })
}

/// Parses the platform identity name.
///
/// The three values that are not "auto" exist so a caller can present one
/// consistent platform across User-Agent and client hints; an unknown name is
/// an error rather than a silent fallback to "auto", because a silent fallback
/// is exactly how the UA and the hints end up disagreeing.
/// One cookie, flattened for Python.
///
/// `(name, value, domain, path, expiry_unix_seconds, has_expiry, secure,
/// http_only, same_site)`.
type PyCookie = (
    String,
    String,
    String,
    String,
    i64,
    bool,
    bool,
    bool,
    &'static str,
);

/// Maps the Core's cookie view onto the tuple the facade reads.
///
/// A tuple rather than a dict because the field order is fixed by the ABI and
/// the facade unpacks it once; a dict would let a rename pass silently.
fn cookie_tuple(
    cookie: Cookie,
) -> (
    String,
    String,
    String,
    String,
    i64,
    bool,
    bool,
    bool,
    &'static str,
) {
    (
        cookie.name,
        cookie.value,
        cookie.domain,
        cookie.path,
        cookie.expiry_unix_seconds,
        cookie.has_expiry,
        cookie.secure,
        cookie.http_only,
        cookie_same_site_name(cookie.same_site),
    )
}

/// Maps the negotiated protocol onto the string the facade reports.
///
/// The facade's vocabulary is the one `curl_cffi` uses, so a caller comparing
/// against that library's values needs no translation. `None` for `Unknown`:
/// the ABI documents that value as "Chromium does not know", and inventing
/// HTTP/1.1 there is exactly the misreport v9 removed.
fn http_version_name(value: HttpVersion) -> Option<&'static str> {
    match value {
        HttpVersion::Http09 => Some("HTTP/0.9"),
        HttpVersion::Http10 => Some("HTTP/1.0"),
        HttpVersion::Http11 => Some("HTTP/1.1"),
        HttpVersion::Http2 => Some("HTTP/2"),
        HttpVersion::Http3 => Some("HTTP/3"),
        HttpVersion::Unknown => None,
    }
}

fn cookie_same_site_name(value: CookieSameSite) -> &'static str {
    match value {
        CookieSameSite::NoRestriction => "none",
        CookieSameSite::Lax => "lax",
        CookieSameSite::Strict => "strict",
        CookieSameSite::Unspecified => "",
    }
}

fn cookie_same_site(name: &str) -> PyResult<CookieSameSite> {
    match name {
        "" | "unspecified" => Ok(CookieSameSite::Unspecified),
        "none" | "no_restriction" => Ok(CookieSameSite::NoRestriction),
        "lax" => Ok(CookieSameSite::Lax),
        "strict" => Ok(CookieSameSite::Strict),
        other => Err(PyValueError::new_err(format!(
            "unknown SameSite value {other:?}; expected none, lax, strict or empty"
        ))),
    }
}

fn platform_mode(name: &str) -> PyResult<Platform> {
    match name {
        "auto" => Ok(Platform::Auto),
        "windows" => Ok(Platform::Windows),
        "macos" => Ok(Platform::Macos),
        "linux" => Ok(Platform::Linux),
        _ => Err(PyValueError::new_err(
            "unknown platform; expected one of auto, windows, macos, linux",
        )),
    }
}

fn protocol_mode(name: Option<&str>) -> PyResult<ProtocolMode> {
    Ok(match name {
        None | Some("native") | Some("auto") => ProtocolMode::Native,
        Some("v1") | Some("http/1.1") | Some("h1") => ProtocolMode::ForceH1,
        Some("v2") | Some("http/2") | Some("h2") => ProtocolMode::ForceH2,
        Some("v3") | Some("http/3") | Some("h3") => ProtocolMode::ForceH3,
        Some(other) => return Err(native_error(format!("unknown http version {other:?}"))),
    })
}

#[pyclass]
struct PyResponse {
    #[pyo3(get)]
    status_code: i32,
    headers: Vec<u8>,
    body: Vec<u8>,
    /// The reason phrase the Core extracted from the status line.
    #[pyo3(get)]
    reason_phrase: String,
    /// Parsed header fields as (name, value) pairs, duplicates preserved.
    #[pyo3(get)]
    header_fields: Vec<(String, String)>,
    /// The negotiated protocol, as the Core reported it.
    ///
    /// `None` means Chromium itself does not know (a cache hit), which the
    /// facade reports as `None` rather than guessing HTTP/1.1.
    #[pyo3(get)]
    http_version: Option<&'static str>,
}

#[pymethods]
impl PyResponse {
    #[getter]
    fn headers(&self) -> Vec<u8> {
        self.headers.clone()
    }

    #[getter]
    fn content(&self) -> Vec<u8> {
        self.body.clone()
    }
}

#[pyclass]
struct PyRequest {
    inner: minicronet::Request,
    response_future: Mutex<Option<ResponseFuture>>,
    body_stream: Mutex<Option<ResponseStream>>,
    notify_scheduled: Arc<AtomicBool>,
    done: AtomicBool,
}

impl PyRequest {
    /// Releases everything a terminal event makes unreachable.  Clearing the
    /// Core event callback drops the captured event loop and `notify` callable,
    /// which is the only way to break the Rust-spanning reference cycle the
    /// asyncio bridge creates.
    fn finish(&self) {
        self.inner.clear_event_callback();
        self.response_future
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .take();
    }
}

#[pyclass]
struct PyWebSocket {
    inner: minicronet::WebSocket,
    events: Mutex<Option<WebSocketEvents>>,
    notify_scheduled: Arc<AtomicBool>,
}

fn websocket_event(event: WebSocketEvent) -> PyWebSocketEvent {
    match event {
        WebSocketEvent::Open { protocol, .. } => ("open".into(), protocol.into_bytes(), None, None),
        WebSocketEvent::Message {
            message_type, data, ..
        } => {
            let kind = match message_type {
                MessageType::Text => "text",
                MessageType::Binary | MessageType::Continuation => "binary",
            };
            (kind.into(), data, None, None)
        }
        WebSocketEvent::Closing => ("closing".into(), Vec::new(), None, None),
        WebSocketEvent::Closed(CloseInfo { code, reason, .. }) => {
            ("closed".into(), Vec::new(), Some(code), Some(reason))
        }
        WebSocketEvent::Failure(failure) => {
            ("error".into(), Vec::new(), None, Some(failure.to_string()))
        }
    }
}

#[pymethods]
impl PyWebSocket {
    fn connect(&self) -> PyResult<()> {
        let events = self.inner.start().map_err(native_error)?;
        *self
            .events
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner) = Some(events);
        Ok(())
    }

    fn send_text(&self, text: &str) -> PyResult<()> {
        self.inner.send_text(text).map_err(native_error)
    }

    fn send_bytes(&self, data: Vec<u8>) -> PyResult<()> {
        self.inner.send_binary(&data).map_err(native_error)
    }

    fn recv(&self, py: Python<'_>) -> PyResult<Option<PyWebSocketEvent>> {
        py.detach(|| {
            let mut events = self
                .events
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner);
            let events = events
                .as_mut()
                .ok_or_else(|| native_error("WebSocket is not connected"))?;
            Ok(events.blocking_next().map(websocket_event))
        })
    }

    fn close(&self, code: u16, reason: &str) -> PyResult<()> {
        let result = self.inner.close(code, reason).map_err(native_error);
        self.inner.clear_event_callback();
        result
    }

    fn cancel(&self) -> PyResult<()> {
        let result = self.inner.cancel().map_err(native_error);
        self.inner.clear_event_callback();
        result
    }

    fn detach_callback(&self) {
        self.inner.clear_event_callback();
    }

    fn is_finished(&self) -> bool {
        self.inner.is_finished()
    }

    fn start_async(&self, py: Python<'_>, loop_: Py<PyAny>, notify: Py<PyAny>) -> PyResult<()> {
        let scheduled = Arc::clone(&self.notify_scheduled);
        let loop_ref = loop_.clone_ref(py);
        let notify_ref = notify.clone_ref(py);
        self.inner.set_event_callback(Arc::new(move || {
            if scheduled.swap(true, Ordering::AcqRel) {
                return;
            }
            Python::attach(|py| {
                if loop_ref
                    .call_method1(py, "call_soon_threadsafe", (notify_ref.clone_ref(py),))
                    .is_err()
                {
                    scheduled.store(false, Ordering::Release);
                }
            });
        }));
        self.connect()
    }

    fn poll_event(&self) -> PyResult<Option<PyWebSocketEvent>> {
        self.notify_scheduled.store(false, Ordering::Release);
        let mut events = self
            .events
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let events = events
            .as_mut()
            .ok_or_else(|| native_error("WebSocket is not connected"))?;
        let event = events.try_next().map(websocket_event);
        if matches!(
            event.as_ref().map(|(kind, ..)| kind.as_str()),
            Some("closed") | Some("error")
        ) {
            self.inner.clear_event_callback();
        }
        Ok(event)
    }

    /// Drains up to `limit` queued events in one call so a busy socket does not
    /// pay an event-loop round trip per frame.
    #[pyo3(signature = (limit=64))]
    fn poll_events(&self, limit: usize) -> PyResult<Vec<PyWebSocketEvent>> {
        self.notify_scheduled.store(false, Ordering::Release);
        let mut events = self
            .events
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let events = events
            .as_mut()
            .ok_or_else(|| native_error("WebSocket is not connected"))?;
        let mut drained = Vec::new();
        let mut terminal = false;
        while drained.len() < limit.max(1) {
            match events.try_next() {
                Some(event) => {
                    let event = websocket_event(event);
                    terminal = matches!(event.0.as_str(), "closed" | "error");
                    drained.push(event);
                    if terminal {
                        break;
                    }
                }
                None => break,
            }
        }
        if terminal {
            self.inner.clear_event_callback();
        }
        Ok(drained)
    }
}

#[pymethods]
impl PyRequest {
    fn cancel(&self) -> PyResult<()> {
        let result = self.inner.cancel().map_err(native_error);
        self.finish();
        result
    }

    fn detach_callback(&self) {
        self.finish();
    }

    fn is_finished(&self) -> bool {
        self.inner.is_finished()
    }

    /// Feeds one chunk of a chunked upload.  Only valid when the request was
    /// created with `upload="chunked"`.
    fn upload_write(&self, py: Python<'_>, data: Vec<u8>, final_chunk: bool) -> PyResult<()> {
        py.detach(|| {
            self.inner
                .upload_write(&data, final_chunk)
                .map_err(native_error)
        })
    }

    fn upload_finish(&self, py: Python<'_>) -> PyResult<()> {
        py.detach(|| self.inner.upload_finish().map_err(native_error))
    }

    /// The protocol the connection used, or `None` when the Core does not know.
    ///
    /// Readable from the response event onwards. The async pump calls this when
    /// it sees that event, rather than widening the event tuple: the tuple is
    /// unpacked positionally in several places, and a sixth element would break
    /// every one of them for a value only the head needs.
    /// The parsed header fields of the response head, duplicates preserved.
    fn header_fields(&self) -> Option<Vec<(String, String)>> {
        self.inner.response_head().map(|head| {
            head.header_fields
                .into_iter()
                .map(|field| (field.name, field.value))
                .collect()
        })
    }

    /// The reason phrase the Core extracted, if a response has started.
    fn reason_phrase(&self) -> Option<String> {
        self.inner.response_head().map(|head| head.reason_phrase)
    }

    fn http_version(&self) -> Option<&'static str> {
        http_version_name(self.inner.negotiated_protocol()?)
    }

    fn resume_read(&self) -> PyResult<()> {
        self.inner.resume_read().map_err(native_error)
    }

    fn follow_redirect(&self) -> PyResult<()> {
        self.inner.follow_redirect().map_err(native_error)
    }

    /// Drains redirect hops the Core has already reported.  The Core reports
    /// them in follow mode too, which is what lets the facade rebuild
    /// `Response.history` and the post-redirect URL without deferring hops.
    fn take_redirects(&self) -> Vec<PyRedirect> {
        let mut hops = Vec::new();
        while let Some(redirect) = self.inner.take_redirect() {
            hops.push(redirect_tuple(redirect));
        }
        hops
    }

    #[pyo3(signature = (timeout=None))]
    fn wait_redirect(&self, py: Python<'_>, timeout: Option<f64>) -> PyResult<Option<PyRedirect>> {
        let deadline = duration(timeout)?.unwrap_or(Duration::from_secs(86_400));
        py.detach(|| {
            self.inner
                .wait_for_redirect(deadline)
                .map(|hop| hop.map(redirect_tuple))
                .map_err(native_error)
        })
    }

    /// Blocks until the Core reports a deferred redirect or the response
    /// headers arrive, whichever happens first.
    ///
    /// `manual` redirect mode makes Chromium defer the hop and wait for
    /// `follow_redirect`, so waiting only on the response would hang.  Returns
    /// `(redirect, head)` with exactly one side set, or `(None, None)` on timeout.
    #[pyo3(signature = (timeout=None))]
    fn wait_manual(&self, py: Python<'_>, timeout: Option<f64>) -> PyResult<PyManualWait> {
        let total = duration(timeout)?.unwrap_or(Duration::from_secs(86_400));
        // `Instant + Duration` panics once the sum leaves the range the clock
        // can represent, and a PanicException derives from BaseException, so it
        // slips past every `except Exception` and kills the caller. `checked_add`
        // keeps an unrepresentable deadline an ordinary Python error.
        let deadline = Instant::now()
            .checked_add(total)
            .ok_or_else(|| native_error("timeout is too large for the system clock"))?;
        py.detach(|| {
            loop {
                if let Some(hop) = self.inner.take_redirect() {
                    return Ok((Some(redirect_tuple(hop)), None));
                }
                let taken = {
                    let mut future = self
                        .response_future
                        .lock()
                        .unwrap_or_else(std::sync::PoisonError::into_inner);
                    future.as_mut().and_then(ResponseFuture::try_take)
                };
                if let Some(result) = taken {
                    self.response_future
                        .lock()
                        .unwrap_or_else(std::sync::PoisonError::into_inner)
                        .take();
                    let response = result.map_err(|error| {
                        self.finish();
                        native_error(error)
                    })?;
                    let head = (response.status_code, response.headers);
                    *self
                        .body_stream
                        .lock()
                        .unwrap_or_else(std::sync::PoisonError::into_inner) = Some(response.body);
                    return Ok((None, Some(head)));
                }
                let now = Instant::now();
                if now >= deadline {
                    return Ok((None, None));
                }
                // The Core notifies this condvar for responses too, so the slice
                // is an upper bound on latency, not a poll interval.
                let slice = std::cmp::min(deadline - now, Duration::from_millis(50));
                if let Some(hop) = self.inner.wait_for_redirect(slice).map_err(native_error)? {
                    return Ok((Some(redirect_tuple(hop)), None));
                }
            }
        })
    }

    fn start_stream(&self, py: Python<'_>) -> PyResult<PyResponse> {
        py.detach(|| {
            let response = self
                .inner
                .start()
                .map_err(native_error)?
                .wait()
                .map_err(native_error)?;
            *self
                .body_stream
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner) = Some(response.body);
            Ok(PyResponse {
                status_code: response.status_code,
                headers: response.headers,
                body: Vec::new(),
                reason_phrase: response.reason_phrase,
                header_fields: response
                    .header_fields
                    .into_iter()
                    .map(|field| (field.name, field.value))
                    .collect(),
                http_version: http_version_name(response.http_version),
            })
        })
    }

    /// Starts the request without waiting for headers.  The facade uses this for
    /// chunked uploads, where the body must be fed before headers can arrive.
    fn start(&self) -> PyResult<()> {
        let response_future = self.inner.start().map_err(native_error)?;
        *self
            .response_future
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner) = Some(response_future);
        Ok(())
    }

    /// Waits for the headers of a request already started by `start`.
    fn await_response(&self, py: Python<'_>) -> PyResult<PyResponse> {
        let future = self
            .response_future
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .take()
            .ok_or_else(|| native_error("request is not started"))?;
        py.detach(|| {
            let response = future.wait().map_err(native_error)?;
            *self
                .body_stream
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner) = Some(response.body);
            Ok(PyResponse {
                status_code: response.status_code,
                headers: response.headers,
                body: Vec::new(),
                reason_phrase: response.reason_phrase,
                header_fields: response
                    .header_fields
                    .into_iter()
                    .map(|field| (field.name, field.value))
                    .collect(),
                http_version: http_version_name(response.http_version),
            })
        })
    }

    fn next_body(&self, py: Python<'_>) -> PyResult<Option<Vec<u8>>> {
        py.detach(|| {
            let mut stream = self
                .body_stream
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner);
            let stream = stream
                .as_mut()
                .ok_or_else(|| native_error("request body stream is not started"))?;
            stream.blocking_next().transpose().map_err(native_error)
        })
    }

    /// Reads the whole body with the GIL released.  Saves one Python round trip
    /// per chunk on the non-streaming synchronous path.
    fn read_body(&self, py: Python<'_>) -> PyResult<Vec<u8>> {
        py.detach(|| {
            let mut stream = self
                .body_stream
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner);
            let stream = stream
                .as_mut()
                .ok_or_else(|| native_error("request body stream is not started"))?;
            let mut body = Vec::new();
            while let Some(chunk) = stream.blocking_next() {
                body.extend_from_slice(&chunk.map_err(native_error)?);
            }
            Ok(body)
        })
    }

    /// Starts a request and arranges a thread-safe notification on an
    /// asyncio-compatible event loop. The callback only schedules `notify`; it
    /// never runs user Python code or waits for network I/O.
    fn start_async(&self, py: Python<'_>, loop_: Py<PyAny>, notify: Py<PyAny>) -> PyResult<()> {
        self.attach_async(py, loop_, notify);
        let response_future = self.inner.start().map_err(native_error)?;
        *self
            .response_future
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner) = Some(response_future);
        Ok(())
    }

    /// Registers the asyncio bridge without starting the request, for chunked
    /// uploads that must be fed after `start`.
    fn attach_async(&self, py: Python<'_>, loop_: Py<PyAny>, notify: Py<PyAny>) {
        self.install_event_bridge(py, loop_, notify);
    }

    /// Returns one non-blocking event as `(kind, status, headers, body, error)`.
    /// `kind` is `response`, `body`, `done`, or `error`; `None` means pending.
    fn poll_event(&self) -> PyResult<Option<PyEvent>> {
        self.notify_scheduled.store(false, Ordering::Release);
        self.poll_one()
    }

    /// Drains up to `limit` events in one call.  Batching keeps a large download
    /// from costing one event-loop round trip per Core chunk.
    #[pyo3(signature = (limit=64))]
    fn poll_events(&self, limit: usize) -> PyResult<Vec<PyEvent>> {
        self.notify_scheduled.store(false, Ordering::Release);
        let mut drained = Vec::new();
        while drained.len() < limit.max(1) {
            match self.poll_one()? {
                Some(event) => {
                    let terminal = matches!(event.0.as_str(), "done" | "error");
                    drained.push(event);
                    if terminal {
                        break;
                    }
                }
                None => break,
            }
        }
        Ok(drained)
    }
}

impl PyRequest {
    /// Installs the asyncio notification bridge.
    ///
    /// The hook holds strong references to the event loop and to the Python
    /// `notify` callable, and `notify` holds this `PyRequest`. That cycle
    /// passes through Rust, so Python's GC cannot break it; `finish` clears the
    /// callback to release it. Both `start_async` and `attach_async` install
    /// exactly this bridge, so it lives here once.
    fn install_event_bridge(&self, py: Python<'_>, loop_: Py<PyAny>, notify: Py<PyAny>) {
        let scheduled = Arc::clone(&self.notify_scheduled);
        let loop_ref = loop_.clone_ref(py);
        let notify_ref = notify.clone_ref(py);
        self.inner.set_event_callback(Arc::new(move || {
            if scheduled.swap(true, Ordering::AcqRel) {
                return;
            }
            Python::attach(|py| {
                if loop_ref
                    .call_method1(py, "call_soon_threadsafe", (notify_ref.clone_ref(py),))
                    .is_err()
                {
                    scheduled.store(false, Ordering::Release);
                }
            });
        }));
    }

    fn poll_one(&self) -> PyResult<Option<PyEvent>> {
        // Redirects are reported before the response in every mode, and in
        // manual mode the response never arrives until `follow_redirect`, so the
        // hop has to reach the caller as an event of its own.
        if let Some(hop) = self.inner.take_redirect() {
            return Ok(Some((
                "redirect".into(),
                hop.status_code,
                hop.headers,
                hop.new_url.into_bytes(),
                Some(hop.new_method),
            )));
        }

        let response = {
            let mut future = self
                .response_future
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner);
            future.as_mut().and_then(ResponseFuture::try_take)
        };
        if let Some(response) = response {
            self.response_future
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner)
                .take();
            return Ok(Some(match response {
                Ok(response) => {
                    *self
                        .body_stream
                        .lock()
                        .unwrap_or_else(std::sync::PoisonError::into_inner) = Some(response.body);
                    (
                        "response".into(),
                        response.status_code,
                        response.headers,
                        Vec::new(),
                        None,
                    )
                }
                Err(error) => {
                    if self.done.swap(true, Ordering::AcqRel) {
                        return Ok(None);
                    }
                    self.finish();
                    (
                        "error".into(),
                        0,
                        Vec::new(),
                        Vec::new(),
                        Some(error.to_string()),
                    )
                }
            }));
        }

        let body_event = {
            let mut stream = self
                .body_stream
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner);
            stream.as_mut().and_then(ResponseStream::try_next)
        };
        if let Some(body_event) = body_event {
            return Ok(Some(match body_event {
                Some(Ok(body)) => ("body".into(), 0, Vec::new(), body, None),
                Some(Err(error)) => {
                    if self.done.swap(true, Ordering::AcqRel) {
                        return Ok(None);
                    }
                    self.finish();
                    (
                        "error".into(),
                        0,
                        Vec::new(),
                        Vec::new(),
                        Some(error.to_string()),
                    )
                }
                None => {
                    if self.done.swap(true, Ordering::AcqRel) {
                        return Ok(None);
                    }
                    self.finish();
                    ("done".into(), 0, Vec::new(), Vec::new(), None)
                }
            }));
        }

        Ok(None)
    }
}

/// Normalises the impersonation aliases the facade accepts (`chrome136`,
/// `chrome_136`) onto the profile identifiers the Core registers.
fn profile_identifier(value: String) -> String {
    if let Some(version) = value.strip_prefix("chrome") {
        let version = version.strip_prefix('_').unwrap_or(version);
        if !version.is_empty() && version.chars().all(|character| character.is_ascii_digit()) {
            return format!("chrome_{version}");
        }
    }
    value
}

#[pyclass]
struct PyEngine {
    inner: Engine,
}

#[pymethods]
impl PyEngine {
    #[new]
    #[pyo3(signature = (
        impersonate=None,
        proxy=None,
        verify=true,
        ca_pem=None,
        user_agent=None,
        accept_language=None,
        proxy_username=None,
        proxy_password=None,
        http_version=None,
        cache=true,
        profile_namespace=None,
        host_mapping=None,
        dns_nameservers=None,
        doh_template=None,
        dns_secure_mode=0,
        dns_over_tls=false,
        dns_rotate=false,
        dns_padding=false,
        hsts_bypass=None,
        proxy_chain=None,
        platform="auto",
    ))]
    #[allow(clippy::too_many_arguments)]
    fn new(
        impersonate: Option<String>,
        proxy: Option<String>,
        verify: bool,
        ca_pem: Option<Vec<u8>>,
        user_agent: Option<String>,
        accept_language: Option<String>,
        proxy_username: Option<String>,
        proxy_password: Option<String>,
        http_version: Option<&str>,
        cache: bool,
        profile_namespace: Option<String>,
        host_mapping: Option<String>,
        dns_nameservers: Option<String>,
        doh_template: Option<String>,
        dns_secure_mode: u8,
        dns_over_tls: bool,
        dns_rotate: bool,
        dns_padding: bool,
        hsts_bypass: Option<String>,
        proxy_chain: Option<String>,
        platform: &str,
    ) -> PyResult<Self> {
        let tls_verify = if !verify {
            TlsVerifyMode::Insecure
        } else if ca_pem.is_some() {
            TlsVerifyMode::CustomCa
        } else {
            TlsVerifyMode::ChromiumDefault
        };
        let config = EngineConfig {
            profile_id: impersonate.map(profile_identifier),
            profile_namespace,
            user_agent,
            accept_language,
            proxy_rules: proxy,
            proxy_username,
            proxy_password,
            http_cache: if cache {
                HttpCacheMode::Enabled
            } else {
                HttpCacheMode::Disabled
            },
            protocol: protocol_mode(http_version)?,
            tls_verify,
            custom_ca_pem: ca_pem,
            host_mapping_rules: host_mapping,
            dns_nameservers,
            doh_template,
            hsts_bypass_list: hsts_bypass,
            proxy_chain,
            dns_secure_mode,
            dns_over_tls: u8::from(dns_over_tls),
            dns_rotate: u8::from(dns_rotate),
            dns_padding: u8::from(dns_padding),
            platform: platform_mode(platform)?,
        };
        Ok(Self {
            inner: Engine::new(config).map_err(native_error)?,
        })
    }

    // ---- cookie store (ABI v9).
    //
    // Each returns plain Python data: the facade builds its own jar from
    // these instead of mirroring what it thinks the store holds, which is the
    // compensation the Core replaced.

    /// Reads the store. `url=None` returns everything; otherwise the cookies a
    /// request to that URL would carry.
    #[pyo3(signature = (url=None))]
    fn cookies(&self, url: Option<String>) -> PyResult<Vec<PyCookie>> {
        let cookies = self.inner.cookies(url.as_deref()).map_err(native_error)?;
        Ok(cookies.into_iter().map(cookie_tuple).collect())
    }

    /// Writes one cookie through the store's own validation.
    #[allow(clippy::too_many_arguments)]
    fn set_cookie(
        &self,
        name: String,
        value: String,
        domain: String,
        path: String,
        source_url: String,
        secure: bool,
        http_only: bool,
        same_site: &str,
        has_expiry: bool,
        expiry_unix_seconds: i64,
    ) -> PyResult<()> {
        let cookie = Cookie {
            name,
            value,
            domain,
            path,
            expiry_unix_seconds,
            has_expiry,
            secure,
            http_only,
            same_site: cookie_same_site(same_site)?,
        };
        self.inner
            .set_cookie(&cookie, &source_url)
            .map_err(native_error)
    }

    /// Deletes every cookie matching the triple; returns how many went.
    fn delete_cookie(&self, domain: String, path: String, name: String) -> PyResult<usize> {
        self.inner
            .delete_cookie(&domain, &path, &name)
            .map_err(native_error)
    }

    /// Empties the store.
    fn clear_cookies(&self) -> PyResult<()> {
        self.inner.clear_cookies().map_err(native_error)
    }

    #[staticmethod]
    fn abi_version() -> u32 {
        Engine::core_abi_version()
    }

    #[staticmethod]
    fn core_version() -> Option<String> {
        Engine::core_version().map(|value| value.to_string_lossy().into_owned())
    }

    #[pyo3(signature = (
        method,
        url,
        headers=None,
        body=None,
        timeout=None,
        allow_redirects=true,
        redirect=None,
        cache=None,
        priority=None,
        chunked=false,
    ))]
    #[allow(clippy::too_many_arguments)]
    fn request(
        &self,
        method: &str,
        url: &str,
        headers: Option<Vec<(String, String)>>,
        body: Option<Vec<u8>>,
        timeout: Option<f64>,
        allow_redirects: bool,
        redirect: Option<&str>,
        cache: Option<&str>,
        priority: Option<&str>,
        chunked: bool,
    ) -> PyResult<PyRequest> {
        let mut config = RequestConfig::get(url.to_owned());
        config.method = method.to_owned();
        config.timeout = duration(timeout)?;
        config.redirect = match redirect {
            Some(name) => redirect_mode(Some(name))?,
            None if allow_redirects => RedirectMode::Follow,
            None => RedirectMode::Manual,
        };
        config.cache = cache_mode(cache)?;
        config.priority = request_priority(priority)?;
        config.headers = headers
            .unwrap_or_default()
            .into_iter()
            .map(|(name, value)| Header::new(name, value))
            .collect();
        config.upload = if chunked {
            Upload::Chunked
        } else {
            match body {
                Some(body) => Upload::Fixed(body),
                None => Upload::None,
            }
        };
        Ok(PyRequest {
            inner: self.inner.request(config).map_err(native_error)?,
            response_future: Mutex::new(None),
            body_stream: Mutex::new(None),
            notify_scheduled: Arc::new(AtomicBool::new(false)),
            done: AtomicBool::new(false),
        })
    }

    #[pyo3(signature = (url, origin="", headers=None, timeout=None, protocols=None))]
    fn websocket(
        &self,
        url: &str,
        origin: &str,
        headers: Option<Vec<(String, String)>>,
        timeout: Option<f64>,
        protocols: Option<Vec<String>>,
    ) -> PyResult<PyWebSocket> {
        let mut config = WebSocketConfig::new(url, origin);
        config.headers = headers
            .unwrap_or_default()
            .into_iter()
            .map(|(name, value)| Header::new(name, value))
            .collect();
        config.protocols = protocols.unwrap_or_default();
        config.timeout = duration(timeout)?;
        Ok(PyWebSocket {
            inner: self.inner.websocket(config).map_err(native_error)?,
            events: Mutex::new(None),
            notify_scheduled: Arc::new(AtomicBool::new(false)),
        })
    }
}

// The callback bridge attaches to Python to schedule asyncio callbacks and
// the public facade owns mutable Python state; retain the GIL on free-threaded
// interpreters until a dedicated Python 3.13t test matrix is available.
#[pymodule(gil_used = true)]
fn chrome_client_native(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<PyEngine>()?;
    module.add_class::<PyRequest>()?;
    module.add_class::<PyResponse>()?;
    module.add_class::<PyWebSocket>()?;
    module.add("abi_version", Engine::core_abi_version())?;
    Ok(())
}
