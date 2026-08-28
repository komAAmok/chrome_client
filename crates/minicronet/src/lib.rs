//! Safe Rust ownership and configuration layer for the MiniCronet Core.
//!
//! Protocol implementation remains exclusively in `libminicronet`; this crate
//! only owns handles, validates configuration, and maps native errors.

mod request;
mod websocket;

use std::{
    ffi::CStr,
    fmt,
    ptr::NonNull,
    sync::{Arc, Mutex, MutexGuard},
};

use minicronet_sys as sys;

pub use request::{
    CacheMode, Header, Redirect, RedirectMode, Request, RequestConfig, RequestError,
    RequestPriority, Response, ResponseFuture, ResponseStream, Upload,
};
pub use websocket::{
    CloseInfo, MessageType, WebSocket, WebSocketConfig, WebSocketEvent, WebSocketEvents,
    WebSocketFailure,
};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Error {
    InvalidArgument,
    UnsupportedAbi,
    OutOfMemory,
    InitializationFailed,
    InvalidState,
    Timeout,
    Canceled,
    Network,
    Tls,
    Proxy,
    Protocol,
    Redirect,
    CacheMiss,
    ProfileConflict,
    ProfileUnsupported,
    CallbackPanic,
    BufferLimit,
    Native(i32),
}

impl From<sys::mn_result_t> for Error {
    fn from(value: sys::mn_result_t) -> Self {
        match value {
            sys::mn_result_t::MN_ERROR_INVALID_ARGUMENT => Self::InvalidArgument,
            sys::mn_result_t::MN_ERROR_UNSUPPORTED_ABI => Self::UnsupportedAbi,
            sys::mn_result_t::MN_ERROR_OUT_OF_MEMORY => Self::OutOfMemory,
            sys::mn_result_t::MN_ERROR_INITIALIZATION_FAILED => Self::InitializationFailed,
            sys::mn_result_t::MN_ERROR_INVALID_STATE => Self::InvalidState,
            sys::mn_result_t::MN_ERROR_TIMEOUT => Self::Timeout,
            sys::mn_result_t::MN_ERROR_CANCELED => Self::Canceled,
            sys::mn_result_t::MN_ERROR_NETWORK => Self::Network,
            sys::mn_result_t::MN_ERROR_TLS => Self::Tls,
            sys::mn_result_t::MN_ERROR_PROXY => Self::Proxy,
            sys::mn_result_t::MN_ERROR_PROTOCOL => Self::Protocol,
            sys::mn_result_t::MN_ERROR_REDIRECT => Self::Redirect,
            sys::mn_result_t::MN_ERROR_CACHE_MISS => Self::CacheMiss,
            sys::mn_result_t::MN_ERROR_PROFILE_CONFLICT => Self::ProfileConflict,
            sys::mn_result_t::MN_ERROR_PROFILE_UNSUPPORTED => Self::ProfileUnsupported,
            sys::mn_result_t::MN_OK => Self::Native(0),
            other => Self::Native(other.0),
        }
    }
}

impl fmt::Display for Error {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Native(code) => write!(formatter, "native error {code}"),
            other => write!(formatter, "{other:?}"),
        }
    }
}

impl std::error::Error for Error {}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ProtocolMode {
    Native,
    ForceH1,
    ForceH2,
    ForceH3,
}

