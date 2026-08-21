use crate::cronet_c::*;
use crate::cronet_pb::proxy_config::ProxyType;
use crate::VERBOSE_MODE;
use std::collections::HashMap;
use std::ffi::{c_void, CStr, CString};
use std::ptr;
use std::sync::atomic::{AtomicBool, AtomicUsize, AtomicI32, Ordering};
use std::sync::{Arc, Mutex, OnceLock};
use tokio::sync::{oneshot, mpsc};

// Macro for verbose logging
macro_rules! verbose_log {
    ($($arg:tt)*) => {
        if VERBOSE_MODE.load(Ordering::Relaxed) {
            eprintln!($($arg)*);
        }
    };
}

// 安全地创建 CString，过滤掉 null 字节
fn safe_cstring(s: &str, context: &str) -> Result<CString, String> {
    // 移除 null 字节
    let safe_str = s.replace('\0', "");
    CString::new(safe_str).map_err(|e| {
        format!("Failed to create CString for {}: {}", context, e)
    })
}

// 验证 HTTP header name 是否合法 (RFC 7230 token)
// token = 1*tchar
// tchar = "!" / "#" / "$" / "%" / "&" / "'" / "*" / "+" / "-" / "." /
//         "^" / "_" / "`" / "|" / "~" / DIGIT / ALPHA
fn is_valid_header_name(name: &str) -> bool {
    if name.is_empty() {
        return false;
    }
    name.bytes().all(|b| matches!(b,
        b'!' | b'#' | b'$' | b'%' | b'&' | b'\'' | b'*' | b'+' | b'-' | b'.' |
        b'^' | b'_' | b'`' | b'|' | b'~' |
        b'0'..=b'9' | b'A'..=b'Z' | b'a'..=b'z'
    ))
}

// 验证 HTTP header value 是否合法（不含控制字符，除了水平制表符）
fn is_valid_header_value(value: &str) -> bool {
    value.bytes().all(|b| b == b'\t' || (b >= 0x20 && b != 0x7f))
}

// -----------------------------------------------------------------------------
// Cronet Engine
// -----------------------------------------------------------------------------

// Engine configuration key for caching
#[derive(Hash, Eq, PartialEq, Clone, Debug)]
struct EngineConfig {
    proxy_rules: Option<String>,
    skip_cert_verify: bool,
}

// Cached engine wrapper
struct CachedEngine {
    ptr: Cronet_EnginePtr,
}

unsafe impl Send for CachedEngine {}
unsafe impl Sync for CachedEngine {}

pub struct CronetEngine {
    ptr: Cronet_EnginePtr,
    // Cache of engines with custom configurations
    engine_cache: Mutex<HashMap<EngineConfig, CachedEngine>>,
}

impl CronetEngine {
    pub fn new(user_agent: &str) -> Self {
        unsafe {
            let engine_ptr = Cronet_Engine_Create();
            let params_ptr = Cronet_EngineParams_Create();

            // 安全地创建 CString
            let c_ua = match safe_cstring(user_agent, "user_agent") {
                Ok(s) => s,
                Err(e) => {
                    eprintln!("[ERROR] {}, using default", e);
                    CString::new("chrome_client/1.0").unwrap()
                }
            };
            Cronet_EngineParams_user_agent_set(params_ptr, c_ua.as_ptr());

            // Use true for params
            Cronet_EngineParams_enable_quic_set(params_ptr, true);
            Cronet_EngineParams_enable_http2_set(params_ptr, true);
            Cronet_EngineParams_enable_brotli_set(params_ptr, true);

            // Enable Cookie Store to handle Set-Cookie in 302 redirects
            let experimental_options = r#"{"enable_cookie_store":true}"#;
            let c_options = CString::new(experimental_options).expect("Invalid experimental options");
            Cronet_EngineParams_experimental_options_set(params_ptr, c_options.as_ptr());

            // Start the engine
            let res = Cronet_Engine_StartWithParams(engine_ptr, params_ptr);
            Cronet_EngineParams_Destroy(params_ptr);

            if res != Cronet_RESULT_Cronet_RESULT_SUCCESS {
                panic!("Failed to start Cronet Engine: {:?}", res);
            }

            CronetEngine {
                ptr: engine_ptr,
                engine_cache: Mutex::new(HashMap::new()),
            }
        }
    }

    // Get or create a cached engine with custom configuration
    fn get_or_create_engine(&self, config_key: &EngineConfig) -> Option<Cronet_EnginePtr> {
        let mut cache = self
            .engine_cache
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());

        if let Some(cached) = cache.get(config_key) {
            verbose_log!("[DEBUG] Reusing cached engine for config: {:?}", config_key);
            return Some(cached.ptr);
        }

