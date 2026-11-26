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
    CacheMode, EchStatus, Header, HeaderPolicy, HttpVersion, Redirect, RedirectMode, Request,
    RequestConfig, RequestError, RequestPriority, Response, ResponseFuture, ResponseHeader,
    ResponseStream, Upload,
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

/// Which header block a name belongs to. The two scopes answer differently for
/// the same name -- Connection is refused on a request and on a handshake, but
/// Origin is refused only on the handshake -- so the scope is never defaulted.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum HeaderScope {
    Request,
    WebSocket,
}

impl From<HeaderScope> for sys::mn_header_scope_t {
    fn from(value: HeaderScope) -> Self {
        match value {
            HeaderScope::Request => Self::MN_HEADER_SCOPE_REQUEST,
            HeaderScope::WebSocket => Self::MN_HEADER_SCOPE_WEBSOCKET,
        }
    }
}

/// Asks the Core what it does with one header name.
///
/// The table lives in the Core because the answer must not depend on the
/// binding's language: Go and Node get the same bits as Python by asking the
/// same question. Error::InvalidArgument means the question itself was malformed
/// (empty name, unknown scope), not that the name is unwritable -- an unwritable
/// name is a successful answer with the relevant flag clear.
pub fn header_disposition(name: &str, scope: HeaderScope) -> Result<u32, Error> {
    let mut flags = 0u32;
    let result = unsafe {
        sys::mn_header_disposition(name.as_ptr().cast(), name.len(), scope.into(), &mut flags)
    };
    if result != sys::mn_result_t::MN_OK {
        return Err(result.into());
    }
    Ok(flags)
}

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

/// Which platform identity the engine presents.
///
/// The User-Agent group, sec-ch-ua-platform and sec-ch-ua-mobile are three
/// statements of one fact, so all three derive from this value. Auto follows
/// the host the Core runs on.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Platform {
    Auto,
    Windows,
    Macos,
    Linux,
}

impl From<Platform> for sys::mn_platform_t {
    fn from(value: Platform) -> Self {
        match value {
            Platform::Auto => Self::MN_PLATFORM_AUTO,
            Platform::Windows => Self::MN_PLATFORM_WINDOWS,
            Platform::Macos => Self::MN_PLATFORM_MACOS,
            Platform::Linux => Self::MN_PLATFORM_LINUX,
        }
    }
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

/// One cookie as the Core reports it.
///
/// Every field is owned: the Core's strings live only until the next cookies
/// call on that engine, so they are copied at the boundary.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Cookie {
    pub name: String,
    pub value: String,
    pub domain: String,
    pub path: String,
    /// Seconds since the Unix epoch, meaningful only when `has_expiry`.
    pub expiry_unix_seconds: i64,
    pub has_expiry: bool,
    pub secure: bool,
    pub http_only: bool,
    pub same_site: CookieSameSite,
}

/// The `SameSite` attribute as the store recorded it.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum CookieSameSite {
    Unspecified,
    NoRestriction,
    Lax,
    Strict,
}

impl Cookie {
    fn from_raw(raw: sys::mn_cookie_t) -> Self {
        Self {
            name: read_str(raw.name, raw.name_length),
            value: read_str(raw.value, raw.value_length),
            domain: read_str(raw.domain, raw.domain_length),
            path: read_str(raw.path, raw.path_length),
            expiry_unix_seconds: raw.expiry_unix_seconds,
            has_expiry: raw.has_expiry != 0,
            secure: raw.secure != 0,
            http_only: raw.http_only != 0,
            same_site: match raw.same_site {
                1 => CookieSameSite::NoRestriction,
                2 => CookieSameSite::Lax,
                3 => CookieSameSite::Strict,
                _ => CookieSameSite::Unspecified,
            },
        }
    }

    fn to_raw(&self) -> sys::mn_cookie_t {
        sys::mn_cookie_t {
            size: std::mem::size_of::<sys::mn_cookie_t>() as u32,
            version: sys::MN_ABI_VERSION,
            name: self.name.as_ptr().cast(),
            name_length: self.name.len(),
            value: self.value.as_ptr().cast(),
            value_length: self.value.len(),
            domain: self.domain.as_ptr().cast(),
            domain_length: self.domain.len(),
            path: self.path.as_ptr().cast(),
            path_length: self.path.len(),
            expiry_unix_seconds: self.expiry_unix_seconds,
            has_expiry: u8::from(self.has_expiry),
            secure: u8::from(self.secure),
            http_only: u8::from(self.http_only),
            same_site: match self.same_site {
                CookieSameSite::NoRestriction => 1,
                CookieSameSite::Lax => 2,
                CookieSameSite::Strict => 3,
                CookieSameSite::Unspecified => 0,
            },
        }
    }
}

