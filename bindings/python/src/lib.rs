use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc, Mutex,
};
use std::time::Duration;

use minicronet::{
    CloseInfo, Engine, EngineConfig, Header, MessageType, RedirectMode, RequestConfig,
    ResponseFuture, ResponseStream, Upload, WebSocketConfig, WebSocketEvent, WebSocketEvents,
};
use pyo3::{exceptions::PyRuntimeError, prelude::*};

fn native_error(error: impl std::fmt::Display) -> PyErr {
    PyRuntimeError::new_err(error.to_string())
}

type PyEvent = (String, i32, Vec<u8>, Vec<u8>, Option<String>);
type PyWebSocketEvent = (String, Vec<u8>, Option<u16>, Option<String>);

#[pyclass]
struct PyResponse {
    #[pyo3(get)]
    status_code: i32,
    headers: Vec<u8>,
    body: Vec<u8>,
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

#[pyclass]
struct PyWebSocket {
    inner: minicronet::WebSocket,
    events: Mutex<Option<WebSocketEvents>>,
    notify_scheduled: Arc<AtomicBool>,
}

fn websocket_event(event: WebSocketEvent) -> (String, Vec<u8>, Option<u16>, Option<String>) {
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
        Ok(events.try_next().map(websocket_event))
    }
}

#[pymethods]
impl PyRequest {
    fn send(&self, py: Python<'_>) -> PyResult<PyResponse> {
        py.detach(|| {
            let response = self
                .inner
                .start()
                .map_err(native_error)?
                .wait()
                .map_err(native_error)?;
            let mut body = Vec::new();
            let mut stream = response.body;
            while let Some(chunk) = stream.blocking_next() {
                body.extend_from_slice(&chunk.map_err(native_error)?);
            }
            Ok(PyResponse {
                status_code: response.status_code,
                headers: response.headers,
                body,
            })
        })
    }

    fn cancel(&self) -> PyResult<()> {
        let result = self.inner.cancel().map_err(native_error);
        self.inner.clear_event_callback();
        result
    }

    fn detach_callback(&self) {
        self.inner.clear_event_callback();
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

    /// Starts a request and arranges a thread-safe notification on an
    /// asyncio-compatible event loop. The callback only schedules `notify`; it
    /// never runs user Python code or waits for network I/O.
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
        let response_future = self.inner.start().map_err(native_error)?;
        *self
            .response_future
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner) = Some(response_future);
        Ok(())
    }

    /// Returns one non-blocking event as `(kind, status, headers, body, error)`.
    /// `kind` is `response`, `body`, `done`, or `error`; `None` means pending.
    fn poll_event(&self) -> PyResult<Option<PyEvent>> {
        self.notify_scheduled.store(false, Ordering::Release);

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
                    ("done".into(), 0, Vec::new(), Vec::new(), None)
                }
            }));
        }

        Ok(None)
    }
}

#[pyclass]
struct PyEngine {
    inner: Engine,
}

#[pymethods]
impl PyEngine {
    #[new]
    #[pyo3(signature = (impersonate=None, proxy=None, verify=true))]
    fn new(impersonate: Option<String>, proxy: Option<String>, verify: bool) -> PyResult<Self> {
        let profile_id = impersonate.map(|value| {
            if let Some(version) = value.strip_prefix("chrome") {
                if !version.is_empty()
                    && version.chars().all(|character| character.is_ascii_digit())
                {
                    return format!("chrome_{version}");
                }
            }
            value
        });
        let config = EngineConfig {
            profile_id,
            proxy_rules: proxy,
            tls_verify: if verify {
                minicronet::TlsVerifyMode::ChromiumDefault
            } else {
                minicronet::TlsVerifyMode::Insecure
            },
            ..EngineConfig::default()
        };
        Ok(Self {
            inner: Engine::new(config).map_err(native_error)?,
        })
    }

    #[pyo3(signature = (method, url, headers=None, body=None, timeout=None, allow_redirects=true))]
    fn request(
        &self,
        method: &str,
        url: &str,
        headers: Option<Vec<(String, String)>>,
        body: Option<Vec<u8>>,
        timeout: Option<f64>,
        allow_redirects: bool,
    ) -> PyResult<PyRequest> {
        let mut config = RequestConfig::get(url.to_owned());
        config.method = method.to_owned();
        config.timeout = timeout
            .map(|seconds| {
                if seconds.is_sign_negative() || !seconds.is_finite() {
                    return Err(native_error("timeout must be a finite non-negative number"));
                }
                Ok(Duration::from_secs_f64(seconds))
            })
            .transpose()?;
        config.redirect = if allow_redirects {
            RedirectMode::Follow
        } else {
            RedirectMode::Manual
        };
        config.headers = headers
            .unwrap_or_default()
            .into_iter()
            .map(|(name, value)| Header::new(name, value))
            .collect();
        if let Some(body) = body {
            config.upload = Upload::Fixed(body);
        }
        Ok(PyRequest {
            inner: self.inner.request(config).map_err(native_error)?,
            response_future: Mutex::new(None),
            body_stream: Mutex::new(None),
            notify_scheduled: Arc::new(AtomicBool::new(false)),
            done: AtomicBool::new(false),
        })
    }

    #[pyo3(signature = (url, origin="", headers=None, timeout=None))]
    fn websocket(
        &self,
        url: &str,
        origin: &str,
        headers: Option<Vec<(String, String)>>,
        timeout: Option<f64>,
    ) -> PyResult<PyWebSocket> {
        let mut config = WebSocketConfig::new(url, origin);
        config.headers = headers
            .unwrap_or_default()
            .into_iter()
            .map(|(name, value)| Header::new(name, value))
            .collect();
        config.timeout = timeout
            .map(|seconds| {
                if seconds.is_sign_negative() || !seconds.is_finite() {
                    return Err(native_error("timeout must be a finite non-negative number"));
                }
                Ok(Duration::from_secs_f64(seconds))
            })
            .transpose()?;
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
    Ok(())
}