        verbose_log!("[DEBUG] Creating new engine for config: {:?}", config_key);
        unsafe {
            let engine = Cronet_Engine_Create();
            let params = Cronet_EngineParams_Create();

            // Configure proxy if present
            if let Some(ref proxy_rules) = config_key.proxy_rules {
                let c_rules = CString::new(proxy_rules.as_str()).expect("Invalid proxy string");
                Cronet_EngineParams_proxy_rules_set(params, c_rules.as_ptr());
            }

            Cronet_EngineParams_enable_quic_set(params, true);
            Cronet_EngineParams_enable_http2_set(params, true);

            // Skip certificate verification if requested
            if config_key.skip_cert_verify {
                Cronet_EngineParams_skip_cert_verify_set(params, true);
            }

            // Enable Cookie Store to handle Set-Cookie in 302 redirects
            let experimental_options = r#"{"enable_cookie_store":true}"#;
            let c_options = CString::new(experimental_options).expect("Invalid experimental options");
            Cronet_EngineParams_experimental_options_set(params, c_options.as_ptr());

            let result = Cronet_Engine_StartWithParams(engine, params);
            Cronet_EngineParams_Destroy(params);

            if result != Cronet_RESULT_Cronet_RESULT_SUCCESS {
                eprintln!(
                    "[ERROR] Failed to start cached Cronet engine for {:?}: {:?}",
                    config_key, result
                );
                Cronet_Engine_Destroy(engine);
                return None;
            }

            cache.insert(config_key.clone(), CachedEngine { ptr: engine });
            Some(engine)
        }
    }

    pub fn start_request(
        &self,
        target: &crate::cronet_pb::TargetRequest,
        config: &crate::cronet_pb::ExecutionConfig,
    ) -> (
        CronetRequest,
        oneshot::Receiver<Result<RequestResult, String>>,
    ) {
        unsafe {
            verbose_log!("[DEBUG] start_request entered");
            // Determine Engine to use (Shared or Cached Engine with custom config)
            let needs_custom_engine = config.proxy.is_some() || config.skip_cert_verify;
            let engine_ptr = if needs_custom_engine {
                // Build proxy rules string if proxy is configured
                let proxy_rules = if let Some(proxy) = &config.proxy {
                    let scheme = match ProxyType::try_from(proxy.r#type).unwrap_or(ProxyType::Http) {
                        ProxyType::Http => "http",
                        ProxyType::Https => "https",
                        ProxyType::Socks5 => "socks5",
                    };

                    let rules = if !proxy.username.is_empty() && !proxy.password.is_empty() {
                        format!(
                            "{}://{}:{}@{}:{}",
                            scheme, proxy.username, proxy.password, proxy.host, proxy.port
                        )
                    } else {
                        format!("{}://{}:{}", scheme, proxy.host, proxy.port)
                    };
                    Some(rules)
                } else {
                    None
                };

                let config_key = EngineConfig {
                    proxy_rules,
                    skip_cert_verify: config.skip_cert_verify,
                };

                // Use cached engine (session is preserved)
                self.get_or_create_engine(&config_key).unwrap_or_else(|| {
                    eprintln!("[WARN] Falling back to the shared Cronet engine after custom engine startup failure");
                    self.ptr
                })
            } else {
                self.ptr
            };
            // owned_engine_ptr is no longer needed since we cache engines
            let owned_engine_ptr: Option<Cronet_EnginePtr> = None;

            // Channel to receive the final result
            let (tx, rx) = oneshot::channel();

            // 创建完成标志，用于追踪请求是否已完成
            let completed = Arc::new(AtomicBool::new(false));
            let callback_claimed = Arc::new(AtomicBool::new(false));
            let callback_returned = Arc::new(AtomicBool::new(false));

            // Create Context to hold state across callbacks
            let context = Box::new(RequestContext {
                tx: Mutex::new(Some(tx)),
                response_buffer: Mutex::new(Vec::new()),
                response_headers: Mutex::new(Vec::new()),
                status_code: AtomicI32::new(0),
                completed: completed.clone(),
                callback_claimed: callback_claimed.clone(),
                callback_returned: callback_returned.clone(),
                active_requests: None, // CronetEngine does not track active requests.
                allow_redirects: true,
                redirect_response: Mutex::new(None),
                context_taken: AtomicBool::new(false),
                is_streaming: false,
                stream_tx: Mutex::new(None),
            });
            let context_ptr = Box::into_raw(context);

            // 复用引擎共享的 executor 线程（避免每个请求创建新线程）
            let executor_context = Box::new(ExecutorContext {
                in_flight_executors: None,  // CronetEngine 不使用 in-flight 计数
            });
            let executor_context_ptr = Box::into_raw(executor_context);

            // Executor
            // We use the same executor for request and upload
            let executor_ptr = Cronet_Executor_CreateWith(Some(executor_execute));
            Cronet_Executor_SetClientContext(executor_ptr, executor_context_ptr as *mut c_void);

            // Callback
            let callback_ptr = Cronet_UrlRequestCallback_CreateWith(
                Some(on_redirect_received),
                Some(on_response_started),
                Some(on_read_completed),
                Some(on_succeeded),
                Some(on_failed),
                Some(on_canceled),
            );
            Cronet_UrlRequestCallback_SetClientContext(callback_ptr, context_ptr as *mut c_void);

            // Request & Params
            let request_ptr = Cronet_UrlRequest_Create();
            let params_ptr = Cronet_UrlRequestParams_Create();

            let c_method = CString::new(target.method.as_str()).unwrap();
            Cronet_UrlRequestParams_http_method_set(params_ptr, c_method.as_ptr());

            // Set highest priority to get HTTP/2 weight=256 (same as normal browsers)
            Cronet_UrlRequestParams_priority_set(
                params_ptr,
                4  // REQUEST_PRIORITY_HIGHEST
            );

            let c_url = CString::new(target.url.as_str()).unwrap();

            // Headers - 按顺序添加（跳过无效的 header name/value）
            for header in &target.headers {
                if !is_valid_header_name(&header.name) {
                    eprintln!("[WARN] Skipping header with invalid name: {:?}", header.name);
                    continue;
                }
                if !is_valid_header_value(&header.value) {
                    eprintln!("[WARN] Skipping header with invalid value for key {:?}", header.name);
                    continue;
                }
                let c_key = CString::new(header.name.as_str()).unwrap();
                let c_val = CString::new(header.value.as_str()).unwrap();

                let header_ptr = Cronet_HttpHeader_Create();
                Cronet_HttpHeader_name_set(header_ptr, c_key.as_ptr());
                Cronet_HttpHeader_value_set(header_ptr, c_val.as_ptr());

                Cronet_UrlRequestParams_request_headers_add(params_ptr, header_ptr);

                Cronet_HttpHeader_Destroy(header_ptr);
            }

            // Upload Data Provider (Body)
            let mut upload_data_provider_ptr: Option<Cronet_UploadDataProviderPtr> = None;

            // Keep body alive
            let upload_body_data = if !target.body.is_empty() {
                Some(target.body.clone())
            } else {
                None
            };

            if let Some(body) = &upload_body_data {
                eprintln!(
                    "[DEBUG] Creating Rust UploadDataProvider. Body len: {}",
                    body.len()
                );

                let upload_context = Box::new(UploadContext {
                    data: body.clone(),
                    position: 0,
                });
                let upload_context_ptr = Box::into_raw(upload_context);

                let provider = Cronet_UploadDataProvider_CreateWith(
                    Some(upload_get_length),
                    Some(upload_read),
                    Some(upload_rewind),
                    Some(upload_close),
                );
                Cronet_UploadDataProvider_SetClientContext(
                    provider,
                    upload_context_ptr as *mut c_void,
                );

                Cronet_UrlRequestParams_upload_data_provider_set(params_ptr, provider);
                Cronet_UrlRequestParams_upload_data_provider_executor_set(params_ptr, executor_ptr);

                upload_data_provider_ptr = Some(provider);
            }

            Cronet_UrlRequest_InitWithParams(
                request_ptr,
                engine_ptr,
                c_url.as_ptr(),
                params_ptr,
                callback_ptr,
                executor_ptr,
            );

            Cronet_UrlRequestParams_Destroy(params_ptr);

            // Start
            verbose_log!("[DEBUG] Starting Cronet Request");
            Cronet_UrlRequest_Start(request_ptr);

            // Return Handle that owns the cleanup
            let request_handle = CronetRequest {
                ptr: request_ptr,
                callback_ptr,
                executor_ptr,
                executor_context_ptr,
                owned_engine_ptr,
                upload_data_provider_ptr,
                upload_body_data,
                completed,
                callback_claimed,
                callback_returned,
            };

            (request_handle, rx)
        }
    }
}

impl Drop for CronetEngine {
    fn drop(&mut self) {
        // NOTE: CronetEngine 由 Arc<CronetEngine> 持有（AppState），仅在进程退出时销毁。
        // 此时 OS 会回收所有资源，因此即使有活跃请求也不会导致实际问题。
        // 如果未来 CronetEngine 可能在运行时被销毁，需要添加类似 Session::drop 的
        // active_requests 等待 + 超时泄漏逻辑。
        unsafe {
            // Clean up cached engines
            let cache = self
                .engine_cache
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner());
            for (_, cached) in cache.iter() {
                Cronet_Engine_Shutdown(cached.ptr);
                Cronet_Engine_Destroy(cached.ptr);
            }
            drop(cache);

            // Clean up main engine
            Cronet_Engine_Shutdown(self.ptr);
            Cronet_Engine_Destroy(self.ptr);
        }
    }
}

unsafe impl Send for CronetEngine {}
unsafe impl Sync for CronetEngine {}

// -----------------------------------------------------------------------------
// Request Infrastructure
// -----------------------------------------------------------------------------

#[derive(Debug)]
pub struct RequestResult {
    pub status_code: i32,
    pub headers: Vec<(String, String)>,
    pub body: Vec<u8>,
}

/// 流式响应数据块
#[derive(Debug)]
pub enum StreamChunk {
    /// 响应头（首个块）
    Headers {
        status_code: i32,
        headers: Vec<(String, String)>,
    },
    /// 响应体数据块
    Data(Vec<u8>),
    /// 请求完成
    Done,
    /// 错误
    Error(String),
}

// 64 x 32 KiB Cronet buffers cap queued response data at roughly 2 MiB per
// stream. Native callbacks apply backpressure instead of allowing unbounded
// growth when Python/C consumers are slower than the network.
const STREAM_CHANNEL_CAPACITY: usize = 64;

fn send_stream_chunk(tx: &mpsc::Sender<StreamChunk>, chunk: StreamChunk) {
    use tokio::sync::mpsc::error::TrySendError;

    let chunk = match tx.try_send(chunk) {
        Ok(()) | Err(TrySendError::Closed(_)) => return,
        Err(TrySendError::Full(chunk)) => chunk,
    };
    if tokio::runtime::Handle::try_current().is_ok() {
        let _ = tokio::task::block_in_place(|| tx.blocking_send(chunk));
    } else {
        let _ = tx.blocking_send(chunk);
    }
}

#[allow(dead_code)]
pub struct CronetRequest {
    ptr: Cronet_UrlRequestPtr,
    callback_ptr: Cronet_UrlRequestCallbackPtr,
    executor_ptr: Cronet_ExecutorPtr,
    executor_context_ptr: *mut ExecutorContext,  // Executor 的独立 context
    owned_engine_ptr: Option<Cronet_EnginePtr>,
    upload_data_provider_ptr: Option<Cronet_UploadDataProviderPtr>,
    upload_body_data: Option<Vec<u8>>, // Owns the body data so pointers are valid
    completed: Arc<AtomicBool>,  // 标记请求是否完成，由回调设置
    // Set as soon as a terminal callback claims the context.  The response
    // channel may wake its consumer before that callback returns; in that
    // window Drop must not call Cancel concurrently with on_failed/on_canceled.
    callback_claimed: Arc<AtomicBool>,
    // The terminal callback may publish its response before returning to
    // Cronet. Native callback objects are destroyed only after it returns.
    callback_returned: Arc<AtomicBool>,
}

unsafe impl Send for CronetRequest {}

struct DeferredRequestCleanup {
    ptr: Cronet_UrlRequestPtr,
    callback_ptr: Cronet_UrlRequestCallbackPtr,
    executor_ptr: Cronet_ExecutorPtr,
    executor_context_ptr: *mut ExecutorContext,
    owned_engine_ptr: Option<Cronet_EnginePtr>,
    upload_data_provider_ptr: Option<Cronet_UploadDataProviderPtr>,
    upload_body_data: Option<Vec<u8>>,
    completed: Arc<AtomicBool>,
    callback_returned: Arc<AtomicBool>,
    callback_claimed: Arc<AtomicBool>,
    queued_at: std::time::Instant,
    cancel_requested: Arc<AtomicBool>,
}

unsafe impl Send for DeferredRequestCleanup {}

