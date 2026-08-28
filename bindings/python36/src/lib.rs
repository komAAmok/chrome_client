//! Python 3.6 compatibility extension.
//!
//! This deliberately keeps the ABI-3 surface small.  The requests-shaped
//! facade is shared with the modern package; this module supplies the native
//! synchronous primitives using the last PyO3 release supporting Python 3.6.

use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc, Mutex,
};
use std::time::Duration;

use minicronet::{
    CloseInfo, Engine as CoreEngine, EngineConfig, Header, MessageType, RedirectMode,
    RequestConfig, ResponseFuture, ResponseStream, Upload, WebSocketConfig, WebSocketEvent,
    WebSocketEvents,
};
use pyo3::{exceptions::PyRuntimeError, prelude::*, types::PyBytes};

fn error(value: impl std::fmt::Display) -> PyErr {
    PyRuntimeError::new_err(value.to_string())
}

type PyEvent = (String, i32, Vec<u8>, Vec<u8>, Option<String>);
type PyWebSocketEvent = (String, Vec<u8>, Option<u16>, Option<String>);

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
        Ok(events.try_next().map(websocket_event))
    }
}

#[pymethods]
impl PyRequest {
    fn send(&self, py: Python) -> PyResult<PyResponse> {
        py.allow_threads(|| {
            let response = self.inner.start().map_err(error)?.wait().map_err(error)?;
            let status_code = response.status_code;
            let headers = response.headers;
            let mut content = Vec::new();
            let mut body = response.body;
            while let Some(chunk) = body.blocking_next() {
                content.extend_from_slice(&chunk.map_err(error)?);
            }
            Ok(PyResponse {
                status_code,
                headers,
                content,
            })
        })
    }

    fn cancel(&self) -> PyResult<()> {
        let result = self.inner.cancel().map_err(error);
        self.inner.clear_event_callback();
        result
    }

    fn detach_callback(&self) {
        self.inner.clear_event_callback();
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

    fn next_body(&self, py: Python) -> PyResult<Option<Vec<u8>>> {
        py.allow_threads(|| {
            let mut stream = self.body_stream.lock().unwrap();
            let stream = stream
                .as_mut()
                .ok_or_else(|| error("request body stream is not started"))?;
            stream.blocking_next().transpose().map_err(error)
        })
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
        let future = self.inner.start().map_err(error)?;
        *self.response_future.lock().unwrap() = Some(future);
        Ok(())
    }

    fn poll_event(&self) -> PyResult<Option<PyEvent>> {
        self.notify_scheduled.store(false, Ordering::Release);
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
                    self.done.store(true, Ordering::Release);
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
                    self.done.store(true, Ordering::Release);
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
                    ("done".into(), 0, Vec::new(), Vec::new(), None)
                }
            }));
        }
        Ok(None)
    }
}

#[pyclass]
struct PyEngine {
    inner: CoreEngine,
}

#[pymethods]
impl PyEngine {
    #[new]
    #[args(impersonate = "None", proxy = "None", verify = "true")]
    fn new(impersonate: Option<String>, proxy: Option<String>, verify: bool) -> PyResult<Self> {
        let profile_id = impersonate.map(|value| {
            value
                .strip_prefix("chrome")
                .filter(|v| !v.is_empty())
                .map_or(value.clone(), |v| format!("chrome_{v}"))
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
        Ok(PyEngine {
            inner: CoreEngine::new(config).map_err(error)?,
        })
    }

    #[args(
        headers = "None",
        body = "None",
        timeout = "None",
        allow_redirects = "true"
    )]
    fn request(
        &self,
        method: &str,
        url: &str,
        headers: Option<Vec<(String, String)>>,
        body: Option<Vec<u8>>,
        timeout: Option<f64>,
        allow_redirects: bool,
    ) -> PyResult<PyRequest> {
        let mut config = RequestConfig::get(url);
        config.method = method.to_owned();
        config.timeout = timeout
            .map(|seconds| {
                if !seconds.is_finite() || seconds < 0.0 {
                    return Err(error("timeout must be a finite non-negative number"));
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
            inner: self.inner.request(config).map_err(error)?,
            response_future: Mutex::new(None),
            body_stream: Mutex::new(None),
            notify_scheduled: Arc::new(AtomicBool::new(false)),
            done: AtomicBool::new(false),
        })
    }

    #[args(origin = "\"\"", headers = "None", timeout = "None")]
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
                if !seconds.is_finite() || seconds < 0.0 {
                    return Err(error("timeout must be a finite non-negative number"));
                }
                Ok(Duration::from_secs_f64(seconds))
            })
            .transpose()?;
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
    Ok(())
}
