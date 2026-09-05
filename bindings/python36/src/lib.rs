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
    CacheMode, CloseInfo, Engine as CoreEngine, EngineConfig, Header, HttpCacheMode, MessageType,
    ProtocolMode, Redirect, RedirectMode, RequestConfig, RequestPriority, ResponseFuture,
    ResponseStream, TlsVerifyMode, Upload, WebSocketConfig, WebSocketEvent, WebSocketEvents,
};
use pyo3::{exceptions::PyRuntimeError, prelude::*, types::PyBytes};

fn error(value: impl std::fmt::Display) -> PyErr {
    PyRuntimeError::new_err(value.to_string())
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
            if !value.is_finite() || value < 0.0 {
                return Err(error("timeout must be a finite non-negative number"));
            }
            Ok(Duration::from_secs_f64(value))
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
        *self.events.lock().unwrap() = Some(self.inner.start().map_err(error)?);
        Ok(())
    }
    fn send_text(&self, text: &str) -> PyResult<()> {
        self.inner.send_text(text).map_err(error)
    }
    fn send_bytes(&self, data: Vec<u8>) -> PyResult<()> {
        self.inner.send_binary(&data).map_err(error)
    }
    fn recv(&self, py: Python) -> PyResult<Option<PyWebSocketEvent>> {
        py.allow_threads(|| {
            let mut events = self.events.lock().unwrap();
            let events = events
                .as_mut()
                .ok_or_else(|| error("WebSocket is not connected"))?;
            Ok(events.blocking_next().map(websocket_event))
        })
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
    fn poll_event(&self) -> PyResult<Option<PyWebSocketEvent>> {
        self.notify_scheduled.store(false, Ordering::Release);
        let mut events = self.events.lock().unwrap();
        let events = events
            .as_mut()
            .ok_or_else(|| error("WebSocket is not connected"))?;
        let event = events.try_next().map(websocket_event);
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
    fn poll_events(&self, limit: usize) -> PyResult<Vec<PyWebSocketEvent>> {
        self.notify_scheduled.store(false, Ordering::Release);
        let mut events = self.events.lock().unwrap();
        let events = events
            .as_mut()
            .ok_or_else(|| error("WebSocket is not connected"))?;
        let cap = if limit == 0 { 1 } else { limit };
        let mut drained = Vec::new();
        let mut terminal = false;
        while drained.len() < cap {
            match events.try_next() {
                Some(event) => {
                    let event = websocket_event(event);
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
        self.response_future.lock().unwrap().take();
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
        let response = self
            .response_future
            .lock()
            .unwrap()
            .as_mut()
            .and_then(ResponseFuture::try_take);
        if let Some(response) = response {
            self.response_future.lock().unwrap().take();
            return Ok(Some(match response {
                Ok(response) => {
                    *self.body_stream.lock().unwrap() = Some(response.body);
                    (
                        "response".into(),
                        response.status_code,
                        response.headers,
                        Vec::new(),
                        None,
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
                        Vec::new(),
                        Vec::new(),
                        Some(err.to_string()),
                    )
                }
            }));
        }
        let body = self
            .body_stream
            .lock()
            .unwrap()
            .as_mut()
            .and_then(ResponseStream::try_next);
        if let Some(body) = body {
            return Ok(Some(match body {
                Some(Ok(chunk)) => ("body".into(), 0, Vec::new(), chunk, None),
                Some(Err(err)) => {
                    if self.done.swap(true, Ordering::AcqRel) {
                        return Ok(None);
                    }
                    self.finish();
                    (
                        "error".into(),
                        0,
                        Vec::new(),
                        Vec::new(),
                        Some(err.to_string()),
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

    fn resume_read(&self) -> PyResult<()> {
        self.inner.resume_read().map_err(error)
    }

    fn follow_redirect(&self) -> PyResult<()> {
        self.inner.follow_redirect().map_err(error)
    }

    fn take_redirects(&self) -> Vec<PyRedirect> {
        let mut hops = Vec::new();
        while let Some(redirect) = self.inner.take_redirect() {
            hops.push(redirect_tuple(redirect));
        }
        hops
    }

    #[args(timeout = "None")]
    fn wait_redirect(&self, py: Python, timeout: Option<f64>) -> PyResult<Option<PyRedirect>> {
        let deadline = duration(timeout)?.unwrap_or_else(|| Duration::from_secs(86_400));
        py.allow_threads(|| {
            self.inner
                .wait_for_redirect(deadline)
                .map(|hop| hop.map(redirect_tuple))
                .map_err(error)
        })
    }

    /// Blocks until the Core reports a deferred redirect or the response
    /// headers arrive, whichever happens first.  See the modern binding for the
    /// reasoning: waiting only on the response would hang in manual mode.
    #[args(timeout = "None")]
    fn wait_manual(
        &self,
        py: Python,
        timeout: Option<f64>,
    ) -> PyResult<PyManualWait> {
        let total = duration(timeout)?.unwrap_or_else(|| Duration::from_secs(86_400));
        py.allow_threads(|| {
            let deadline = Instant::now() + total;
            loop {
                if let Some(hop) = self.inner.take_redirect() {
                    return Ok((Some(redirect_tuple(hop)), None));
                }
                let taken = self
                    .response_future
                    .lock()
                    .unwrap()
                    .as_mut()
                    .and_then(ResponseFuture::try_take);
                if let Some(result) = taken {
                    self.response_future.lock().unwrap().take();
                    let response = match result {
                        Ok(response) => response,
                        Err(err) => {
                            self.finish();
                            return Err(error(err));
                        }
                    };
                    let head = (response.status_code, response.headers);
                    *self.body_stream.lock().unwrap() = Some(response.body);
                    return Ok((None, Some(head)));
                }
                let now = Instant::now();
                if now >= deadline {
                    return Ok((None, None));
                }
                let slice = std::cmp::min(deadline - now, Duration::from_millis(50));
                if let Some(hop) = self.inner.wait_for_redirect(slice).map_err(error)? {
                    return Ok((Some(redirect_tuple(hop)), None));
                }
            }
        })
    }

    fn start_stream(&self, py: Python) -> PyResult<PyResponse> {
        py.allow_threads(|| {
            let response = self.inner.start().map_err(error)?.wait().map_err(error)?;
            *self.body_stream.lock().unwrap() = Some(response.body);
            Ok(PyResponse {
                status_code: response.status_code,
                headers: response.headers,
                content: Vec::new(),
            })
        })
    }

    fn start(&self) -> PyResult<()> {
        let future = self.inner.start().map_err(error)?;
        *self.response_future.lock().unwrap() = Some(future);
        Ok(())
    }

    fn await_response(&self, py: Python) -> PyResult<PyResponse> {
        let future = self
            .response_future
            .lock()
            .unwrap()
            .take()
            .ok_or_else(|| error("request is not started"))?;
        py.allow_threads(|| {
            let response = future.wait().map_err(error)?;
            *self.body_stream.lock().unwrap() = Some(response.body);
            Ok(PyResponse {
                status_code: response.status_code,
                headers: response.headers,
                content: Vec::new(),
            })
        })
    }

    fn next_body(&self, py: Python) -> PyResult<Option<Vec<u8>>> {
        py.allow_threads(|| {
            let mut stream = self.body_stream.lock().unwrap();
            let stream = stream
                .as_mut()
                .ok_or_else(|| error("request body stream is not started"))?;
            stream.blocking_next().transpose().map_err(error)
        })
    }

    fn read_body(&self, py: Python) -> PyResult<Vec<u8>> {
        py.allow_threads(|| {
            let mut stream = self.body_stream.lock().unwrap();
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
        *self.response_future.lock().unwrap() = Some(future);
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

    fn poll_event(&self) -> PyResult<Option<PyEvent>> {
        self.notify_scheduled.store(false, Ordering::Release);
        self.poll_one()
    }

    #[args(limit = "64")]
    fn poll_events(&self, limit: usize) -> PyResult<Vec<PyEvent>> {
        self.notify_scheduled.store(false, Ordering::Release);
        let cap = if limit == 0 { 1 } else { limit };
        let mut drained = Vec::new();
        while drained.len() < cap {
            match self.poll_one()? {
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
        profile_namespace = "None"
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
        };
        Ok(PyEngine {
            inner: CoreEngine::new(config).map_err(error)?,
        })
    }

    #[staticmethod]
    fn abi_version() -> u32 {
        CoreEngine::core_abi_version()
    }

    #[staticmethod]
    fn core_version() -> Option<String> {
        CoreEngine::core_version().map(|value| value.to_string_lossy().into_owned())
    }

    #[args(
        headers = "None",
        body = "None",
        timeout = "None",
        allow_redirects = "true",
        redirect = "None",
        cache = "None",
        priority = "None",
        chunked = "false"
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