impl DeferredRequestCleanup {
    unsafe fn destroy(self) {
        if !self.ptr.is_null() {
            Cronet_UrlRequest_Destroy(self.ptr);
        }
        if !self.callback_ptr.is_null() {
            Cronet_UrlRequestCallback_Destroy(self.callback_ptr);
        }
        if !self.executor_ptr.is_null() {
            Cronet_Executor_Destroy(self.executor_ptr);
        }
        if !self.executor_context_ptr.is_null() {
            let _ = Box::from_raw(self.executor_context_ptr);
        }
        if let Some(provider) = self.upload_data_provider_ptr {
            Cronet_UploadDataProvider_Destroy(provider);
        }
        drop(self.upload_body_data);
        if let Some(engine_ptr) = self.owned_engine_ptr {
            Cronet_Engine_Shutdown(engine_ptr);
            Cronet_Engine_Destroy(engine_ptr);
        }
    }
}

enum DeferredCleanup {
    Request(DeferredRequestCleanup),
    Engine {
        ptr: Cronet_EnginePtr,
        active_requests: Arc<AtomicUsize>,
    },
}

unsafe impl Send for DeferredCleanup {}

impl DeferredCleanup {
    fn is_ready(&self) -> bool {
        match self {
            Self::Request(cleanup) => {
                cleanup.completed.load(Ordering::Acquire)
                    && cleanup.callback_returned.load(Ordering::Acquire)
            }
            Self::Engine {
                active_requests, ..
            } => active_requests.load(Ordering::Acquire) == 0,
        }
    }

    unsafe fn destroy(self) {
        match self {
            Self::Request(cleanup) => cleanup.destroy(),
            Self::Engine { ptr, .. } => {
                Cronet_Engine_Shutdown(ptr);
                Cronet_Engine_Destroy(ptr);
            }
        }
    }

    unsafe fn request_cancel_if_stale(&self) {
        let Self::Request(cleanup) = self else { return };
        if cleanup.queued_at.elapsed() < std::time::Duration::from_secs(5)
            || cleanup.cancel_requested.swap(true, Ordering::AcqRel)
            || cleanup.callback_claimed.load(Ordering::Acquire)
            || cleanup.ptr.is_null()
        {
            return;
        }
        // Keep the native objects alive and ask Cronet to deliver its normal
        // terminal callback. We still never destroy them before that callback
        // returns.
        Cronet_UrlRequest_Cancel(cleanup.ptr);
    }
}

fn defer_cleanup(cleanup: DeferredCleanup) {
    static REAPER: OnceLock<std::sync::mpsc::Sender<DeferredCleanup>> = OnceLock::new();

    let tx = REAPER.get_or_init(|| {
        let (tx, rx) = std::sync::mpsc::channel::<DeferredCleanup>();
        std::thread::Builder::new()
            .name("cronet-request-reaper".to_string())
            .spawn(move || {
                let mut pending = Vec::new();
                loop {
                    match rx.recv_timeout(std::time::Duration::from_millis(100)) {
                        Ok(cleanup) => pending.push(cleanup),
                        Err(std::sync::mpsc::RecvTimeoutError::Timeout) => {}
                        Err(std::sync::mpsc::RecvTimeoutError::Disconnected) => break,
                    }

                    let mut index = 0;
                    while index < pending.len() {
                        unsafe { pending[index].request_cancel_if_stale() };
                        if pending[index].is_ready() {
                            let cleanup = pending.swap_remove(index);
                            unsafe { cleanup.destroy() };
                        } else {
                            index += 1;
                        }
                    }
                }
            })
            .expect("failed to start Cronet request cleanup thread");
        tx
    });

    if let Err(error) = tx.send(cleanup) {
        // The process is shutting down and the reaper is gone. Keeping these
        // pointers alive is safer than freeing memory still owned by Cronet.
        std::mem::forget(error.0);
    }
}

fn defer_request_cleanup(cleanup: DeferredRequestCleanup) {
    defer_cleanup(DeferredCleanup::Request(cleanup));
}

fn defer_engine_cleanup(ptr: Cronet_EnginePtr, active_requests: Arc<AtomicUsize>) {
    defer_cleanup(DeferredCleanup::Engine {
        ptr,
        active_requests,
    });
}

struct RequestCompletionGuard {
    completed: Arc<AtomicBool>,
    active_requests: Option<Arc<AtomicUsize>>,
}

impl Drop for RequestCompletionGuard {
    fn drop(&mut self) {
        if let Some(counter) = &self.active_requests {
            counter.fetch_sub(1, Ordering::Release);
        }
        self.completed.store(true, Ordering::Release);
    }
}

// A terminal callback can send the oneshot/stream event before returning to
// Cronet.  Keep native callback objects alive until the C callback has fully
// unwound, including early-return paths.
struct CallbackReturnGuard(Arc<AtomicBool>);

impl Drop for CallbackReturnGuard {
    fn drop(&mut self) {
        self.0.store(true, Ordering::Release);
    }
}

impl CronetRequest {
    fn take_cleanup(&mut self) -> DeferredRequestCleanup {
        DeferredRequestCleanup {
            ptr: std::mem::replace(&mut self.ptr, std::ptr::null_mut()),
            callback_ptr: std::mem::replace(&mut self.callback_ptr, std::ptr::null_mut()),
            executor_ptr: std::mem::replace(&mut self.executor_ptr, std::ptr::null_mut()),
            executor_context_ptr: std::mem::replace(
                &mut self.executor_context_ptr,
                std::ptr::null_mut(),
            ),
            owned_engine_ptr: self.owned_engine_ptr.take(),
            upload_data_provider_ptr: self.upload_data_provider_ptr.take(),
            upload_body_data: self.upload_body_data.take(),
            completed: self.completed.clone(),
            callback_returned: self.callback_returned.clone(),
            callback_claimed: self.callback_claimed.clone(),
            queued_at: std::time::Instant::now(),
            cancel_requested: Arc::new(AtomicBool::new(false)),
        }
    }
}

fn should_cancel_request(callback_claimed: bool, native_done: bool) -> bool {
    !callback_claimed && !native_done
}

impl Drop for CronetRequest {
    fn drop(&mut self) {
        unsafe {
            // 检查请求是否已完成
            if !self.completed.load(Ordering::Acquire) {
                // 请求尚未完成，先取消它
                verbose_log!("[DEBUG] CronetRequest::drop - Request not completed; checking cancellation state");
                let callback_claimed = self.callback_claimed.load(Ordering::Acquire);
                let native_done = !self.ptr.is_null() && Cronet_UrlRequest_IsDone(self.ptr);
                if !self.ptr.is_null()
                    && should_cancel_request(callback_claimed, native_done)
                {
                    // Cronet marks a failed request done before dispatching
                    // on_failed. Do not race that callback with Cancel.
                    Cronet_UrlRequest_Cancel(self.ptr);
                } else if callback_claimed || native_done {
                    verbose_log!("[DEBUG] CronetRequest::drop - terminal callback is in progress; waiting without Cancel");
                }
                // 等待请求完成（最多等待 5 秒）
                let start = std::time::Instant::now();
                while !self.completed.load(Ordering::Acquire) {
                    if start.elapsed() > std::time::Duration::from_secs(5) {
                        eprintln!("[WARN] CronetRequest::drop - Cancel callback is delayed; deferring native cleanup");
                        // Cronet may still own the callback pointers. The reaper
                        // frees them only after the callback has fully returned.
                        defer_request_cleanup(self.take_cleanup());
                        return;
                    }
                    std::thread::sleep(std::time::Duration::from_millis(10));
                }
            }

            // completed == true，可以安全销毁
            // The receiver may wake before the terminal callback returns.
            // Keep native callback/request objects alive until it unwinds.
            let cleanup = self.take_cleanup();
            if cleanup.callback_returned.load(Ordering::Acquire) {
                cleanup.destroy();
            } else {
                defer_request_cleanup(cleanup);
            }
        }
    }
}

// Context passed to C callbacks
struct RequestContext {
    tx: Mutex<Option<oneshot::Sender<Result<RequestResult, String>>>>,
    response_buffer: Mutex<Vec<u8>>,
    response_headers: Mutex<Vec<(String, String)>>,
    status_code: AtomicI32,
    completed: Arc<AtomicBool>,  // 标记请求是否完成
    callback_claimed: Arc<AtomicBool>,
    callback_returned: Arc<AtomicBool>,
    active_requests: Option<Arc<AtomicUsize>>,  // Session 的活跃请求计数器
    allow_redirects: bool,  // 是否允许重定向（只读，不需要锁）
    redirect_response: Mutex<Option<RequestResult>>,  // 存储重定向响应（当 allow_redirects=false 时）
    context_taken: AtomicBool,  // 防止双重释放：标记 context 是否已被取走
    // 流式响应
    is_streaming: bool,
    stream_tx: Mutex<Option<mpsc::Sender<StreamChunk>>>,
}

// Executor 专用 context - 独立于 RequestContext，避免 use-after-free
struct ExecutorContext {
    in_flight_executors: Option<Arc<AtomicUsize>>,
}

