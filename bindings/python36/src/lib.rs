//! Python 3.6 compatibility extension.
//!
//! This deliberately keeps the ABI-3 surface small.  The requests-shaped
//! facade is shared with the modern package; this module supplies the same
//! native primitives using the last PyO3 release supporting Python 3.6.
//!
//! Keep this in step with `bindings/python/src/lib.rs`: one Python package
//! ships against both extensions, so a method missing here becomes an
//! `AttributeError` on Python 3.6 only.

use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc, Mutex,
};
use std::time::{Duration, Instant};

use minicronet::{
    CacheMode, CloseInfo, Cookie, CookieSameSite, EchStatus, Engine as CoreEngine,
    EngineConfig, Header, HttpCacheMode, HttpVersion, MessageType, Platform, ProtocolMode,
    Redirect, RedirectMode, RequestConfig, RequestPriority, ResponseFuture, ResponseStream,
    TlsVerifyMode, Upload, WebSocketConfig, WebSocketEvent, WebSocketEvents,
};
use pyo3::{exceptions::PyRuntimeError, prelude::*, types::PyBytes};

fn error(value: impl std::fmt::Display) -> PyErr {
    PyRuntimeError::new_err(value.to_string())
}

// The facade is shared with the modern package and unpacks both tuples
// positionally, so their arity is part of the contract between the two crates.
// It was 5/4 here while the facade unpacked 6/5: the shapes had drifted and
// nothing compared them, which is why tools/audit-python36-artifact.py now
// drives a redirect and an async request through the legacy extension
// instead of only importing it.
//
// The three byte-carrying fields are PyBytes, not Vec<u8>: pyo3 0.15 converts a
// Vec<u8> into a Python LIST of ints, while the modern binding pyo3 0.30
// converts it into bytes. The facade is shared, so it treats a body chunk as
// bytes -- b''.join(state.body) on the async path raised
// "sequence item 0: expected a bytes-like object, list found" for every
// non-streaming async request through this extension. Converting at the
// boundary is what makes both extensions hand the same Python types to the
// same facade.
type PyEvent = (
    String,
    i32,
    Py<PyBytes>,
    Py<PyBytes>,
    Option<String>,
    Vec<(String, String)>,
);
type PyWebSocketEvent = (String, Py<PyBytes>, Option<u16>, Option<String>);
type PyRedirect = (i32, Py<PyBytes>, String, String, Vec<(String, String)>);
type PyHead = (i32, Py<PyBytes>);
/// `(redirect, head)` with exactly one side set, or both `None` on timeout.
type PyManualWait = (Option<PyRedirect>, Option<PyHead>);

fn redirect_tuple(py: Python, redirect: Redirect) -> PyRedirect {
    (
        redirect.status_code,
        PyBytes::new(py, &redirect.headers).into(),
        redirect.new_url,
        redirect.new_method,
        redirect
            .fields
            .into_iter()
            .map(|field| (field.name, field.value))
            .collect(),
    )
}


/// Maps the negotiated protocol onto the string the facade reports.
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

/// Maps the Core's ECH result onto the string the facade reports.
fn ech_status_name(value: EchStatus) -> Option<&'static str> {
    Some(match value {
        EchStatus::Success => "success",
        EchStatus::Rejected => "rejected",
        EchStatus::NotAttempted => "not_attempted",
    })
}

fn cookie_same_site_name(value: CookieSameSite) -> &'static str {
    match value {
        CookieSameSite::Unspecified => "unspecified",
        CookieSameSite::NoRestriction => "no_restriction",
        CookieSameSite::Lax => "lax",
        CookieSameSite::Strict => "strict",
    }
}