/// Copies a Core-owned string, whose length the Core reports separately.
fn read_str(data: *const i8, length: usize) -> String {
    if data.is_null() || length == 0 {
        return String::new();
    }
    let bytes = unsafe { std::slice::from_raw_parts(data.cast::<u8>(), length) };
    String::from_utf8_lossy(bytes).into_owned()
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
    pub host_mapping_rules: Option<String>,
    pub dns_nameservers: Option<String>,
    pub doh_template: Option<String>,
    pub hsts_bypass_list: Option<String>,
    pub proxy_chain: Option<String>,
    pub dns_secure_mode: u8,
    pub dns_over_tls: u8,
    pub dns_rotate: u8,
    pub dns_padding: u8,
    pub platform: Platform,
    /// Whether the Core injects the profile's captured browser header set.
    ///
    /// The Core holds the captured set (core/source/request.cc, BrowserHeaderSet)
    /// and merges it under the caller's headers, so the wire order is the
    /// capture's order and a caller value only replaces the field it names.
    /// Without this flag that whole path is compiled but never reached, which is
    /// why `impersonate=` used to change only the User-Agent string.
    pub inject_client_hints: bool,
    /// Maximum TCP connections to one origin; 0 keeps Chromium's own limit (6).
    ///
    /// Raising it is an observable deviation from the browser being imitated
    /// and the only knob that moves the per-origin throughput ceiling, which is
    /// this number times 1/RTT. Documented at mn_engine_config_t.
    pub max_sockets_per_group: u32,
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
            host_mapping_rules: None,
            dns_nameservers: None,
            doh_template: None,
            hsts_bypass_list: None,
            proxy_chain: None,
            dns_secure_mode: 0,
            dns_over_tls: 0,
            dns_rotate: 0,
            dns_padding: 0,
            platform: Platform::Auto,
            // False by default. The facade injects the captured set itself
            // (bindings/python .../sessions.py _browser_headers), because it is
            // the layer that knows whether the caller asked for a browser profile
            // at all, and because the captured values must be ordered before
            // Pragma/Cache-Control rather than appended after them. Turning the
            // Core path on as well would merge the same set twice. The field
            // exists so a future binding can hand the job down; see the note at
            // mn_engine_config_t.inject_client_hints.
            inject_client_hints: false,
            max_sockets_per_group: 0,
        }
    }
}

/// The capability bits a Core artifact was compiled with.
///
/// A newtype rather than a bare `u32`: the bits are a mask whose meaning lives
/// in the ABI, and a caller comparing raw integers would silently accept a bit a
/// future revision renumbers. Unknown bits are preserved and simply never match
/// an accessor here.
///
/// Bit 1 is reserved and never set: the ABI has no field that can carry a
/// bracketed multi-hop chain. See `core/abi/minicronet.h`.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct CoreCapabilities(u32);

impl CoreCapabilities {
    /// The artifact can speak to a `quic://` proxy.
    pub fn quic_proxy(self) -> bool {
        self.0 & sys::mn_capability_t::MN_CAPABILITY_QUIC_PROXY.0 != 0
    }