// -----------------------------------------------------------------------------
// C Callbacks (Extern "C")
// -----------------------------------------------------------------------------

unsafe extern "C" fn executor_execute(_self: Cronet_ExecutorPtr, command: Cronet_RunnablePtr) {
    // 同步执行，避免线程调度导致的竞态条件
    // 这会牺牲一些性能，但能保证线程安全
    Cronet_Runnable_Run(command);
    Cronet_Runnable_Destroy(command);
}

// UrlRequest Callbacks
unsafe extern "C" fn on_redirect_received(
    self_: Cronet_UrlRequestCallbackPtr,
    request: Cronet_UrlRequestPtr,
    info: Cronet_UrlResponseInfoPtr,
    _new_location_url: Cronet_String,
) {
    // 获取 RequestContext 检查是否允许重定向
    let context_ptr = Cronet_UrlRequestCallback_GetClientContext(self_) as *mut RequestContext;
    let context = &*context_ptr;

    // 获取响应头（无论是否允许重定向，都需要提取 Set-Cookie）
    let mut headers = Vec::new();
    let header_count = Cronet_UrlResponseInfo_all_headers_list_size(info);
    for i in 0..header_count {
        let header_ptr = Cronet_UrlResponseInfo_all_headers_list_at(info, i);
        if !header_ptr.is_null() {
            let name_ptr = Cronet_HttpHeader_name_get(header_ptr);
            let value_ptr = Cronet_HttpHeader_value_get(header_ptr);

            if !name_ptr.is_null() && !value_ptr.is_null() {
                let name = CStr::from_ptr(name_ptr).to_string_lossy().to_string();
                let value = CStr::from_ptr(value_ptr).to_string_lossy().to_string();
                headers.push((name, value));
            }
        }
    }

    if context.allow_redirects {
        // 允许重定向：将重定向响应头追加到 response_headers（用于提取 Set-Cookie）
        match context.response_headers.lock() {
            Ok(mut response_headers) => {
                response_headers.extend(headers);
            }
            Err(poisoned) => {
                eprintln!("[WARN] on_redirect_received: response_headers mutex poisoned, recovering");
                let mut response_headers = poisoned.into_inner();
                response_headers.extend(headers);
            }
        }
        Cronet_UrlRequest_FollowRedirect(request);
    } else {
        // 不允许重定向，保存重定向响应信息然后取消请求
        let status_code = Cronet_UrlResponseInfo_http_status_code_get(info);

        // 保存重定向响应（使用锁保护，处理 poisoned）
        match context.redirect_response.lock() {
            Ok(mut redirect_response) => {
                *redirect_response = Some(RequestResult {
                    status_code,
                    headers,
                    body: Vec::new(), // 重定向响应通常没有 body
                });
            }
            Err(poisoned) => {
                eprintln!("[WARN] on_redirect_received: Mutex poisoned, recovering");
                let mut redirect_response = poisoned.into_inner();
                *redirect_response = Some(RequestResult {
                    status_code,
                    headers,
                    body: Vec::new(),
                });
            }
        }

        // 取消请求，on_canceled 会检查 redirect_response 并发送它
        Cronet_UrlRequest_Cancel(request);
    }
}

unsafe extern "C" fn on_response_started(
    self_: Cronet_UrlRequestCallbackPtr,
    request: Cronet_UrlRequestPtr,
    info: Cronet_UrlResponseInfoPtr,
) {
    verbose_log!("[DEBUG] on_response_started");
    let context_ptr = Cronet_UrlRequestCallback_GetClientContext(self_) as *mut RequestContext;
    let context = &*context_ptr;

    let status_code = Cronet_UrlResponseInfo_http_status_code_get(info);
    context.status_code.store(status_code, Ordering::Release);

    // 提取响应 headers（使用锁保护，处理 poisoned）
    match context.response_headers.lock() {
        Ok(mut response_headers) => {
            let header_count = Cronet_UrlResponseInfo_all_headers_list_size(info);
            for i in 0..header_count {
                let header_ptr = Cronet_UrlResponseInfo_all_headers_list_at(info, i);
                if !header_ptr.is_null() {
                    let name_ptr = Cronet_HttpHeader_name_get(header_ptr);
                    let value_ptr = Cronet_HttpHeader_value_get(header_ptr);
                    if !name_ptr.is_null() && !value_ptr.is_null() {
                        let name = CStr::from_ptr(name_ptr).to_string_lossy().into_owned();
                        let value = CStr::from_ptr(value_ptr).to_string_lossy().into_owned();
                        response_headers.push((name, value));
                    }
                }
            }
        }
        Err(poisoned) => {
            eprintln!("[WARN] on_response_started: Mutex poisoned, recovering");
            let mut response_headers = poisoned.into_inner();
            let header_count = Cronet_UrlResponseInfo_all_headers_list_size(info);
            for i in 0..header_count {
                let header_ptr = Cronet_UrlResponseInfo_all_headers_list_at(info, i);
                if !header_ptr.is_null() {
                    let name_ptr = Cronet_HttpHeader_name_get(header_ptr);
                    let value_ptr = Cronet_HttpHeader_value_get(header_ptr);
                    if !name_ptr.is_null() && !value_ptr.is_null() {
                        let name = CStr::from_ptr(name_ptr).to_string_lossy().into_owned();
                        let value = CStr::from_ptr(value_ptr).to_string_lossy().into_owned();
                        response_headers.push((name, value));
                    }
                }
            }
        }
    }

    // 流式模式：发送 Headers 块
    if context.is_streaming {
        let status_code = context.status_code.load(Ordering::Acquire);
        let headers = match context.response_headers.lock() {
            Ok(guard) => guard.clone(),
            Err(poisoned) => {
                eprintln!("[WARN] on_response_started: response_headers mutex poisoned for streaming");
                poisoned.into_inner().clone()
            }
        };
        let stream_tx = context
            .stream_tx
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .clone();
        if let Some(tx) = stream_tx {
            send_stream_chunk(&tx, StreamChunk::Headers { status_code, headers });
        }
    }

    let buffer_ptr = Cronet_Buffer_Create();
    Cronet_Buffer_InitWithAlloc(buffer_ptr, 32 * 1024);

    Cronet_UrlRequest_Read(request, buffer_ptr);
}

unsafe extern "C" fn on_read_completed(
    self_: Cronet_UrlRequestCallbackPtr,
    request: Cronet_UrlRequestPtr,
    _info: Cronet_UrlResponseInfoPtr,
    buffer: Cronet_BufferPtr,
    bytes_read: u64,
) {
    verbose_log!("[DEBUG] on_read_completed: {} bytes", bytes_read);
    let context_ptr = Cronet_UrlRequestCallback_GetClientContext(self_) as *mut RequestContext;
    let context = &*context_ptr;

    let data_ptr = Cronet_Buffer_GetData(buffer);
    let slice = std::slice::from_raw_parts(data_ptr as *const u8, bytes_read as usize);

    if context.is_streaming {
        // 流式模式：直接发送数据块
        let stream_tx = context
            .stream_tx
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .clone();
        if let Some(tx) = stream_tx {
            send_stream_chunk(&tx, StreamChunk::Data(slice.to_vec()));
        }
    } else {
        // 非流式模式：缓冲数据
        match context.response_buffer.lock() {
            Ok(mut response_buffer) => {
                response_buffer.extend_from_slice(slice);
            }
            Err(poisoned) => {
                eprintln!("[WARN] on_read_completed: Mutex poisoned, recovering");
                let mut response_buffer = poisoned.into_inner();
                response_buffer.extend_from_slice(slice);
            }
        }
    }

    Cronet_Buffer_Destroy(buffer);

    let new_buffer = Cronet_Buffer_Create();
    Cronet_Buffer_InitWithAlloc(new_buffer, 32 * 1024);

    Cronet_UrlRequest_Read(request, new_buffer);
}

unsafe extern "C" fn on_succeeded(
    self_: Cronet_UrlRequestCallbackPtr,
    _request: Cronet_UrlRequestPtr,
    _info: Cronet_UrlResponseInfoPtr,
) {
    verbose_log!("[DEBUG] on_succeeded");
    let context_ptr = Cronet_UrlRequestCallback_GetClientContext(self_) as *mut RequestContext;
    let _callback_returned = CallbackReturnGuard((&*context_ptr).callback_returned.clone());
    complete_request(self_, Ok(()));
}

unsafe extern "C" fn on_failed(
    self_: Cronet_UrlRequestCallbackPtr,
    _request: Cronet_UrlRequestPtr,
    _info: Cronet_UrlResponseInfoPtr,
    error: Cronet_ErrorPtr,
) {
    verbose_log!("[DEBUG] on_failed");
    let context_ptr = Cronet_UrlRequestCallback_GetClientContext(self_) as *mut RequestContext;
    let _callback_returned = CallbackReturnGuard((&*context_ptr).callback_returned.clone());
    let msg = CStr::from_ptr(Cronet_Error_message_get(error))
        .to_string_lossy()
        .into_owned();
    complete_request(self_, Err(msg));
}