fn cookie_same_site(name: &str) -> PyResult<CookieSameSite> {
    // Case-insensitive, and "none" accepted as an alias for no_restriction,
    // exactly as the modern binding does: a `SameSite=None` cookie reaches
    // this from the jar with the server's own capitalization.
    Ok(match name.to_ascii_lowercase().as_str() {
        // The facade passes an empty string for "the jar had no SameSite".
        "" | "unspecified" => CookieSameSite::Unspecified,
        "none" | "no_restriction" => CookieSameSite::NoRestriction,
        "lax" => CookieSameSite::Lax,
        "strict" => CookieSameSite::Strict,
        other => return Err(error(format!("unknown same_site {other:?}"))),
    })
}

/// One cookie, flattened for Python. Same order as the modern binding.
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

fn cookie_tuple(cookie: Cookie) -> PyCookie {
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
fn duration(seconds: Option<f64>) -> PyResult<Option<Duration>> {
    seconds
        .map(|value| {
            if !value.is_finite() || value < 0.0 {
                return Err(error("timeout must be a finite non-negative number"));
            }
            // See the modern binding: from_secs_f64 panics on an out-of-range
            // value, and a PanicException escapes `except Exception`.
            Duration::try_from_secs_f64(value)
                .map_err(|_| error("timeout must be a finite non-negative number"))
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
        other => return Err(error(format!("unknown cache mode {:?}", other))),
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
        other => return Err(error(format!("unknown priority {:?}", other))),
    })
}

fn redirect_mode(name: Option<&str>) -> PyResult<RedirectMode> {
    Ok(match name.unwrap_or("follow") {
        "follow" => RedirectMode::Follow,
        "manual" => RedirectMode::Manual,
        "error" => RedirectMode::Error,
        other => return Err(error(format!("unknown redirect mode {:?}", other))),
    })
}

fn protocol_mode(name: Option<&str>) -> PyResult<ProtocolMode> {
    Ok(match name {
        None | Some("native") | Some("auto") => ProtocolMode::Native,
        Some("v1") | Some("http/1.1") | Some("h1") => ProtocolMode::ForceH1,
        Some("v2") | Some("http/2") | Some("h2") => ProtocolMode::ForceH2,
        Some("v3") | Some("http/3") | Some("h3") => ProtocolMode::ForceH3,
        Some(other) => return Err(error(format!("unknown http version {:?}", other))),
    })
}

fn platform_mode(name: Option<&str>) -> PyResult<Platform> {
    Ok(match name {
        None | Some("auto") => Platform::Auto,
        Some("windows") => Platform::Windows,
        Some("macos") => Platform::Macos,
        Some("linux") => Platform::Linux,
        Some(other) => return Err(error(format!("unknown platform {other:?}"))),
    })
}

fn profile_identifier(value: String) -> String {
    if let Some(version) = value.strip_prefix("chrome") {
        let version = version.strip_prefix('_').unwrap_or(version);
        if !version.is_empty() && version.chars().all(|c| c.is_ascii_digit()) {
            return format!("chrome_{}", version);
        }
    }
    value
}

#[pyclass]
struct PyResponse {
    status_code: i32,
    headers: Vec<u8>,
    content: Vec<u8>,
    /// Whether Encrypted ClientHello was used. The facade reads this off the
    /// head object, so the 3.6 extension needs it for the same reason the
    /// modern one does -- without it Response.ech_status is always None.
    ech_status: Option<&'static str>,
}

#[pymethods]
impl PyResponse {
    #[getter]
    fn status_code(&self) -> i32 {
        self.status_code
    }
    #[getter]
    fn headers<'p>(&self, py: Python<'p>) -> &'p PyBytes {
        PyBytes::new(py, &self.headers)
    }
    #[getter]
    fn content<'p>(&self, py: Python<'p>) -> &'p PyBytes {
        PyBytes::new(py, &self.content)
    }
    #[getter]
    fn ech_status(&self) -> Option<&'static str> {
        self.ech_status
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

#[pyclass]
struct PyWebSocket {
    inner: minicronet::WebSocket,
    events: Mutex<Option<WebSocketEvents>>,
    notify_scheduled: Arc<AtomicBool>,
}