    /// The raw mask, for a binding that wants to forward unknown bits.
    pub fn bits(self) -> u32 {
        self.0
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
        let host_mapping_rules = bytes(config.host_mapping_rules.as_deref());
        let dns_nameservers = bytes(config.dns_nameservers.as_deref());
        let doh_template = bytes(config.doh_template.as_deref());
        let hsts_bypass_list = bytes(config.hsts_bypass_list.as_deref());
        let proxy_chain = bytes(config.proxy_chain.as_deref());
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
            custom_ca_pem: ffi_bytes(ca),
            custom_ca_pem_length: ca.len(),
            host_mapping_rules: ptr_or_null(&host_mapping_rules),
            host_mapping_rules_length: host_mapping_rules.len(),
            dns_nameservers: ptr_or_null(&dns_nameservers),
            dns_nameservers_length: dns_nameservers.len(),
            doh_template: ptr_or_null(&doh_template),
            doh_template_length: doh_template.len(),
            hsts_bypass_list: ptr_or_null(&hsts_bypass_list),
            hsts_bypass_list_length: hsts_bypass_list.len(),
            proxy_chain: ptr_or_null(&proxy_chain),
            proxy_chain_length: proxy_chain.len(),
            dns_secure_mode: config.dns_secure_mode,
            dns_over_tls: config.dns_over_tls,
            dns_rotate: config.dns_rotate,
            dns_padding: config.dns_padding,
            cookie_samesite_override: 0,
            inject_client_hints: u8::from(config.inject_client_hints),
            platform: config.platform.into(),
            max_sockets_per_group: config.max_sockets_per_group,
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

    /// The User-Agent group and sec-ch-ua-platform value this artifact emits
    /// for *platform*, as the Core computes them.
    ///
    /// A binding must not derive these itself. The Core decides them from the
    /// flags it was compiled with (BUILDFLAG(IS_WIN/IS_MAC), ARCH_CPU_*), while a
    /// binding can only see the host it runs on, and on one x86_64 host serving a
    /// 32-bit Core the two answers differ. Reading them here makes the two sides
    /// agree by construction rather than by coincidence.
    pub fn platform_identity(platform: Platform) -> Result<(String, String), Error> {
        let raw = sys::mn_platform_t::from(platform);
        let mut group_ptr: *const i8 = std::ptr::null();
        let mut group_len: u32 = 0;
        let result =
            unsafe { sys::mn_platform_user_agent_group(raw, &mut group_ptr, &mut group_len) };
        if result != sys::mn_result_t::MN_OK {
            return Err(result.into());
        }
        let mut hint_ptr: *const i8 = std::ptr::null();
        let mut hint_len: u32 = 0;
        let result = unsafe { sys::mn_platform_hint(raw, &mut hint_ptr, &mut hint_len) };
        if result != sys::mn_result_t::MN_OK {
            return Err(result.into());
        }
        // The pointer is Core-owned static storage, so a copy is the only sound
        // thing to hand back: keeping the pointer would tie the caller to a
        // lifetime the ABI never promises.
        //
        // A null pointer would be UB for from_raw_parts even with a zero length
        // (the pointer must be non-null and aligned). MN_OK is documented to imply
        // a valid pointer, but building a slice from one without checking turns a
        // hypothetical Core bug into undefined behaviour in the binding.
        if group_ptr.is_null() || hint_ptr.is_null() {
            return Err(Error::InitializationFailed);
        }
        let group =
            unsafe { std::slice::from_raw_parts(group_ptr.cast::<u8>(), group_len as usize) };
        let hint = unsafe { std::slice::from_raw_parts(hint_ptr.cast::<u8>(), hint_len as usize) };
        Ok((
            String::from_utf8_lossy(group).into_owned(),
            String::from_utf8_lossy(hint).into_owned(),
        ))
    }

    /// What this Core artifact was compiled with.
    ///
    /// Readable before any engine exists: the bits describe the library, not a
    /// connection. Callers use them to decide whether a feature is worth
    /// attempting, instead of making a request and interpreting its error.
    pub fn core_capabilities() -> Result<CoreCapabilities, Error> {
        let mut flags: u32 = 0;
        let result = unsafe { sys::mn_engine_capabilities(&mut flags) };
        if result != sys::mn_result_t::MN_OK {
            return Err(result.into());
        }
        Ok(CoreCapabilities(flags))
    }

    pub fn request(&self, config: RequestConfig) -> Result<Request, Error> {
        Request::new(self, config)
    }

    pub fn websocket(&self, config: WebSocketConfig) -> Result<WebSocket, Error> {
        WebSocket::new(self, config)
    }

    pub(crate) fn raw(&self) -> *mut sys::mn_engine_t {
        self.0.raw.as_ptr()
    }

    /// Reads the engine's Chromium cookie store.
    ///
    /// `url` of `None` returns every cookie; `Some` returns the cookies a
    /// request to that URL would carry. Both block until the Core answers.
    ///
    /// The strings a returned cookie points at belong to the engine's handle
    /// and stay valid until the next cookies call or until the engine is
    /// released, so every field is copied out here: a caller must not have to
    /// reason about that lifetime.
    pub fn cookies(&self, url: Option<&str>) -> Result<Vec<Cookie>, Error> {
        let mut count = 0usize;
        let url_ptr = url.map(|v| v.as_ptr().cast::<i8>());
        let url_len = url.map_or(0, str::len);
        let mut result = unsafe {
            sys::mn_engine_cookies_get(
                self.raw(),
                url_ptr.unwrap_or(std::ptr::null()),
                url_len,
                std::ptr::null_mut(),
                0,
                &mut count,
            )
        };
        if result != sys::mn_result_t::MN_OK {
            return Err(result.into());
        }
        if count == 0 {
            return Ok(Vec::new());
        }
        let mut raw = vec![sys::mn_cookie_t::default(); count];
        result = unsafe {
            sys::mn_engine_cookies_get(
                self.raw(),
                url_ptr.unwrap_or(std::ptr::null()),
                url_len,
                raw.as_mut_ptr(),
                raw.len(),
                &mut count,
            )
        };
        if result != sys::mn_result_t::MN_OK {
            return Err(result.into());
        }
        raw.truncate(count.min(raw.len()));
        Ok(raw.into_iter().map(Cookie::from_raw).collect())
    }

    /// Writes one cookie through the store's own validation.
    ///
    /// `source_url` decides domain matching; a cookie the store rejects comes
    /// back as an error rather than being silently dropped.
    pub fn set_cookie(&self, cookie: &Cookie, source_url: &str) -> Result<(), Error> {
        let raw = cookie.to_raw();
        let result = unsafe {
            sys::mn_engine_cookies_set(
                self.raw(),
                &raw,
                source_url.as_ptr().cast(),
                source_url.len(),
                0,
            )
        };
        if result == sys::mn_result_t::MN_OK {
            Ok(())
        } else {
            Err(result.into())
        }
    }

    /// Deletes every cookie matching the triple and returns how many went.
    pub fn delete_cookie(&self, domain: &str, path: &str, name: &str) -> Result<usize, Error> {
        let mut removed = 0usize;
        let result = unsafe {
            sys::mn_engine_cookies_delete(
                self.raw(),
                domain.as_ptr().cast(),
                domain.len(),
                path.as_ptr().cast(),
                path.len(),
                name.as_ptr().cast(),
                name.len(),
                &mut removed,
            )
        };
        if result == sys::mn_result_t::MN_OK {
            Ok(removed)
        } else {
            Err(result.into())
        }
    }

    /// Empties the store.
    pub fn clear_cookies(&self) -> Result<(), Error> {
        let result = unsafe { sys::mn_engine_cookies_clear(self.raw()) };
        if result == sys::mn_result_t::MN_OK {
            Ok(())
        } else {
            Err(result.into())
        }
    }

    /// Registers the client certificate this engine presents to one server.
    ///
    /// `cert_pem` is a chain of PEM `CERTIFICATE` blocks, `key_pem` a single
    /// unencrypted PEM private key. The certificate is sent only when the server
    /// issues a CertificateRequest, so it cannot change the ClientHello of any
    /// other connection and cannot disturb a pinned TLS fingerprint.
    ///
    /// Replacing a certificate makes the Core refresh that origin's connection
    /// group, so the idle connections opened under the old certificate are not
    /// reused. Without that refresh the next request would keep presenting the
    /// previous certificate to the server.
    ///
    /// An empty `cert_pem` or `key_pem`, a malformed PEM block, an encrypted
    /// key and an empty host all come back as `ErrorKind::InvalidArgument`.
    /// Sending no certificate when one was asked for is a silent authentication
    /// failure, so every one of those cases fails loudly instead.
    pub fn set_client_certificate(
        &self,
        host: &str,
        port: u16,
        cert_pem: &[u8],
        key_pem: &[u8],
    ) -> Result<(), Error> {
        if host.is_empty() {
            return Err(Error::from(sys::mn_result_t::MN_ERROR_INVALID_ARGUMENT));
        }
        let result = unsafe {
            sys::mn_engine_set_client_certificate(
                self.raw(),
                host.as_ptr().cast(),
                host.len(),
                port,
                ptr_or_null(cert_pem).cast(),
                cert_pem.len(),
                ptr_or_null(key_pem).cast(),
                key_pem.len(),
            )
        };
        if result == sys::mn_result_t::MN_OK {
            Ok(())
        } else {
            Err(result.into())
        }
    }

    /// Forgets the certificate registered for (host, port).
    ///
    /// Succeeds even when none was registered, so unregistering is idempotent
    /// and a caller does not have to track what it set.
    pub fn clear_client_certificate(&self, host: &str, port: u16) -> Result<(), Error> {
        if host.is_empty() {
            return Err(Error::from(sys::mn_result_t::MN_ERROR_INVALID_ARGUMENT));
        }
        let result = unsafe {
            sys::mn_engine_clear_client_certificate(
                self.raw(),
                host.as_ptr().cast(),
                host.len(),
                port,
            )
        };
        if result == sys::mn_result_t::MN_OK {
            Ok(())
        } else {
            Err(result.into())
        }
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

pub(crate) fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner)
}

/// How long one blocking park lasts before the loop re-checks the state.
///
/// `Condvar::wait` parks the calling thread with no deadline and no escape
/// hatch: if the Core never delivers its terminal callback, the caller blocks
/// forever and cannot observe that nothing is coming. The Core normally does
/// deliver one, and its own deadline timer covers the `timeout_ms_ > 0` case,
/// but a request with no deadline has no Core-side timer at all, and a callback
/// lost to a dropped consumer is a failure mode this project has already hit
/// once (the abandoned-body-stream stall).
///
/// This is a re-check interval, not the request deadline: the deadline stays
/// the Core's job (`timeout_ms`). Bounding one park means a lost wakeup or a
/// Core that stopped mid-flight cannot pin the caller permanently, while a
/// healthy request notices no difference -- the loop wakes on `notify_all`
/// long before the slice elapses.
pub(crate) const WAIT_SLICE: std::time::Duration = std::time::Duration::from_millis(250);

/// Converts a deadline into the ABI's whole-millisecond field.
///
/// `0` is the ABI's "no deadline" value -- `core/source/request.cc` only arms
/// the timer `if (timeout_ms_ > 0)` -- so a positive duration must never
/// truncate to it. `as_millis()` truncates, which turned every sub-millisecond
/// deadline into "wait forever": exactly the opposite of what the caller asked
/// for. Rounding a positive duration up to 1 ms keeps the deadline a deadline
/// and costs at most one millisecond of extra waiting.
///
/// Only `None` maps to 0. Every supplied deadline maps to at least 1 ms,
/// including `Duration::ZERO`: a caller that passes a deadline wants the
/// request to stop, and `ZERO` is the strongest form of that request, so
/// encoding it as "no deadline" inverted its meaning. The Python facade rejects
/// `timeout=0` for the same reason; this is the Rust-side equivalent, and it
/// also makes the mapping monotonic -- `Some(_)` is always >= 1, `None` is 0.
pub(crate) fn timeout_ms(timeout: Option<std::time::Duration>) -> u64 {
    let Some(value) = timeout else {
        return 0;
    };
    let millis = value.as_millis().min(u128::from(u64::MAX)) as u64;
    millis.max(1)
}

pub(crate) fn ffi_bytes(data: &[u8]) -> *const u8 {
    if data.is_empty() {
        std::ptr::null()
    } else {
        data.as_ptr()
    }
}

/// Turns a Core return code into `Result`, mapping every non-OK value through
/// `Error::from`. Both the request and WebSocket layers call this.
pub(crate) fn result(value: sys::mn_result_t) -> Result<(), Error> {
    if value == sys::mn_result_t::MN_OK {
        Ok(())
    } else {
        Err(value.into())
    }
}

/// Copies a Core callback buffer. A zero length is answered with an empty Vec
/// rather than `slice::from_raw_parts`, which requires a non-null, aligned
/// pointer even for zero elements; the Core passes null for empty payloads.
pub(crate) fn copy_bytes(data: *const u8, length: usize) -> Vec<u8> {
    if length == 0 {
        Vec::new()
    } else {
        unsafe { std::slice::from_raw_parts(data, length) }.to_vec()
    }
}

pub(crate) fn copy_string(data: *const i8, length: usize) -> String {
    String::from_utf8_lossy(&copy_bytes(data.cast(), length)).into_owned()
}