unsafe extern "C" fn on_canceled(
    self_: Cronet_UrlRequestCallbackPtr,
    _request: Cronet_UrlRequestPtr,
    _info: Cronet_UrlResponseInfoPtr,
) {
    verbose_log!("[DEBUG] on_canceled");

    let context_ptr = Cronet_UrlRequestCallback_GetClientContext(self_) as *mut RequestContext;
    let _callback_returned = CallbackReturnGuard((&*context_ptr).callback_returned.clone());

    // 检查 context 是否已被取走，防止双重释放
    let context_ref = &*context_ptr;
    context_ref.callback_claimed.store(true, Ordering::Release);
    if context_ref.context_taken.swap(true, Ordering::AcqRel) {
        verbose_log!("[WARN] on_canceled: Context already taken, skipping");
        return;
    }

    let _completion = RequestCompletionGuard {
        completed: context_ref.completed.clone(),
        active_requests: context_ref.active_requests.clone(),
    };
    let context = Box::from_raw(context_ptr);

    // Completion is published by _completion after context cleanup finishes.

    // 流式模式：发送错误或重定向响应
    if context.is_streaming {
        let redirect_response = match context.redirect_response.lock() {
            Ok(mut guard) => guard.take(),
            Err(poisoned) => poisoned.into_inner().take(),
        };
        let stream_tx = match context.stream_tx.lock() {
            Ok(mut guard) => guard.take(),
            Err(poisoned) => poisoned.into_inner().take(),
        };
        if let Some(tx) = stream_tx {
            if let Some(redirect) = redirect_response {
                // 重定向响应：发送 Headers 然后 Done
                send_stream_chunk(&tx, StreamChunk::Headers {
                    status_code: redirect.status_code,
                    headers: redirect.headers,
                });
                send_stream_chunk(&tx, StreamChunk::Done);
            } else {
                send_stream_chunk(&tx, StreamChunk::Error("Canceled".to_string()));
            }
        }
        return;
    }

    // 检查是否有保存的重定向响应（allow_redirects=false 的情况）
    let redirect_response = match context.redirect_response.lock() {
        Ok(mut guard) => guard.take(),
        Err(poisoned) => {
            eprintln!("[WARN] on_canceled: redirect_response mutex poisoned, recovering");
            poisoned.into_inner().take()
        }
    };

    if let Some(redirect_response) = redirect_response {
        verbose_log!("[DEBUG] on_canceled: Sending redirect response (status {})", redirect_response.status_code);
        let tx = match context.tx.lock() {
            Ok(mut guard) => guard.take(),
            Err(poisoned) => {
                eprintln!("[WARN] on_canceled: tx mutex poisoned, recovering");
                poisoned.into_inner().take()
            }
        };
        if let Some(tx) = tx {
            let _ = tx.send(Ok(redirect_response));
        }
    } else {
        // 正常的取消，发送错误
        let tx = match context.tx.lock() {
            Ok(mut guard) => guard.take(),
            Err(poisoned) => {
                eprintln!("[WARN] on_canceled: tx mutex poisoned, recovering");
                poisoned.into_inner().take()
            }
        };
        if let Some(tx) = tx {
            let _ = tx.send(Err("Canceled".to_string()));
        }
    }
}

unsafe fn complete_request(callback_ptr: Cronet_UrlRequestCallbackPtr, result: Result<(), String>) {
    let context_ptr =
        Cronet_UrlRequestCallback_GetClientContext(callback_ptr) as *mut RequestContext;

    // 检查 context 是否已被取走，防止双重释放
    let context_ref = &*context_ptr;
    context_ref.callback_claimed.store(true, Ordering::Release);
    if context_ref.context_taken.swap(true, Ordering::AcqRel) {
        verbose_log!("[WARN] complete_request: Context already taken, skipping");
        return;
    }

    let _completion = RequestCompletionGuard {
        completed: context_ref.completed.clone(),
        active_requests: context_ref.active_requests.clone(),
    };
    // Declared after the guard so context drops before completion is published.
    let context = Box::from_raw(context_ptr);

    // Completion is published by _completion after context cleanup finishes.

    verbose_log!("[DEBUG] complete_request: {:?}", result);

    // 流式模式：发送 Done 或 Error
    if context.is_streaming {
        let stream_tx = match context.stream_tx.lock() {
            Ok(mut guard) => guard.take(),
            Err(poisoned) => poisoned.into_inner().take(),
        };
        if let Some(tx) = stream_tx {
            match result {
                Ok(_) => send_stream_chunk(&tx, StreamChunk::Done),
                Err(e) => send_stream_chunk(&tx, StreamChunk::Error(e)),
            }
        }
        return;
    }

    let tx = match context.tx.lock() {
        Ok(mut guard) => guard.take(),
        Err(poisoned) => {
            eprintln!("[WARN] complete_request: tx mutex poisoned, recovering");
            poisoned.into_inner().take()
        }
    };

    if let Some(tx) = tx {
        match result {
            Ok(_) => {
                let status_code = context.status_code.load(Ordering::Acquire);

                let headers = match context.response_headers.lock() {
                    Ok(guard) => guard.clone(),
                    Err(poisoned) => {
                        eprintln!("[WARN] complete_request: response_headers mutex poisoned, recovering");
                        poisoned.into_inner().clone()
                    }
                };

                let body = match context.response_buffer.lock() {
                    Ok(guard) => guard.clone(),
                    Err(poisoned) => {
                        eprintln!("[WARN] complete_request: response_buffer mutex poisoned, recovering");
                        poisoned.into_inner().clone()
                    }
                };

                let res = RequestResult {
                    status_code,
                    headers,
                    body,
                };
                let _ = tx.send(Ok(res));
            }
            Err(e) => {
                let _ = tx.send(Err(e));
            }
        }
    }
}

// -----------------------------------------------------------------------------
// Upload Data Provider Callbacks
// -----------------------------------------------------------------------------

struct UploadContext {
    data: Vec<u8>,
    position: u64,
}

unsafe extern "C" fn upload_get_length(self_: Cronet_UploadDataProviderPtr) -> i64 {
    let context_ptr = Cronet_UploadDataProvider_GetClientContext(self_) as *mut UploadContext;
    let context = &*context_ptr;
    context.data.len() as i64
}

unsafe extern "C" fn upload_read(
    self_: Cronet_UploadDataProviderPtr,
    sink: Cronet_UploadDataSinkPtr,
    buffer: Cronet_BufferPtr,
) {
    let context_ptr = Cronet_UploadDataProvider_GetClientContext(self_) as *mut UploadContext;
    let context = &mut *context_ptr;

    let buffer_size = Cronet_Buffer_GetSize(buffer);
    let buffer_data = Cronet_Buffer_GetData(buffer) as *mut u8;

    let remaining = (context.data.len() as u64) - context.position;
    let to_read = std::cmp::min(buffer_size, remaining);

    if to_read > 0 {
        ptr::copy_nonoverlapping(
            context.data.as_ptr().add(context.position as usize),
            buffer_data,
            to_read as usize,
        );
        context.position += to_read;
    }

    Cronet_UploadDataSink_OnReadSucceeded(sink, to_read, false);
}

unsafe extern "C" fn upload_rewind(
    self_: Cronet_UploadDataProviderPtr,
    sink: Cronet_UploadDataSinkPtr,
) {
    let context_ptr = Cronet_UploadDataProvider_GetClientContext(self_) as *mut UploadContext;
    let context = &mut *context_ptr;
    context.position = 0;
    Cronet_UploadDataSink_OnRewindSucceeded(sink);
}

unsafe extern "C" fn upload_close(self_: Cronet_UploadDataProviderPtr) {
    let context_ptr = Cronet_UploadDataProvider_GetClientContext(self_) as *mut UploadContext;
    // Take ownership to drop
    let _ = Box::from_raw(context_ptr);
}

// -----------------------------------------------------------------------------
// Session Management
// -----------------------------------------------------------------------------

use std::sync::RwLock;
use std::time::Instant;
use uuid::Uuid;

/// 会话配置
#[derive(Clone, Debug)]
pub struct SessionConfig {
    pub proxy_rules: Option<String>,
    pub skip_cert_verify: bool,
    pub timeout_ms: u64,
    pub cipher_suites: Option<Vec<String>>,
    pub tls_curves: Option<Vec<String>>,
    pub tls_extensions: Option<Vec<String>>,
    pub signature_algorithms: Option<Vec<String>>,
    pub allow_redirects: bool,
}

/// 单个会话 - 持有独立的 Cronet Engine
pub struct Session {
    pub id: String,
    engine_ptr: Cronet_EnginePtr,
    pub config: SessionConfig,
    pub created_at: Instant,
    active_requests: Arc<AtomicUsize>,  // 追踪活跃请求数量（仅用于监控）
    in_flight_executors: Arc<AtomicUsize>,  // 追踪正在执行的 executor 回调数量
    is_closed: Arc<AtomicBool>,  // 标记 session 是否已关闭
}