impl From<ProtocolMode> for sys::mn_protocol_mode_t {
    fn from(value: ProtocolMode) -> Self {
        match value {
            ProtocolMode::Native => Self::MN_PROTOCOL_NATIVE,
            ProtocolMode::ForceH1 => Self::MN_PROTOCOL_FORCE_H1,
            ProtocolMode::ForceH2 => Self::MN_PROTOCOL_FORCE_H2,
            ProtocolMode::ForceH3 => Self::MN_PROTOCOL_FORCE_H3,
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum HttpCacheMode {
    Enabled,
    Disabled,
}

impl From<HttpCacheMode> for sys::mn_http_cache_mode_t {
    fn from(value: HttpCacheMode) -> Self {
        match value {
            HttpCacheMode::Enabled => Self::MN_HTTP_CACHE_ENABLED,
            HttpCacheMode::Disabled => Self::MN_HTTP_CACHE_DISABLED,
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum TlsVerifyMode {
    ChromiumDefault,
    CustomCa,
    Insecure,
}

impl From<TlsVerifyMode> for sys::mn_tls_verify_mode_t {
    fn from(value: TlsVerifyMode) -> Self {
        match value {
            TlsVerifyMode::ChromiumDefault => Self::MN_TLS_VERIFY_CHROMIUM_DEFAULT,
            TlsVerifyMode::CustomCa => Self::MN_TLS_VERIFY_CUSTOM_CA,
            TlsVerifyMode::Insecure => Self::MN_TLS_VERIFY_INSECURE,
        }
    }
}

#[derive(Clone, Debug)]
pub struct EngineConfig {
    pub profile_id: Option<String>,
    pub profile_namespace: Option<String>,
    pub user_agent: Option<String>,
    pub accept_language: Option<String>,
    pub proxy_rules: Option<String>,
    pub proxy_username: Option<String>,
    pub proxy_password: Option<String>,
    pub http_cache: HttpCacheMode,
    pub protocol: ProtocolMode,
    pub tls_verify: TlsVerifyMode,
    pub custom_ca_pem: Option<Vec<u8>>,
}

impl Default for EngineConfig {
    fn default() -> Self {
        Self {
            profile_id: None,
            profile_namespace: None,
            user_agent: None,
            accept_language: None,
            proxy_rules: None,
            proxy_username: None,
            proxy_password: None,
            http_cache: HttpCacheMode::Enabled,
            protocol: ProtocolMode::Native,
            tls_verify: TlsVerifyMode::ChromiumDefault,
            custom_ca_pem: None,
        }
    }
}

struct EngineInner {
    raw: NonNull<sys::mn_engine_t>,
}

// The Core ABI documents retain/release and engine operations as thread-safe.
// The opaque pointer itself carries no Rust auto-traits, so state that
// audited ABI contract explicitly at this boundary.
unsafe impl Send for EngineInner {}
unsafe impl Sync for EngineInner {}

impl Drop for EngineInner {
    fn drop(&mut self) {
        // The Core contract makes the final release safe after all operations
        // have completed; Engine owns the sole Rust reference here.
        unsafe { sys::mn_engine_release(self.raw.as_ptr()) };
    }
}

#[allow(dead_code)]
#[derive(Clone)]
pub struct Engine(Arc<EngineInner>);

impl Engine {
    pub fn new(config: EngineConfig) -> Result<Self, Error> {
        let native_abi = unsafe { sys::mn_abi_version() };
        if native_abi != sys::MN_ABI_VERSION {
            return Err(Error::UnsupportedAbi);
        }
        let profile_id = bytes(config.profile_id.as_deref());
        let profile_namespace = bytes(config.profile_namespace.as_deref());
        let user_agent = bytes(config.user_agent.as_deref());
        let accept_language = bytes(config.accept_language.as_deref());
        let proxy_rules = bytes(config.proxy_rules.as_deref());
        let proxy_username = bytes(config.proxy_username.as_deref());
        let proxy_password = bytes(config.proxy_password.as_deref());
        let ca = config.custom_ca_pem.as_deref().unwrap_or_default();
        let raw_config = sys::mn_engine_config_t {
            size: std::mem::size_of::<sys::mn_engine_config_t>() as u32,
            version: sys::MN_ABI_VERSION,
            user_agent: ptr_or_null(&user_agent),
            user_agent_length: user_agent.len(),
            proxy_rules: ptr_or_null(&proxy_rules),
            proxy_rules_length: proxy_rules.len(),
            proxy_username: ptr_or_null(&proxy_username),
            proxy_username_length: proxy_username.len(),
            proxy_password: ptr_or_null(&proxy_password),
            proxy_password_length: proxy_password.len(),
            http_cache_mode: config.http_cache.into(),
            accept_language: ptr_or_null(&accept_language),
            accept_language_length: accept_language.len(),
            profile_id: ptr_or_null(&profile_id),
            profile_id_length: profile_id.len(),
            profile_namespace: ptr_or_null(&profile_namespace),
            profile_namespace_length: profile_namespace.len(),
            protocol_mode: config.protocol.into(),
            tls_verify_mode: config.tls_verify.into(),
            custom_ca_pem: ptr_or_null_bytes(ca),
            custom_ca_pem_length: ca.len(),
        };
        let mut raw = std::ptr::null_mut();
        let result = unsafe { sys::mn_engine_create(&raw_config, &mut raw) };
        if result != sys::mn_result_t::MN_OK {
            return Err(result.into());
        }
        let raw = NonNull::new(raw).ok_or(Error::InitializationFailed)?;
        Ok(Self(Arc::new(EngineInner { raw })))
    }

    pub fn core_abi_version() -> u32 {
        sys::MN_ABI_VERSION
    }

    pub fn core_version() -> Option<&'static CStr> {
        // The returned string is static by the Core ABI contract.
        let ptr = unsafe { sys::mn_version_string() };
        (!ptr.is_null()).then(|| unsafe { CStr::from_ptr(ptr.cast()) })
    }

    pub fn request(&self, config: RequestConfig) -> Result<Request, Error> {
        Request::new(self, config)
    }

    pub fn websocket(&self, config: WebSocketConfig) -> Result<WebSocket, Error> {
        WebSocket::new(self, config)
    }

    #[allow(dead_code)]
    pub(crate) fn raw(&self) -> *mut sys::mn_engine_t {
        self.0.raw.as_ptr()
    }
}

fn bytes(value: Option<&str>) -> Vec<u8> {
    value.unwrap_or_default().as_bytes().to_vec()
}

fn ptr_or_null(value: &[u8]) -> *const i8 {
    if value.is_empty() {
        std::ptr::null()
    } else {
        value.as_ptr().cast()
    }
}

fn ptr_or_null_bytes(value: &[u8]) -> *const u8 {
    if value.is_empty() {
        std::ptr::null()
    } else {
        value.as_ptr()
    }
}

pub(crate) fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner)
}

pub(crate) fn timeout_ms(timeout: Option<std::time::Duration>) -> u64 {
    timeout
        .map(|value| value.as_millis().min(u128::from(u64::MAX)) as u64)
        .unwrap_or(0)
}

pub(crate) fn ffi_bytes(data: &[u8]) -> *const u8 {
    if data.is_empty() {
        std::ptr::null()
    } else {
        data.as_ptr()
    }
}