fn websocket_event(py: Python, event: WebSocketEvent) -> PyWebSocketEvent {
    match event {
        WebSocketEvent::Open { protocol, .. } => (
            "open".into(),
            PyBytes::new(py, protocol.as_bytes()).into(),
            None,
            None,
        ),
        WebSocketEvent::Message {
            message_type, data, ..
        } => {
            let kind = match message_type {
                MessageType::Text => "text",
                MessageType::Binary | MessageType::Continuation => "binary",
            };
            (kind.into(), PyBytes::new(py, &data).into(), None, None)
        }
        WebSocketEvent::Closing => ("closing".into(), PyBytes::new(py, &[]).into(), None, None),
        WebSocketEvent::Closed(CloseInfo { code, reason, .. }) => (
            "closed".into(),
            PyBytes::new(py, &[]).into(),
            Some(code),
            Some(reason),
        ),
        WebSocketEvent::Failure(failure) => (
            "error".into(),
            PyBytes::new(py, &[]).into(),
            None,
            Some(failure.to_string()),
        ),
    }
}

#[pymethods]
impl PyWebSocket {
    fn connect(&self) -> PyResult<()> {
        *self.events.lock().unwrap_or_else(std::sync::PoisonError::into_inner) = Some(self.inner.start().map_err(error)?);
        Ok(())
    }
    fn send_text(&self, text: &str) -> PyResult<()> {
        self.inner.send_text(text).map_err(error)
    }
    fn send_bytes(&self, data: Vec<u8>) -> PyResult<()> {
        self.inner.send_binary(&data).map_err(error)
    }
    fn recv(&self, py: Python) -> PyResult<Option<PyWebSocketEvent>> {
        let event = py.allow_threads(|| {
            let mut events = self.events.lock().unwrap_or_else(std::sync::PoisonError::into_inner);
            let events = events
                .as_mut()
                .ok_or_else(|| error("WebSocket is not connected"))?;
            Ok::<Option<WebSocketEvent>, PyErr>(events.blocking_next())
        })?;
        Ok(event.map(|event| websocket_event(py, event)))
    }
    fn close(&self, code: u16, reason: &str) -> PyResult<()> {
        let result = self.inner.close(code, reason).map_err(error);
        self.inner.clear_event_callback();
        result
    }
    fn cancel(&self) -> PyResult<()> {
        let result = self.inner.cancel().map_err(error);
        self.inner.clear_event_callback();
        result
    }
    fn detach_callback(&self) {
        self.inner.clear_event_callback();
    }
    fn is_finished(&self) -> bool {
        self.inner.is_finished()
    }
    fn start_async(&self, py: Python, loop_: PyObject, notify: PyObject) -> PyResult<()> {
        let scheduled = Arc::clone(&self.notify_scheduled);
        let loop_ref = loop_.clone_ref(py);
        let notify_ref = notify.clone_ref(py);
        self.inner.set_event_callback(Arc::new(move || {
            if scheduled.swap(true, Ordering::AcqRel) {
                return;
            }
            Python::with_gil(|py| {
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
    fn poll_event(&self, py: Python) -> PyResult<Option<PyWebSocketEvent>> {
        self.notify_scheduled.store(false, Ordering::Release);
        let mut events = self.events.lock().unwrap_or_else(std::sync::PoisonError::into_inner);
        let events = events
            .as_mut()
            .ok_or_else(|| error("WebSocket is not connected"))?;
        let event = events.try_next().map(|event| websocket_event(py, event));
        let terminal = match event.as_ref() {
            Some(event) => event.0 == "closed" || event.0 == "error",
            None => false,
        };
        if terminal {
            self.inner.clear_event_callback();
        }
        Ok(event)
    }
    #[args(limit = "64")]
    fn poll_events(&self, py: Python, limit: usize) -> PyResult<Vec<PyWebSocketEvent>> {
        self.notify_scheduled.store(false, Ordering::Release);
        let mut events = self.events.lock().unwrap_or_else(std::sync::PoisonError::into_inner);
        let events = events
            .as_mut()
            .ok_or_else(|| error("WebSocket is not connected"))?;
        let cap = if limit == 0 { 1 } else { limit };
        let mut drained = Vec::new();
        let mut terminal = false;
        while drained.len() < cap {
            match events.try_next() {
                Some(event) => {
                    let event = websocket_event(py, event);
                    terminal = event.0 == "closed" || event.0 == "error";
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

impl PyRequest {
    /// Releases everything a terminal event makes unreachable.  Clearing the
    /// Core event callback drops the captured event loop and `notify` callable,
    /// which is the only way to break the Rust-spanning reference cycle the
    /// asyncio bridge creates.
    fn finish(&self) {
        self.inner.clear_event_callback();
        self.response_future.lock().unwrap_or_else(std::sync::PoisonError::into_inner).take();
    }

    fn poll_one(&self, py: Python) -> PyResult<Option<PyEvent>> {
        // Redirects are reported before the response in every mode, and in
        // manual mode the response never arrives until `follow_redirect`, so the
        // hop has to reach the caller as an event of its own.
        if let Some(hop) = self.inner.take_redirect() {
            let fields = hop
                .fields
                .iter()
                .map(|field| (field.name.clone(), field.value.clone()))
                .collect();
            return Ok(Some((
                "redirect".into(),
                hop.status_code,
                PyBytes::new(py, &hop.headers).into(),
                PyBytes::new(py, hop.new_url.as_bytes()).into(),
                Some(hop.new_method),
                fields,
            )));
        }
        let response = self
            .response_future
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .as_mut()
            .and_then(ResponseFuture::try_take);
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
                        PyBytes::new(py, &response.headers).into(),
                        PyBytes::new(py, &[]).into(),
                        None,
                        Vec::new(),
                    )
                }
                Err(err) => {
                    if self.done.swap(true, Ordering::AcqRel) {
                        return Ok(None);
                    }
                    self.finish();
                    (
                        "error".into(),
                        0,
                        PyBytes::new(py, &[]).into(),
                        PyBytes::new(py, &[]).into(),
                        Some(err.to_string()),
                        Vec::new(),
                    )
                }
            }));
        }
        let body = self
            .body_stream
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .as_mut()
            .and_then(ResponseStream::try_next);
        if let Some(body) = body {
            return Ok(Some(match body {
                Some(Ok(chunk)) => (
                    "body".into(),
                    0,
                    PyBytes::new(py, &[]).into(),
                    PyBytes::new(py, &chunk).into(),
                    None,
                    Vec::new(),
                ),
                Some(Err(err)) => {
                    if self.done.swap(true, Ordering::AcqRel) {
                        return Ok(None);
                    }
                    self.finish();
                    (
                        "error".into(),
                        0,
                        PyBytes::new(py, &[]).into(),
                        PyBytes::new(py, &[]).into(),
                        Some(err.to_string()),
                        Vec::new(),
                    )
                }
                None => {
                    if self.done.swap(true, Ordering::AcqRel) {
                        return Ok(None);
                    }
                    self.finish();
                    (
                        "done".into(),
                        0,
                        PyBytes::new(py, &[]).into(),
                        PyBytes::new(py, &[]).into(),
                        None,
                        Vec::new(),
                    )
                }
            }));
        }
        Ok(None)
    }
}

#[pymethods]
impl PyRequest {
    fn cancel(&self) -> PyResult<()> {
        let result = self.inner.cancel().map_err(error);
        self.finish();
        result
    }

    fn detach_callback(&self) {
        self.finish();
    }

    fn is_finished(&self) -> bool {
        self.inner.is_finished()
    }

    fn upload_write(&self, py: Python, data: Vec<u8>, final_chunk: bool) -> PyResult<()> {
        py.allow_threads(|| self.inner.upload_write(&data, final_chunk).map_err(error))
    }

    fn upload_finish(&self, py: Python) -> PyResult<()> {
        py.allow_threads(|| self.inner.upload_finish().map_err(error))
    }

    /// The parsed header fields of the response head, duplicates preserved.
    fn header_fields(&self) -> Option<Vec<(String, String)>> {
        self.inner.response_head().map(|head| {
            head.header_fields
                .into_iter()
                .map(|field| (field.name, field.value))
                .collect()
        })
    }

    /// Whether Encrypted ClientHello was used, once a response has started.
    fn ech_status(&self) -> Option<&'static str> {
        self.inner.ech_status().map(|value| match value {
            EchStatus::Success => "success",
            EchStatus::Rejected => "rejected",
            EchStatus::NotAttempted => "not_attempted",
        })
    }

    /// The reason phrase the Core extracted, if a response has started.
    fn reason_phrase(&self) -> Option<String> {
        self.inner.response_head().map(|head| head.reason_phrase)
    }

    /// The protocol the connection used, or None when the Core does not know.
    fn http_version(&self) -> Option<&'static str> {
        http_version_name(self.inner.negotiated_protocol()?)
    }

    fn resume_read(&self) -> PyResult<()> {
        self.inner.resume_read().map_err(error)
    }

    fn follow_redirect(&self) -> PyResult<()> {
        self.inner.follow_redirect().map_err(error)
    }

    fn take_redirects(&self, py: Python) -> Vec<PyRedirect> {
        let mut hops = Vec::new();
        while let Some(redirect) = self.inner.take_redirect() {
            hops.push(redirect_tuple(py, redirect));
        }
        hops
    }

    #[args(timeout = "None")]
    fn wait_redirect(&self, py: Python, timeout: Option<f64>) -> PyResult<Option<PyRedirect>> {
        let deadline = duration(timeout)?.unwrap_or_else(|| Duration::from_secs(86_400));
        // The hop is taken without the GIL, then turned into Python objects with
        // it: PyBytes cannot be built inside allow_threads.
        let hop = py.allow_threads(|| self.inner.wait_for_redirect(deadline).map_err(error))?;
        Ok(hop.map(|hop| redirect_tuple(py, hop)))
    }

    /// Blocks until the Core reports a deferred redirect or the response
    /// headers arrive, whichever happens first.  See the modern binding for the
    /// reasoning: waiting only on the response would hang in manual mode.
    #[args(timeout = "None")]
    fn wait_manual(&self, py: Python, timeout: Option<f64>) -> PyResult<PyManualWait> {
        let total = duration(timeout)?.unwrap_or_else(|| Duration::from_secs(86_400));
        // `Instant + Duration` panics once the sum leaves the range the clock
        // can represent, and a PanicException derives from BaseException, so it
        // slips past every `except Exception` and kills the caller. `checked_add`
        // keeps an unrepresentable deadline an ordinary Python error.
        let deadline = Instant::now()
            .checked_add(total)
            .ok_or_else(|| error("timeout is too large for the system clock"))?;
        // Waits without the GIL, then builds the tuple with it. The hop carries
        // PyBytes now, which cannot be constructed on a detached thread.
        enum Waited {
            Hop(Redirect),
            Head(i32, Vec<u8>),
            Nothing,
        }
        let waited = py.allow_threads(|| {
            loop {
                if let Some(hop) = self.inner.take_redirect() {
                    return Ok(Waited::Hop(hop));
                }
                let taken = self
                    .response_future
                    .lock()
                    .unwrap_or_else(std::sync::PoisonError::into_inner)
                    .as_mut()
                    .and_then(ResponseFuture::try_take);
                if let Some(result) = taken {
                    self.response_future
                        .lock()
                        .unwrap_or_else(std::sync::PoisonError::into_inner)
                        .take();
                    let response = match result {
                        Ok(response) => response,
                        Err(err) => {
                            self.finish();
                            return Err(error(err));
                        }
                    };
                    let head = (response.status_code, response.headers);
                    *self
                        .body_stream
                        .lock()
                        .unwrap_or_else(std::sync::PoisonError::into_inner) = Some(response.body);
                    return Ok(Waited::Head(head.0, head.1));
                }
                let now = Instant::now();
                if now >= deadline {
                    return Ok(Waited::Nothing);
                }
                let slice = std::cmp::min(deadline - now, Duration::from_millis(50));
                if let Some(hop) = self.inner.wait_for_redirect(slice).map_err(error)? {
                    return Ok(Waited::Hop(hop));
                }
            }
        })?;
        Ok(match waited {
            Waited::Hop(hop) => (Some(redirect_tuple(py, hop)), None),
            Waited::Head(status_code, headers) => (
                None,
                Some((status_code, PyBytes::new(py, &headers).into())),
            ),
            Waited::Nothing => (None, None),
        })
    }

    fn start_stream(&self, py: Python) -> PyResult<PyResponse> {
        py.allow_threads(|| {
            let response = self.inner.start().map_err(error)?.wait().map_err(error)?;
            *self.body_stream.lock().unwrap_or_else(std::sync::PoisonError::into_inner) = Some(response.body);
            Ok(PyResponse {
                status_code: response.status_code,
                headers: response.headers,
                content: Vec::new(),
                ech_status: ech_status_name(response.ech_status),
            })
        })
    }

    fn start(&self) -> PyResult<()> {
        let future = self.inner.start().map_err(error)?;
        *self.response_future.lock().unwrap_or_else(std::sync::PoisonError::into_inner) = Some(future);
        Ok(())
    }

    fn await_response(&self, py: Python) -> PyResult<PyResponse> {
        let future = self
            .response_future
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .take()
            .ok_or_else(|| error("request is not started"))?;
        py.allow_threads(|| {
            let response = future.wait().map_err(error)?;
            *self.body_stream.lock().unwrap_or_else(std::sync::PoisonError::into_inner) = Some(response.body);
            Ok(PyResponse {
                status_code: response.status_code,
                headers: response.headers,
                content: Vec::new(),
                ech_status: ech_status_name(response.ech_status),
            })
        })
    }

    fn next_body(&self, py: Python) -> PyResult<Option<Vec<u8>>> {
        py.allow_threads(|| {
            let mut stream = self.body_stream.lock().unwrap_or_else(std::sync::PoisonError::into_inner);
            let stream = stream
                .as_mut()
                .ok_or_else(|| error("request body stream is not started"))?;
            stream.blocking_next().transpose().map_err(error)
        })
    }

    fn read_body(&self, py: Python) -> PyResult<Vec<u8>> {
        py.allow_threads(|| {
            let mut stream = self.body_stream.lock().unwrap_or_else(std::sync::PoisonError::into_inner);
            let stream = stream
                .as_mut()
                .ok_or_else(|| error("request body stream is not started"))?;
            let mut body = Vec::new();
            while let Some(chunk) = stream.blocking_next() {
                body.extend_from_slice(&chunk.map_err(error)?);
            }
            Ok(body)
        })
    }

    fn start_async(&self, py: Python, loop_: PyObject, notify: PyObject) -> PyResult<()> {
        self.attach_async(py, loop_, notify);
        let future = self.inner.start().map_err(error)?;
        *self.response_future.lock().unwrap_or_else(std::sync::PoisonError::into_inner) = Some(future);
        Ok(())
    }

    fn attach_async(&self, py: Python, loop_: PyObject, notify: PyObject) {
        let scheduled = Arc::clone(&self.notify_scheduled);
        let loop_ref = loop_.clone_ref(py);
        let notify_ref = notify.clone_ref(py);
        self.inner.set_event_callback(Arc::new(move || {
            if scheduled.swap(true, Ordering::AcqRel) {
                return;
            }
            Python::with_gil(|py| {
                if loop_ref
                    .call_method1(py, "call_soon_threadsafe", (notify_ref.clone_ref(py),))
                    .is_err()
                {
                    scheduled.store(false, Ordering::Release);
                }
            });
        }));
    }

    fn poll_event(&self, py: Python) -> PyResult<Option<PyEvent>> {
        self.notify_scheduled.store(false, Ordering::Release);
        self.poll_one(py)
    }

    #[args(limit = "64")]
    fn poll_events(&self, py: Python, limit: usize) -> PyResult<Vec<PyEvent>> {
        self.notify_scheduled.store(false, Ordering::Release);
        let cap = if limit == 0 { 1 } else { limit };
        let mut drained = Vec::new();
        while drained.len() < cap {
            match self.poll_one(py)? {
                Some(event) => {
                    let terminal = event.0 == "done" || event.0 == "error";
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

#[pyclass]
struct PyEngine {
    inner: CoreEngine,
}

#[pymethods]
impl PyEngine {
    #[new]
    // The shared facade passes every option the modern extension accepts, by
    // keyword, and it does so unconditionally: it cannot know which extension it
    // is talking to. A keyword this constructor does not declare is a TypeError
    // on the FIRST REQUEST, not at import, so the whole 3.6 wheel was unusable
    // while tools/audit-python36-artifact.py still passed -- it only imported the
    // module. Every option is therefore declared here, and forwarded: a keyword
    // that is accepted and then ignored is the silent ignore AGENTS.md section 8
    // forbids.
    #[args(
        impersonate = "None",
        proxy = "None",
        verify = "true",
        ca_pem = "None",
        user_agent = "None",
        accept_language = "None",
        proxy_username = "None",
        proxy_password = "None",
        http_version = "None",
        cache = "true",
        profile_namespace = "None",
        host_mapping = "None",
        dns_nameservers = "None",
        doh_template = "None",
        dns_secure_mode = "None",
        dns_over_tls = "false",
        dns_rotate = "false",
        dns_padding = "false",
        hsts_bypass = "None",
        proxy_chain = "None",
        platform = "None",
        max_sockets_per_group = "0"
    )]
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
        http_version: Option<String>,
        cache: bool,
        profile_namespace: Option<String>,
        host_mapping: Option<String>,
        dns_nameservers: Option<String>,
        doh_template: Option<String>,
        dns_secure_mode: Option<i32>,
        dns_over_tls: bool,
        dns_rotate: bool,
        dns_padding: bool,
        hsts_bypass: Option<String>,
        proxy_chain: Option<String>,
        platform: Option<String>,
        max_sockets_per_group: u32,
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
            protocol: protocol_mode(http_version.as_deref())?,
            tls_verify,
            custom_ca_pem: ca_pem,
            host_mapping_rules: host_mapping,
            dns_nameservers,
            doh_template,
            hsts_bypass_list: hsts_bypass,
            proxy_chain,
            dns_secure_mode: dns_secure_mode.unwrap_or(0) as u8,
            dns_over_tls: u8::from(dns_over_tls),
            dns_rotate: u8::from(dns_rotate),
            dns_padding: u8::from(dns_padding),
            platform: platform_mode(platform.as_deref())?,
            max_sockets_per_group,
        };
        Ok(PyEngine {
            inner: CoreEngine::new(config).map_err(error)?,
        })
    }

    /// Reads the store. url=None returns everything; otherwise the cookies a
    /// request to that URL would carry.
    #[args(url = "None")]
    fn cookies(&self, py: Python, url: Option<String>) -> PyResult<Vec<PyCookie>> {
        py.allow_threads(|| {
            let cookies = self.inner.cookies(url.as_deref()).map_err(error)?;
            Ok(cookies.into_iter().map(cookie_tuple).collect())
        })
    }

    /// Writes one cookie through the store own validation.
    ///
    /// No #[args] defaults: the facade always passes all eleven positionally,
    /// exactly as it does for the modern extension, and a default here would be
    /// a second place the argument order could drift.
    #[allow(clippy::too_many_arguments)]
    fn set_cookie(
        &self,
        py: Python,
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
        py.allow_threads(|| self.inner.set_cookie(&cookie, &source_url).map_err(error))
    }

    /// Deletes every cookie matching the triple; returns how many went.
    fn delete_cookie(
        &self,
        py: Python,
        domain: String,
        path: String,
        name: String,
    ) -> PyResult<usize> {
        py.allow_threads(|| self.inner.delete_cookie(&domain, &path, &name).map_err(error))
    }

    /// Empties the store.
    fn clear_cookies(&self, py: Python) -> PyResult<()> {
        py.allow_threads(|| self.inner.clear_cookies().map_err(error))
    }

    /// Registers the client certificate presented to one server.
    fn set_client_certificate(
        &self,
        py: Python,
        host: String,
        port: u16,
        cert_pem: Vec<u8>,
        key_pem: Vec<u8>,
    ) -> PyResult<()> {
        py.allow_threads(|| {
            self.inner
                .set_client_certificate(&host, port, &cert_pem, &key_pem)
                .map_err(error)
        })
    }

    /// Forgets the certificate registered for (host, port).
    fn clear_client_certificate(&self, py: Python, host: String, port: u16) -> PyResult<()> {
        py.allow_threads(|| self.inner.clear_client_certificate(&host, port).map_err(error))
    }
    #[staticmethod]
    fn abi_version() -> u32 {
        CoreEngine::core_abi_version()
    }

    #[staticmethod]
    fn core_version() -> Option<String> {
        CoreEngine::core_version().map(|value| value.to_string_lossy().into_owned())
    }

    // The facade passes the first eleven positionally, exactly as it does for the
    // modern extension: strip_authorization is the eleventh and the two body
    // options follow by keyword. A shorter signature here was a TypeError on the
    // first request rather than at import.
    #[args(
        headers = "None",
        body = "None",
        timeout = "None",
        allow_redirects = "true",
        redirect = "None",
        cache = "None",
        priority = "None",
        chunked = "false",
        strip_authorization = "None",
        take_body_buffers = "None"
    )]
    #[allow(clippy::too_many_arguments)]
    fn request(
        &self,
        method: &str,
        url: &str,
        headers: Option<Vec<(String, String)>>,
        body: Option<Vec<u8>>,
        timeout: Option<f64>,
        allow_redirects: bool,
        redirect: Option<String>,
        cache: Option<String>,
        priority: Option<String>,
        chunked: bool,
        strip_authorization: Option<bool>,
        take_body_buffers: Option<bool>,
    ) -> PyResult<PyRequest> {
        let mut config = RequestConfig::get(url);
        config.method = method.to_owned();
        config.timeout = duration(timeout)?;
        config.redirect = match redirect.as_deref() {
            Some(name) => redirect_mode(Some(name))?,
            None if allow_redirects => RedirectMode::Follow,
            None => RedirectMode::Manual,
        };
        config.cache = cache_mode(cache.as_deref())?;
        config.priority = request_priority(priority.as_deref())?;
        // ABI 12. Off unless asked for, matching what an ABI 11 caller got.
        config.strip_authorization_on_cross_origin_redirect =
            strip_authorization.unwrap_or(false);
        config.take_body_buffers = take_body_buffers.unwrap_or(false);
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
            inner: self.inner.request(config).map_err(error)?,
            response_future: Mutex::new(None),
            body_stream: Mutex::new(None),
            notify_scheduled: Arc::new(AtomicBool::new(false)),
            done: AtomicBool::new(false),
        })
    }

    #[args(
        origin = "\"\"",
        headers = "None",
        timeout = "None",
        protocols = "None"
    )]
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
            inner: self.inner.websocket(config).map_err(error)?,
            events: Mutex::new(None),
            notify_scheduled: Arc::new(AtomicBool::new(false)),
        })
    }
}

#[pymodule]
fn chrome_client_native36(_py: Python, module: &PyModule) -> PyResult<()> {
    module.add_class::<PyEngine>()?;
    module.add_class::<PyRequest>()?;
    module.add_class::<PyResponse>()?;
    module.add_class::<PyWebSocket>()?;
    module.add("abi_version", CoreEngine::core_abi_version())?;
    Ok(())
}