unsafe impl Send for Session {}
unsafe impl Sync for Session {}

impl Drop for Session {
    fn drop(&mut self) {
        verbose_log!("[DEBUG] Session::drop - Starting for session {}", self.id);

        // 标记 session 已关闭
        self.is_closed.store(true, Ordering::Release);

        unsafe {
            if !self.engine_ptr.is_null() {
                // 等待所有活跃请求完成
                let active = self.active_requests.load(Ordering::Acquire);
                verbose_log!("[DEBUG] Session::drop - active_requests={}", active);

                if active > 0 {
                    verbose_log!("[DEBUG] Session::drop - Waiting for {} active requests to complete", active);
                    let start = std::time::Instant::now();
                    while self.active_requests.load(Ordering::Acquire) > 0 {
                        if start.elapsed() > std::time::Duration::from_secs(30) {
                            eprintln!("[WARN] Session::drop - Timeout waiting for {} active requests; deferring engine cleanup",
                                self.active_requests.load(Ordering::Acquire));
                            let engine_ptr = std::mem::replace(
                                &mut self.engine_ptr,
                                std::ptr::null_mut(),
                            );
                            defer_engine_cleanup(engine_ptr, self.active_requests.clone());
                            return;
                        }
                        std::thread::sleep(std::time::Duration::from_millis(50));
                    }
                }

                // 所有请求已完成，可以安全销毁
                verbose_log!("[DEBUG] Session::drop - Calling Cronet_Engine_Shutdown");
                Cronet_Engine_Shutdown(self.engine_ptr);

                verbose_log!("[DEBUG] Session::drop - Calling Cronet_Engine_Destroy");
                Cronet_Engine_Destroy(self.engine_ptr);
                verbose_log!("[DEBUG] Session::drop - Engine destroyed");
            }
        }
        verbose_log!("[DEBUG] Session::drop - Finished for session {}", self.id);
    }
}

/// 会话管理器 - 管理多个会话，支持并发访问
pub struct SessionManager {
    sessions: RwLock<HashMap<String, Arc<Session>>>,
}

impl SessionManager {
    pub fn new() -> Self {
        SessionManager {
            sessions: RwLock::new(HashMap::new()),
        }
    }

    /// 创建新会话，返回会话ID
    pub fn create_session(&self, config: SessionConfig) -> String {
        let session_id = Uuid::new_v4().to_string();

        unsafe {
            let engine = Cronet_Engine_Create();
            let params = Cronet_EngineParams_Create();

            if let Some(ref proxy_rules) = config.proxy_rules {
                let c_rules = CString::new(proxy_rules.as_str()).expect("Invalid proxy string");
                Cronet_EngineParams_proxy_rules_set(params, c_rules.as_ptr());
            }

            Cronet_EngineParams_enable_quic_set(params, true);
            Cronet_EngineParams_enable_http2_set(params, true);
            Cronet_EngineParams_enable_brotli_set(params, true);

            if config.skip_cert_verify {
                Cronet_EngineParams_skip_cert_verify_set(params, true);
            }

            // Set custom TLS configuration and enable cookie store
            let mut options_parts = Vec::new();

            // Always enable Cookie Store to handle Set-Cookie in 302 redirects
            options_parts.push("\"enable_cookie_store\":true".to_string());

            if let Some(ref cipher_suites) = config.cipher_suites {
                if !cipher_suites.is_empty() {
                    if let Ok(value) = serde_json::to_string(cipher_suites) {
                        options_parts.push(format!("\"tls_cipher_suites\":{}", value));
                    }
                }
            }

            if let Some(ref tls_curves) = config.tls_curves {
                if !tls_curves.is_empty() {
                    if let Ok(value) = serde_json::to_string(tls_curves) {
                        options_parts.push(format!("\"tls_curves\":{}", value));
                    }
                }
            }

            if let Some(ref tls_extensions) = config.tls_extensions {
                if !tls_extensions.is_empty() {
                    if let Ok(value) = serde_json::to_string(tls_extensions) {
                        options_parts.push(format!("\"tls_extensions\":{}", value));
                    }
                }
            }

            if let Some(ref signature_algorithms) = config.signature_algorithms {
                if !signature_algorithms.is_empty() {
                    if let Ok(value) = serde_json::to_string(signature_algorithms) {
                        options_parts.push(format!("\"tls_signature_algorithms\":{}", value));
                    }
                }
            }

            // Always set experimental_options (at least for enable_cookie_store)
            if !options_parts.is_empty() {
                let experimental_options = format!("{{{}}}", options_parts.join(","));
                verbose_log!("[DEBUG] Setting experimental options: {}", experimental_options);
                let c_options = CString::new(experimental_options).expect("Invalid experimental options");
                Cronet_EngineParams_experimental_options_set(params, c_options.as_ptr());
            }

            let res = Cronet_Engine_StartWithParams(engine, params);
            Cronet_EngineParams_Destroy(params);

            if res != Cronet_RESULT_Cronet_RESULT_SUCCESS {
                eprintln!("[ERROR] Failed to create session engine: {:?}", res);
                Cronet_Engine_Destroy(engine);
                return String::new();
            }

            // 创建 in-flight 计数器用于监控
            let in_flight = Arc::new(AtomicUsize::new(0));

            let session = Session {
                id: session_id.clone(),
                engine_ptr: engine,
                config,
                created_at: Instant::now(),
                active_requests: Arc::new(AtomicUsize::new(0)),
                in_flight_executors: in_flight,
                is_closed: Arc::new(AtomicBool::new(false)),
            };

            verbose_log!("[DEBUG] Created session: {}", session_id);
            match self.sessions.write() {
                Ok(mut sessions) => {
                    sessions.insert(session_id.clone(), Arc::new(session));
                }
                Err(poisoned) => {
                    eprintln!("[WARN] create_session: RwLock poisoned, recovering");
                    let mut sessions = poisoned.into_inner();
                    sessions.insert(session_id.clone(), Arc::new(session));
                }
            }
        }

        session_id
    }

    /// 使用会话发送请求
    /// 限制并发请求数量,避免资源泄漏
    /// 返回 (CronetRequest, Receiver, timeout_ms)
    pub fn send_request(
        &self,
        session_id: &str,
        target: &crate::cronet_pb::TargetRequest,
        allow_redirects: bool,
    ) -> Option<(CronetRequest, oneshot::Receiver<Result<RequestResult, String>>, u64)> {
        let sessions = match self.sessions.read() {
            Ok(guard) => guard,
            Err(poisoned) => {
                eprintln!("[WARN] send_request: RwLock poisoned, recovering");
                poisoned.into_inner()
            }
        };
        let session = sessions.get(session_id)?;

        // 检查 session 是否已关闭
        if session.is_closed.load(Ordering::Acquire) {
            eprintln!("[WARN] Session {} is closed, rejecting request", session_id);
            return None;
        }

        // 增加活跃请求计数
        session.active_requests.fetch_add(1, Ordering::Acquire);
        let current_active = session.active_requests.load(Ordering::Acquire);

        verbose_log!("[DEBUG] Using session {} to send request to {} (active: {})",
            session_id, target.url, current_active);

        let (request, rx) = Self::start_request_with_engine(
            session.engine_ptr,
            target,
            Some(session.active_requests.clone()),
            Some(session.in_flight_executors.clone()),
            allow_redirects,
            None,
        );

        Some((request, rx, session.config.timeout_ms))
    }

    /// 使用指定的 engine 发送请求
    fn start_request_with_engine(
        engine_ptr: Cronet_EnginePtr,
        target: &crate::cronet_pb::TargetRequest,
        active_requests: Option<Arc<AtomicUsize>>,
        in_flight_executors: Option<Arc<AtomicUsize>>,
        allow_redirects: bool,
        stream_sender: Option<mpsc::Sender<StreamChunk>>,
    ) -> (CronetRequest, oneshot::Receiver<Result<RequestResult, String>>) {
        unsafe {
            let (tx, rx) = oneshot::channel();

            // 创建完成标志
            let completed = Arc::new(AtomicBool::new(false));
            let callback_claimed = Arc::new(AtomicBool::new(false));
            let callback_returned = Arc::new(AtomicBool::new(false));

            let is_streaming = stream_sender.is_some();
            let context = Box::new(RequestContext {
                tx: Mutex::new(if is_streaming { None } else { Some(tx) }),
                response_buffer: Mutex::new(Vec::new()),
                response_headers: Mutex::new(Vec::new()),
                status_code: AtomicI32::new(0),
                completed: completed.clone(),
                callback_claimed: callback_claimed.clone(),
                callback_returned: callback_returned.clone(),
                active_requests,
                allow_redirects,
                redirect_response: Mutex::new(None),
                context_taken: AtomicBool::new(false),
                is_streaming,
                stream_tx: Mutex::new(stream_sender),
            });
            let context_ptr = Box::into_raw(context);

            // 创建独立的 ExecutorContext
            let executor_context = Box::new(ExecutorContext {
                in_flight_executors,
            });
            let executor_context_ptr = Box::into_raw(executor_context);

            // Executor - 使用独立的 ExecutorContext
            let executor_ptr = Cronet_Executor_CreateWith(Some(executor_execute));
            Cronet_Executor_SetClientContext(executor_ptr, executor_context_ptr as *mut c_void);

            // Callback - 使用 RequestContext
            let callback_ptr = Cronet_UrlRequestCallback_CreateWith(
                Some(on_redirect_received),
                Some(on_response_started),
                Some(on_read_completed),
                Some(on_succeeded),
                Some(on_failed),
                Some(on_canceled),
            );
            Cronet_UrlRequestCallback_SetClientContext(callback_ptr, context_ptr as *mut c_void);

            // Request & Params
            let request_ptr = Cronet_UrlRequest_Create();
            let params_ptr = Cronet_UrlRequestParams_Create();

            let c_method = CString::new(target.method.as_str()).unwrap();
            Cronet_UrlRequestParams_http_method_set(params_ptr, c_method.as_ptr());

            // Set highest priority to get HTTP/2 weight=256 (same as normal browsers)
            Cronet_UrlRequestParams_priority_set(
                params_ptr,
                4  // REQUEST_PRIORITY_HIGHEST
            );

            let c_url = CString::new(target.url.as_str()).unwrap();

            // Headers - 按顺序添加（跳过无效的 header name/value）
            for header in &target.headers {
                if !is_valid_header_name(&header.name) {
                    eprintln!("[WARN] Skipping header with invalid name: {:?}", header.name);
                    continue;
                }
                if !is_valid_header_value(&header.value) {
                    eprintln!("[WARN] Skipping header with invalid value for key {:?}", header.name);
                    continue;
                }
                let c_key = CString::new(header.name.as_str()).unwrap();
                let c_val = CString::new(header.value.as_str()).unwrap();

                let header_ptr = Cronet_HttpHeader_Create();
                Cronet_HttpHeader_name_set(header_ptr, c_key.as_ptr());
                Cronet_HttpHeader_value_set(header_ptr, c_val.as_ptr());

                Cronet_UrlRequestParams_request_headers_add(params_ptr, header_ptr);
                Cronet_HttpHeader_Destroy(header_ptr);
            }

            // Upload Data Provider (Body)
            let mut upload_data_provider_ptr: Option<Cronet_UploadDataProviderPtr> = None;
            let upload_body_data = if !target.body.is_empty() {
                Some(target.body.clone())
            } else {
                None
            };

            if let Some(body) = &upload_body_data {
                let upload_context = Box::new(UploadContext {
                    data: body.clone(),
                    position: 0,
                });
                let upload_context_ptr = Box::into_raw(upload_context);

                let provider = Cronet_UploadDataProvider_CreateWith(
                    Some(upload_get_length),
                    Some(upload_read),
                    Some(upload_rewind),
                    Some(upload_close),
                );
                Cronet_UploadDataProvider_SetClientContext(
                    provider,
                    upload_context_ptr as *mut c_void,
                );

                Cronet_UrlRequestParams_upload_data_provider_set(params_ptr, provider);
                Cronet_UrlRequestParams_upload_data_provider_executor_set(params_ptr, executor_ptr);

                upload_data_provider_ptr = Some(provider);
            }

            Cronet_UrlRequest_InitWithParams(
                request_ptr,
                engine_ptr,
                c_url.as_ptr(),
                params_ptr,
                callback_ptr,
                executor_ptr,
            );

            Cronet_UrlRequestParams_Destroy(params_ptr);

            // Start
            Cronet_UrlRequest_Start(request_ptr);

            let request_handle = CronetRequest {
                ptr: request_ptr,
                callback_ptr,
                executor_ptr,
                executor_context_ptr,
                owned_engine_ptr: None, // Session owns the engine
                upload_data_provider_ptr,
                upload_body_data,
                completed,
                callback_claimed,
                callback_returned,
            };

            (request_handle, rx)
        }
    }

    /// 使用会话发送流式请求
    /// 返回 (CronetRequest, Receiver<StreamChunk>, timeout_ms)
    pub fn send_request_stream(
        &self,
        session_id: &str,
        target: &crate::cronet_pb::TargetRequest,
        allow_redirects: bool,
    ) -> Option<(CronetRequest, mpsc::Receiver<StreamChunk>, u64)> {
        let sessions = match self.sessions.read() {
            Ok(guard) => guard,
            Err(poisoned) => {
                eprintln!("[WARN] send_request_stream: RwLock poisoned, recovering");
                poisoned.into_inner()
            }
        };
        let session = sessions.get(session_id)?;

        if session.is_closed.load(Ordering::Acquire) {
            eprintln!("[WARN] Session {} is closed, rejecting stream request", session_id);
            return None;
        }

        session.active_requests.fetch_add(1, Ordering::Acquire);

        let (stream_tx, stream_rx) = mpsc::channel(STREAM_CHANNEL_CAPACITY);

        let (request, _rx) = Self::start_request_with_engine(
            session.engine_ptr,
            target,
            Some(session.active_requests.clone()),
            Some(session.in_flight_executors.clone()),
            allow_redirects,
            Some(stream_tx),
        );

        Some((request, stream_rx, session.config.timeout_ms))
    }

    /// 关闭会话
    pub fn close_session(&self, session_id: &str) -> bool {
        let mut sessions = match self.sessions.write() {
            Ok(guard) => guard,
            Err(poisoned) => {
                eprintln!("[WARN] close_session: RwLock poisoned, recovering");
                poisoned.into_inner()
            }
        };
        let removed = sessions.remove(session_id);
        drop(sessions);

        if removed.is_some() {
            verbose_log!("[DEBUG] Closed session: {}", session_id);
            true
        } else {
            verbose_log!("[DEBUG] Session not found: {}", session_id);
            false
        }
    }

    /// 列出所有会话ID
    pub fn list_sessions(&self) -> Vec<String> {
        match self.sessions.read() {
            Ok(sessions) => sessions.keys().cloned().collect(),
            Err(poisoned) => poisoned.into_inner().keys().cloned().collect(),
        }
    }

    /// 获取会话数量
    pub fn session_count(&self) -> usize {
        match self.sessions.read() {
            Ok(sessions) => sessions.len(),
            Err(poisoned) => poisoned.into_inner().len(),
        }
    }

    /// 检查会话是否存在
    pub fn session_exists(&self, session_id: &str) -> bool {
        match self.sessions.read() {
            Ok(sessions) => sessions.contains_key(session_id),
            Err(poisoned) => poisoned.into_inner().contains_key(session_id),
        }
    }

    /// Return the raw engine together with ownership that keeps the session
    /// alive for long-lived users such as WebSockets.
    pub fn get_engine_handle(&self, session_id: &str) -> Option<(Cronet_EnginePtr, Arc<Session>)> {
        let sessions = match self.sessions.read() {
            Ok(sessions) => sessions,
            Err(poisoned) => poisoned.into_inner(),
        };
        let session = sessions.get(session_id)?.clone();
        if session.is_closed.load(Ordering::Acquire) {
            return None;
        }
        Some((session.engine_ptr, session))
    }
}

// -----------------------------------------------------------------------------
// WebSocket Support
// -----------------------------------------------------------------------------

// Bindgen names anonymous C enum constants differently across platforms.
// Keep the values from cronet_websocket_c.h under stable Rust names.
const WEBSOCKET_MESSAGE_TEXT: Cronet_WebSocket_MessageType = 1;
const WEBSOCKET_MESSAGE_BINARY: Cronet_WebSocket_MessageType = 2;

/// WebSocket 事件
#[derive(Debug, Clone)]
pub enum WebSocketEvent {
    Open { protocol: String },
    Message { is_text: bool, data: Vec<u8> },
    Close { was_clean: bool, code: u16, reason: String },
    Error { net_error: i32, message: String },
}

/// 内部状态，通过 user_data 指针传递给 C 回调
struct WebSocketState {
    tx: std::sync::mpsc::Sender<WebSocketEvent>,
}

unsafe extern "C" fn ws_on_open(
    _ws: Cronet_WebSocketPtr,
    user_data: *mut c_void,
    protocol: *const std::os::raw::c_char,
) {
    let state = &*(user_data as *const WebSocketState);
    let proto = if protocol.is_null() {
        String::new()
    } else {
        CStr::from_ptr(protocol).to_string_lossy().into_owned()
    };
    let _ = state.tx.send(WebSocketEvent::Open { protocol: proto });
}

unsafe extern "C" fn ws_on_message(
    _ws: Cronet_WebSocketPtr,
    user_data: *mut c_void,
    msg_type: Cronet_WebSocket_MessageType,
    data: *const c_void,
    len: u64,
) {
    let state = &*(user_data as *const WebSocketState);
    let slice = if len == 0 {
        &[]
    } else if data.is_null() {
        let _ = state.tx.send(WebSocketEvent::Error {
            net_error: -1,
            message: "WebSocket callback returned null message data".to_string(),
        });
        return;
    } else {
        std::slice::from_raw_parts(data as *const u8, len as usize)
    };
    let _ = state.tx.send(WebSocketEvent::Message {
        is_text: msg_type == WEBSOCKET_MESSAGE_TEXT,
        data: slice.to_vec(),
    });
}

unsafe extern "C" fn ws_on_close(
    _ws: Cronet_WebSocketPtr,
    user_data: *mut c_void,
    was_clean: std::os::raw::c_int,
    code: u16,
    reason: *const std::os::raw::c_char,
) {
    let state = &*(user_data as *const WebSocketState);
    let reason_str = if reason.is_null() {
        String::new()
    } else {
        CStr::from_ptr(reason).to_string_lossy().into_owned()
    };
    let _ = state.tx.send(WebSocketEvent::Close {
        was_clean: was_clean != 0,
        code,
        reason: reason_str,
    });
}

unsafe extern "C" fn ws_on_error(
    _ws: Cronet_WebSocketPtr,
    user_data: *mut c_void,
    net_error: std::os::raw::c_int,
    message: *const std::os::raw::c_char,
) {
    let state = &*(user_data as *const WebSocketState);
    let msg = if message.is_null() {
        String::new()
    } else {
        CStr::from_ptr(message).to_string_lossy().into_owned()
    };
    let _ = state.tx.send(WebSocketEvent::Error {
        net_error,
        message: msg,
    });
}

/// Rust-safe WebSocket handle
pub struct CronetWebSocket {
    ws_ptr: Cronet_WebSocketPtr,
    // Box 保持 state 存活，C 回调通过 user_data 指针访问
    _state: Box<WebSocketState>,
    pub rx: std::sync::mpsc::Receiver<WebSocketEvent>,
    // Must outlive ws_ptr: Session::drop destroys the Cronet engine.
    _session: Arc<Session>,
}

unsafe impl Send for CronetWebSocket {}

impl CronetWebSocket {
    pub fn new_with_lifetime(
        engine_ptr: Cronet_EnginePtr,
        session: Arc<Session>,
    ) -> Result<Self, String> {
        Self::create(engine_ptr, session)
    }

    fn create(
        engine_ptr: Cronet_EnginePtr,
        session: Arc<Session>,
    ) -> Result<Self, String> {
        let (tx, rx) = std::sync::mpsc::channel();
        let state = Box::new(WebSocketState { tx });
        let state_ptr = &*state as *const WebSocketState as *mut c_void;

        let callbacks = Cronet_WebSocket_Callbacks {
            on_open: Some(ws_on_open),
            on_message: Some(ws_on_message),
            on_close: Some(ws_on_close),
            on_error: Some(ws_on_error),
        };

        let ws_ptr = unsafe {
            Cronet_WebSocket_Create(engine_ptr, &callbacks, state_ptr)
        };
        if ws_ptr.is_null() {
            return Err("Failed to create WebSocket".to_string());
        }

        Ok(CronetWebSocket {
            ws_ptr,
            _state: state,
            rx,
            _session: session,
        })
    }

    pub fn connect(
        &self,
        url: &str,
        sub_protocols: Option<&str>,
        origin: Option<&str>,
        extra_headers: Option<&str>,
    ) -> Result<(), String> {
        let c_url = safe_cstring(url, "ws_url")?;
        let c_protos = sub_protocols.map(|s| safe_cstring(s, "ws_sub_protocols")).transpose()?;
        let c_origin = origin.map(|s| safe_cstring(s, "ws_origin")).transpose()?;
        #[cfg(target_os = "windows")]
        if matches!(extra_headers, Some(headers) if !headers.is_empty()) {
            return Err("Custom WebSocket headers are not supported by the bundled Windows Cronet library".to_string());
        }
        #[cfg(not(target_os = "windows"))]
        let c_extra_headers = extra_headers.map(|s| safe_cstring(s, "ws_extra_headers")).transpose()?;

        let ret = unsafe {
            #[cfg(target_os = "windows")]
            {
                Cronet_WebSocket_Connect(
                    self.ws_ptr,
                    c_url.as_ptr(),
                    c_protos.as_ref().map_or(ptr::null(), |s| s.as_ptr()),
                    c_origin.as_ref().map_or(ptr::null(), |s| s.as_ptr()),
                )
            }
            #[cfg(not(target_os = "windows"))]
            {
            Cronet_WebSocket_Connect(
                self.ws_ptr,
                c_url.as_ptr(),
                c_protos.as_ref().map_or(ptr::null(), |s| s.as_ptr()),
                c_origin.as_ref().map_or(ptr::null(), |s| s.as_ptr()),
                c_extra_headers.as_ref().map_or(ptr::null(), |s| s.as_ptr()),
            )
            }
        };
        if ret != 0 {
            return Err(format!("WebSocket connect failed: {}", ret));
        }
        Ok(())
    }

    pub fn send_text(&self, text: &str) -> Result<(), String> {
        let ret = unsafe {
            Cronet_WebSocket_Send(
                self.ws_ptr,
                WEBSOCKET_MESSAGE_TEXT,
                text.as_ptr() as *const c_void,
                text.len() as u64,
            )
        };
        if ret != 0 {
            return Err(format!("WebSocket send failed: {}", ret));
        }
        Ok(())
    }

    pub fn send_binary(&self, data: &[u8]) -> Result<(), String> {
        let ret = unsafe {
            Cronet_WebSocket_Send(
                self.ws_ptr,
                WEBSOCKET_MESSAGE_BINARY,
                data.as_ptr() as *const c_void,
                data.len() as u64,
            )
        };
        if ret != 0 {
            return Err(format!("WebSocket send failed: {}", ret));
        }
        Ok(())
    }

    pub fn close(&self, code: u16, reason: &str) -> Result<(), String> {
        let c_reason = safe_cstring(reason, "ws_close_reason")?;
        let ret = unsafe {
            Cronet_WebSocket_Close(self.ws_ptr, code, c_reason.as_ptr())
        };
        if ret != 0 {
            return Err(format!("WebSocket close failed: {}", ret));
        }
        Ok(())
    }
}

impl Drop for CronetWebSocket {
    fn drop(&mut self) {
        unsafe {
            if !self.ws_ptr.is_null() {
                Cronet_WebSocket_Destroy(self.ws_ptr);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn completion_guard_updates_state_on_drop() {
        let completed = Arc::new(AtomicBool::new(false));
        let active = Arc::new(AtomicUsize::new(1));
        {
            let _guard = RequestCompletionGuard {
                completed: completed.clone(),
                active_requests: Some(active.clone()),
            };
            assert!(!completed.load(Ordering::Acquire));
            assert_eq!(active.load(Ordering::Acquire), 1);
        }
        assert!(completed.load(Ordering::Acquire));
        assert_eq!(active.load(Ordering::Acquire), 0);
    }

    #[test]
    fn terminal_request_is_not_canceled_again() {
        assert!(should_cancel_request(false, false));
        assert!(!should_cancel_request(true, false));
        assert!(!should_cancel_request(false, true));
    }

    #[test]
    fn deferred_cleanup_waits_for_callback_return() {
        let completed = Arc::new(AtomicBool::new(true));
        let callback_returned = Arc::new(AtomicBool::new(false));
        let cleanup = DeferredRequestCleanup {
            ptr: std::ptr::null_mut(),
            callback_ptr: std::ptr::null_mut(),
            executor_ptr: std::ptr::null_mut(),
            executor_context_ptr: std::ptr::null_mut(),
            owned_engine_ptr: None,
            upload_data_provider_ptr: None,
            upload_body_data: None,
            completed: completed.clone(),
            callback_returned: callback_returned.clone(),
            callback_claimed: Arc::new(AtomicBool::new(false)),
            queued_at: std::time::Instant::now(),
            cancel_requested: Arc::new(AtomicBool::new(false)),
        };
        assert!(!DeferredCleanup::Request(cleanup).is_ready());

        callback_returned.store(true, Ordering::Release);
        let cleanup = DeferredRequestCleanup {
            ptr: std::ptr::null_mut(),
            callback_ptr: std::ptr::null_mut(),
            executor_ptr: std::ptr::null_mut(),
            executor_context_ptr: std::ptr::null_mut(),
            owned_engine_ptr: None,
            upload_data_provider_ptr: None,
            upload_body_data: None,
            completed,
            callback_returned,
            callback_claimed: Arc::new(AtomicBool::new(false)),
            queued_at: std::time::Instant::now(),
            cancel_requested: Arc::new(AtomicBool::new(false)),
        };
        assert!(DeferredCleanup::Request(cleanup).is_ready());
    }
}
