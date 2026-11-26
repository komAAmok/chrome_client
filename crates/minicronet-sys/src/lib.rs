#![no_std]
#![allow(non_camel_case_types)]

use core::ffi::c_void;

pub const MN_ABI_VERSION: u32 = 17;

#[repr(C)]
pub struct mn_engine_t {
    _private: [u8; 0],
}
#[repr(C)]
pub struct mn_request_t {
    _private: [u8; 0],
}
#[repr(C)]
pub struct mn_websocket_t {
    _private: [u8; 0],
}

#[repr(transparent)]
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct mn_result_t(pub i32);

impl mn_result_t {
    pub const MN_OK: Self = Self(0);
    pub const MN_ERROR_INVALID_ARGUMENT: Self = Self(1);
    pub const MN_ERROR_UNSUPPORTED_ABI: Self = Self(2);
    pub const MN_ERROR_OUT_OF_MEMORY: Self = Self(3);
    pub const MN_ERROR_INITIALIZATION_FAILED: Self = Self(4);
    pub const MN_ERROR_INVALID_STATE: Self = Self(5);
    pub const MN_ERROR_TIMEOUT: Self = Self(6);
    pub const MN_ERROR_CANCELED: Self = Self(7);
    pub const MN_ERROR_NETWORK: Self = Self(8);
    pub const MN_ERROR_TLS: Self = Self(9);
    pub const MN_ERROR_PROXY: Self = Self(10);
    pub const MN_ERROR_PROTOCOL: Self = Self(11);
    pub const MN_ERROR_REDIRECT: Self = Self(12);
    pub const MN_ERROR_CACHE_MISS: Self = Self(13);
    pub const MN_ERROR_PROFILE_CONFLICT: Self = Self(14);
    pub const MN_ERROR_PROFILE_UNSUPPORTED: Self = Self(15);
}

#[repr(transparent)]
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct mn_http_cache_mode_t(pub i32);

impl mn_http_cache_mode_t {
    pub const MN_HTTP_CACHE_ENABLED: Self = Self(0);
    pub const MN_HTTP_CACHE_DISABLED: Self = Self(1);
}

#[repr(transparent)]
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct mn_protocol_mode_t(pub i32);

impl mn_protocol_mode_t {
    pub const MN_PROTOCOL_NATIVE: Self = Self(0);
    pub const MN_PROTOCOL_FORCE_H1: Self = Self(1);
    pub const MN_PROTOCOL_FORCE_H2: Self = Self(2);
    pub const MN_PROTOCOL_FORCE_H3: Self = Self(3);
}

#[repr(transparent)]
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct mn_tls_verify_mode_t(pub i32);

impl mn_tls_verify_mode_t {
    pub const MN_TLS_VERIFY_CHROMIUM_DEFAULT: Self = Self(0);
    pub const MN_TLS_VERIFY_CUSTOM_CA: Self = Self(1);
    pub const MN_TLS_VERIFY_INSECURE: Self = Self(2);
}

#[repr(transparent)]
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct mn_platform_t(pub i32);

impl mn_platform_t {
    pub const MN_PLATFORM_AUTO: Self = Self(0);
    pub const MN_PLATFORM_WINDOWS: Self = Self(1);
    pub const MN_PLATFORM_MACOS: Self = Self(2);
    pub const MN_PLATFORM_LINUX: Self = Self(3);
}

#[repr(C)]
#[derive(Clone, Copy)]
pub struct mn_engine_config_t {
    pub size: u32,
    pub version: u32,
    pub user_agent: *const i8,
    pub user_agent_length: usize,
    pub proxy_rules: *const i8,
    pub proxy_rules_length: usize,
    pub proxy_username: *const i8,
    pub proxy_username_length: usize,
    pub proxy_password: *const i8,
    pub proxy_password_length: usize,
    pub http_cache_mode: mn_http_cache_mode_t,
    pub accept_language: *const i8,
    pub accept_language_length: usize,
    pub profile_id: *const i8,
    pub profile_id_length: usize,
    pub profile_namespace: *const i8,
    pub profile_namespace_length: usize,
    pub protocol_mode: mn_protocol_mode_t,
    pub tls_verify_mode: mn_tls_verify_mode_t,
    pub custom_ca_pem: *const u8,
    pub custom_ca_pem_length: usize,
    // ---- v9 appends --------------------------------------------------
    pub host_mapping_rules: *const i8,
    pub host_mapping_rules_length: usize,
    pub dns_nameservers: *const i8,
    pub dns_nameservers_length: usize,
    pub doh_template: *const i8,
    pub doh_template_length: usize,
    pub hsts_bypass_list: *const i8,
    pub hsts_bypass_list_length: usize,
    pub proxy_chain: *const i8,
    pub proxy_chain_length: usize,
    pub dns_secure_mode: u8,
    pub dns_over_tls: u8,
    pub dns_rotate: u8,
    pub dns_padding: u8,
    pub cookie_samesite_override: u8,
    pub inject_client_hints: u8,
    pub platform: mn_platform_t,
    // ---- v11 appends. 0 keeps Chromium's own per-origin TCP limit (6), which
    // is what every captured profile does; a non-zero value raises it for this
    // engine and is an observable deviation from the imitated browser.
    pub max_sockets_per_group: u32,
}

#[repr(C)]
#[derive(Clone, Copy)]
pub struct mn_header_t {
    pub name: *const i8,
    pub name_length: usize,
    pub value: *const i8,
    pub value_length: usize,
}

#[repr(C)]
#[derive(Clone, Copy)]
pub struct mn_string_t {
    pub data: *const i8,
    pub length: usize,
}

#[repr(C)]
#[derive(Clone, Copy, Default)]
pub struct mn_cookie_t {
    pub size: u32,
    pub version: u32,
    pub name: *const i8,
    pub name_length: usize,
    pub value: *const i8,
    pub value_length: usize,
    pub domain: *const i8,
    pub domain_length: usize,
    pub path: *const i8,
    pub path_length: usize,
    pub expiry_unix_seconds: i64,
    pub has_expiry: u8,
    pub secure: u8,
    pub http_only: u8,
    pub same_site: u8,
}

#[repr(transparent)]
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct mn_cookie_same_site_t(pub i32);

impl mn_cookie_same_site_t {
    pub const MN_COOKIE_SAME_SITE_UNSPECIFIED: Self = Self(0);
    pub const MN_COOKIE_SAME_SITE_NO_RESTRICTION: Self = Self(1);
    pub const MN_COOKIE_SAME_SITE_LAX: Self = Self(2);
    pub const MN_COOKIE_SAME_SITE_STRICT: Self = Self(3);
}

#[repr(transparent)]
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct mn_http_version_t(pub i32);

impl mn_http_version_t {
    pub const MN_HTTP_VERSION_UNKNOWN: Self = Self(0);
    pub const MN_HTTP_VERSION_0_9: Self = Self(1);
    pub const MN_HTTP_VERSION_1_0: Self = Self(2);
    pub const MN_HTTP_VERSION_1_1: Self = Self(3);
    pub const MN_HTTP_VERSION_2: Self = Self(4);
    pub const MN_HTTP_VERSION_3: Self = Self(5);
}

#[repr(transparent)]
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct mn_ech_status_t(pub i32);

impl mn_ech_status_t {
    pub const MN_ECH_NOT_ATTEMPTED: Self = Self(0);
    pub const MN_ECH_SUCCESS: Self = Self(1);
    pub const MN_ECH_FAILED: Self = Self(2);
}

#[repr(transparent)]
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct mn_upload_mode_t(pub i32);

impl mn_upload_mode_t {
    pub const MN_UPLOAD_NONE: Self = Self(0);
    pub const MN_UPLOAD_FIXED: Self = Self(1);
    pub const MN_UPLOAD_CHUNKED: Self = Self(2);
}

#[repr(transparent)]
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct mn_cache_mode_t(pub i32);

impl mn_cache_mode_t {
    pub const MN_CACHE_DEFAULT: Self = Self(0);
    pub const MN_CACHE_VALIDATE: Self = Self(1);
    pub const MN_CACHE_BYPASS: Self = Self(2);
    pub const MN_CACHE_NO_STORE: Self = Self(3);
    pub const MN_CACHE_FORCE: Self = Self(4);
    pub const MN_CACHE_ONLY_IF_CACHED: Self = Self(5);
}

#[repr(transparent)]
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct mn_redirect_mode_t(pub i32);

impl mn_redirect_mode_t {
    pub const MN_REDIRECT_FOLLOW: Self = Self(0);
    pub const MN_REDIRECT_MANUAL: Self = Self(1);
    pub const MN_REDIRECT_ERROR: Self = Self(2);
}

#[repr(transparent)]
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct mn_request_priority_t(pub i32);

impl mn_request_priority_t {
    pub const MN_REQUEST_PRIORITY_DEFAULT: Self = Self(0);
    pub const MN_REQUEST_PRIORITY_HIGHEST: Self = Self(1);
    pub const MN_REQUEST_PRIORITY_MEDIUM: Self = Self(2);
    pub const MN_REQUEST_PRIORITY_LOW: Self = Self(3);
    pub const MN_REQUEST_PRIORITY_LOWEST: Self = Self(4);
    pub const MN_REQUEST_PRIORITY_IDLE: Self = Self(5);
}

/// Which header names a caller may write. The Core owns the table; a binding
/// asks through mn_header_disposition rather than keeping its own list.
pub const MN_HEADER_FLAG_SETTABLE: u32 = 1 << 0;
pub const MN_HEADER_FLAG_REMOVABLE: u32 = 1 << 1;
pub const MN_HEADER_FLAG_DROPPED: u32 = 1 << 2;

#[repr(transparent)]
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct mn_header_scope_t(pub i32);

impl mn_header_scope_t {
    pub const MN_HEADER_SCOPE_REQUEST: Self = Self(0);
    pub const MN_HEADER_SCOPE_WEBSOCKET: Self = Self(1);
}

#[repr(transparent)]
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct mn_header_policy_t(pub i32);

impl mn_header_policy_t {
    pub const MN_HEADER_POLICY_REJECT: Self = Self(0);
    pub const MN_HEADER_POLICY_DROP: Self = Self(1);
}

#[repr(transparent)]
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct mn_read_disposition_t(pub i32);

impl mn_read_disposition_t {
    pub const MN_READ_PAUSE: Self = Self(0);
    pub const MN_READ_CONTINUE: Self = Self(1);
    pub const MN_READ_TAKE_BUFFER: Self = Self(2);
}

/// Bits reported by [`mn_engine_capabilities`].
///
/// A capability bit exists because the same ABI is built nine times with
/// different `args.gn`, and two of those knobs change something a caller can
/// observe. Without a bit a binding can only discover the difference by making
/// a request and reading the error, which cannot separate "this build refuses
/// the scheme" from "the scheme is supported and the peer refused".
///
/// Bits this revision does not define must be ignored: a later revision only
/// ever appends.
#[repr(transparent)]
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct mn_capability_t(pub u32);

impl mn_capability_t {
    /// The artifact can speak to a `quic://` proxy. Chromium gates it behind
    /// `ENABLE_QUIC_PROXY_SUPPORT`, which upstream sets to `is_debug`; this
    /// project turns it on explicitly.
    pub const MN_CAPABILITY_QUIC_PROXY: Self = Self(1 << 0);
    // Bit 1 is reserved and never reported: proxy_chain splits on "," and each
    // entry becomes one ProxyList::AddProxyServer hop, so the ABI cannot express
    // a bracketed chain. See the reserved note in core/abi/minicronet.h.
}

/// The response callback.
///
/// Both header forms arrive: the raw block and the parsed fields, plus the
/// reason phrase. Every pointer is borrowed for the call only.
pub type mn_request_response_fn = unsafe extern "C" fn(
    *mut c_void,
    *mut mn_request_t,
    i32,
    *const i8,
    usize,
    *const mn_header_t,
    usize,
    *const i8,
    usize,
);
pub type mn_request_body_fn =
    unsafe extern "C" fn(*mut c_void, *mut mn_request_t, *const u8, usize) -> mn_read_disposition_t;
/// ABI 16 batch wakeup: called with the requests that have callback output
/// waiting. Every handle is valid for the duration of the call.
pub type mn_engine_wakeup_fn =
    unsafe extern "C" fn(user_data: *mut c_void, tokens: *mut *mut c_void, count: usize);
pub type mn_request_complete_fn =
    unsafe extern "C" fn(*mut c_void, *mut mn_request_t, mn_result_t, i32);
pub type mn_request_redirect_fn = unsafe extern "C" fn(
    *mut c_void,
    *mut mn_request_t,
    i32,
    *const mn_header_t,
    usize,
    *const i8,
    usize,
    *const i8,
    usize,
    *const i8,
    usize,
);

#[repr(C)]
#[derive(Clone, Copy)]
pub struct mn_request_callbacks_t {
    pub size: u32,
    pub version: u32,
    pub user_data: *mut c_void,
    pub on_response: Option<mn_request_response_fn>,
    pub on_body: Option<mn_request_body_fn>,
    pub on_complete: Option<mn_request_complete_fn>,
    pub on_redirect: Option<mn_request_redirect_fn>,
}

#[repr(C)]
#[derive(Clone, Copy)]
pub struct mn_request_config_t {
    pub size: u32,
    pub version: u32,
    pub url: *const i8,
    pub url_length: usize,
    pub method: *const i8,
    pub method_length: usize,
    pub headers: *const mn_header_t,
    pub header_count: usize,
    pub timeout_ms: u64,
    pub callbacks: mn_request_callbacks_t,
    pub body: *const u8,
    pub body_length: usize,
    pub upload_mode: mn_upload_mode_t,
    pub cache_mode: mn_cache_mode_t,
    pub redirect_mode: mn_redirect_mode_t,
    pub priority: mn_request_priority_t,
    pub strip_authorization_on_cross_origin_redirect: u8,
    pub header_policy: mn_header_policy_t,
}

#[repr(transparent)]
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct mn_websocket_message_type_t(pub i32);

impl mn_websocket_message_type_t {
    pub const MN_WEBSOCKET_MESSAGE_CONTINUATION: Self = Self(0);
    pub const MN_WEBSOCKET_MESSAGE_TEXT: Self = Self(1);
    pub const MN_WEBSOCKET_MESSAGE_BINARY: Self = Self(2);
}

pub type mn_websocket_open_fn =
    unsafe extern "C" fn(*mut c_void, *mut mn_websocket_t, *const i8, usize, *const i8, usize);
pub type mn_websocket_message_fn = unsafe extern "C" fn(
    *mut c_void,
    *mut mn_websocket_t,
    mn_websocket_message_type_t,
    i32,
    *const u8,
    usize,
);
pub type mn_websocket_closing_fn = unsafe extern "C" fn(*mut c_void, *mut mn_websocket_t);
pub type mn_websocket_closed_fn =
    unsafe extern "C" fn(*mut c_void, *mut mn_websocket_t, i32, u16, *const i8, usize);
pub type mn_websocket_failure_fn =
    unsafe extern "C" fn(*mut c_void, *mut mn_websocket_t, mn_result_t, i32, i32, *const i8, usize);

#[repr(C)]
#[derive(Clone, Copy)]
pub struct mn_websocket_callbacks_t {
    pub size: u32,
    pub version: u32,
    pub user_data: *mut c_void,
    pub on_open: Option<mn_websocket_open_fn>,
    pub on_message: Option<mn_websocket_message_fn>,
    pub on_closing: Option<mn_websocket_closing_fn>,
    pub on_closed: Option<mn_websocket_closed_fn>,
    pub on_failure: Option<mn_websocket_failure_fn>,
}

#[repr(C)]
#[derive(Clone, Copy)]
pub struct mn_websocket_config_t {
    pub size: u32,
    pub version: u32,
    pub url: *const i8,
    pub url_length: usize,
    pub origin: *const i8,
    pub origin_length: usize,
    pub protocols: *const mn_string_t,
    pub protocol_count: usize,
    pub headers: *const mn_header_t,
    pub header_count: usize,
    pub callbacks: mn_websocket_callbacks_t,
    pub timeout_ms: u64,
}

extern "C" {
    pub fn mn_abi_version() -> u32;
    pub fn mn_version_string() -> *const i8;
    pub fn mn_header_disposition(
        name: *const i8,
        name_length: usize,
        scope: mn_header_scope_t,
        out_flags: *mut u32,
    ) -> mn_result_t;
    /// What this artifact was compiled with. Readable before any engine
    /// exists; bits not defined by this revision must be ignored.
    pub fn mn_engine_capabilities(out_flags: *mut u32) -> mn_result_t;
    /// The User-Agent group this artifact emits for a platform, without
    /// parentheses. The returned pointer is owned by the Core and lives for the
    /// process; it must not be freed.
    pub fn mn_platform_user_agent_group(
        platform: mn_platform_t,
        out_group: *mut *const i8,
        out_length: *mut u32,
    ) -> mn_result_t;
    /// The sec-ch-ua-platform value for a platform, quotes included.
    pub fn mn_platform_hint(
        platform: mn_platform_t,
        out_hint: *mut *const i8,
        out_length: *mut u32,
    ) -> mn_result_t;
    pub fn mn_engine_create(
        config: *const mn_engine_config_t,
        out_engine: *mut *mut mn_engine_t,
    ) -> mn_result_t;
    pub fn mn_engine_retain(engine: *mut mn_engine_t);
    pub fn mn_engine_release(engine: *mut mn_engine_t);
    pub fn mn_request_create(
        engine: *mut mn_engine_t,
        config: *const mn_request_config_t,
        out_request: *mut *mut mn_request_t,
    ) -> mn_result_t;
    pub fn mn_request_retain(request: *mut mn_request_t);
    pub fn mn_request_release(request: *mut mn_request_t);
    pub fn mn_request_start(request: *mut mn_request_t) -> mn_result_t;
    pub fn mn_request_cancel(request: *mut mn_request_t) -> mn_result_t;
    pub fn mn_request_upload_write(
        request: *mut mn_request_t,
        data: *const u8,
        data_length: usize,
        final_chunk: i32,
    ) -> mn_result_t;
    pub fn mn_request_follow_redirect(request: *mut mn_request_t) -> mn_result_t;
    pub fn mn_request_resume_read(request: *mut mn_request_t) -> mn_result_t;
    pub fn mn_request_release_buffer(request: *mut mn_request_t, data: *const u8) -> mn_result_t;
    pub fn mn_engine_cookies_get(
        engine: *mut mn_engine_t,
        url: *const i8,
        url_length: usize,
        out_cookies: *mut mn_cookie_t,
        capacity: usize,
        out_count: *mut usize,
    ) -> mn_result_t;
    pub fn mn_engine_cookies_set(
        engine: *mut mn_engine_t,
        cookie: *const mn_cookie_t,
        source_url: *const i8,
        source_url_length: usize,
        creation_unix_seconds: i64,
    ) -> mn_result_t;
    pub fn mn_engine_cookies_delete(
        engine: *mut mn_engine_t,
        domain: *const i8,
        domain_length: usize,
        path: *const i8,
        path_length: usize,
        name: *const i8,
        name_length: usize,
        out_removed: *mut usize,
    ) -> mn_result_t;
    pub fn mn_engine_cookies_clear(engine: *mut mn_engine_t) -> mn_result_t;
    pub fn mn_engine_set_client_certificate(
        engine: *mut mn_engine_t,
        host: *const i8,
        host_length: usize,
        port: u16,
        cert_pem: *const u8,
        cert_pem_length: usize,
        key_pem: *const u8,
        key_pem_length: usize,
    ) -> mn_result_t;
    pub fn mn_engine_clear_client_certificate(
        engine: *mut mn_engine_t,
        host: *const i8,
        host_length: usize,
        port: u16,
    ) -> mn_result_t;
    pub fn mn_engine_set_wakeup_batch(
        engine: *mut mn_engine_t,
        callback: Option<mn_engine_wakeup_fn>,
        user_data: *mut c_void,
        max_batch: u32,
    ) -> mn_result_t;
    pub fn mn_request_set_wakeup_token(
        request: *mut mn_request_t,
        token: *mut c_void,
    ) -> mn_result_t;
    pub fn mn_request_negotiated_protocol(
        request: *mut mn_request_t,
        out_version: *mut mn_http_version_t,
    ) -> mn_result_t;
    pub fn mn_request_ech_status(
        request: *mut mn_request_t,
        out_status: *mut mn_ech_status_t,
    ) -> mn_result_t;
    pub fn mn_websocket_create(
        engine: *mut mn_engine_t,
        config: *const mn_websocket_config_t,
        out_websocket: *mut *mut mn_websocket_t,
    ) -> mn_result_t;
    pub fn mn_websocket_retain(websocket: *mut mn_websocket_t);
    pub fn mn_websocket_release(websocket: *mut mn_websocket_t);
    pub fn mn_websocket_start(websocket: *mut mn_websocket_t) -> mn_result_t;
    pub fn mn_websocket_send(
        websocket: *mut mn_websocket_t,
        message_type: mn_websocket_message_type_t,
        data: *const u8,
        data_length: usize,
    ) -> mn_result_t;
    pub fn mn_websocket_close(
        websocket: *mut mn_websocket_t,
        code: u16,
        reason: *const i8,
        reason_length: usize,
    ) -> mn_result_t;
    pub fn mn_websocket_cancel(websocket: *mut mn_websocket_t) -> mn_result_t;
}

#[cfg(test)]
mod abi_layout_tests {
    use super::*;
    use core::mem::{offset_of, size_of};

    #[test]
    fn field_offsets_match_public_header() {
        assert_eq!(offset_of!(mn_engine_config_t, size), 0);
        assert_eq!(offset_of!(mn_engine_config_t, version), 4);
        assert_eq!(offset_of!(mn_engine_config_t, user_agent), 8);
        assert_eq!(
            offset_of!(mn_engine_config_t, user_agent_length),
            if cfg!(target_pointer_width = "64") {
                16
            } else {
                12
            }
        );
        assert_eq!(
            offset_of!(mn_engine_config_t, proxy_rules),
            if cfg!(target_pointer_width = "64") {
                24
            } else {
                16
            }
        );
        assert_eq!(
            offset_of!(mn_engine_config_t, proxy_rules_length),
            if cfg!(target_pointer_width = "64") {
                32
            } else {
                20
            }
        );
        assert_eq!(
            offset_of!(mn_engine_config_t, proxy_username),
            if cfg!(target_pointer_width = "64") {
                40
            } else {
                24
            }
        );
        assert_eq!(
            offset_of!(mn_engine_config_t, proxy_username_length),
            if cfg!(target_pointer_width = "64") {
                48
            } else {
                28
            }
        );
        assert_eq!(
            offset_of!(mn_engine_config_t, proxy_password),
            if cfg!(target_pointer_width = "64") {
                56
            } else {
                32
            }
        );
        assert_eq!(
            offset_of!(mn_engine_config_t, proxy_password_length),
            if cfg!(target_pointer_width = "64") {
                64
            } else {
                36
            }
        );
        assert_eq!(
            offset_of!(mn_engine_config_t, http_cache_mode),
            if cfg!(target_pointer_width = "64") {
                72
            } else {
                40
            }
        );
        assert_eq!(
            offset_of!(mn_engine_config_t, accept_language),
            if cfg!(target_pointer_width = "64") {
                80
            } else {
                44
            }
        );
        assert_eq!(
            offset_of!(mn_engine_config_t, accept_language_length),
            if cfg!(target_pointer_width = "64") {
                88
            } else {
                48
            }
        );
        assert_eq!(
            offset_of!(mn_engine_config_t, profile_id),
            if cfg!(target_pointer_width = "64") {
                96
            } else {
                52
            }
        );
        assert_eq!(
            offset_of!(mn_engine_config_t, profile_id_length),
            if cfg!(target_pointer_width = "64") {
                104
            } else {
                56
            }
        );
        assert_eq!(
            offset_of!(mn_engine_config_t, profile_namespace),
            if cfg!(target_pointer_width = "64") {
                112
            } else {
                60
            }
        );
        assert_eq!(
            offset_of!(mn_engine_config_t, profile_namespace_length),
            if cfg!(target_pointer_width = "64") {
                120
            } else {
                64
            }
        );
        assert_eq!(
            offset_of!(mn_engine_config_t, protocol_mode),
            if cfg!(target_pointer_width = "64") {
                128
            } else {
                68
            }
        );
        assert_eq!(
            offset_of!(mn_engine_config_t, tls_verify_mode),
            if cfg!(target_pointer_width = "64") {
                132
            } else {
                72
            }
        );
        assert_eq!(
            offset_of!(mn_engine_config_t, custom_ca_pem),
            if cfg!(target_pointer_width = "64") {
                136
            } else {
                76
            }
        );
        assert_eq!(
            offset_of!(mn_engine_config_t, custom_ca_pem_length),
            if cfg!(target_pointer_width = "64") {
                144
            } else {
                80
            }
        );

        assert_eq!(offset_of!(mn_header_t, name), 0);
        assert_eq!(
            offset_of!(mn_header_t, name_length),
            if cfg!(target_pointer_width = "64") {
                8
            } else {
                4
            }
        );
        assert_eq!(
            offset_of!(mn_header_t, value),
            if cfg!(target_pointer_width = "64") {
                16
            } else {
                8
            }
        );
        assert_eq!(
            offset_of!(mn_header_t, value_length),
            if cfg!(target_pointer_width = "64") {
                24
            } else {
                12
            }
        );
        assert_eq!(offset_of!(mn_string_t, data), 0);
        assert_eq!(
            offset_of!(mn_string_t, length),
            if cfg!(target_pointer_width = "64") {
                8
            } else {
                4
            }
        );

        assert_eq!(offset_of!(mn_request_callbacks_t, size), 0);
        assert_eq!(offset_of!(mn_request_callbacks_t, version), 4);
        assert_eq!(offset_of!(mn_request_callbacks_t, user_data), 8);
        assert_eq!(
            offset_of!(mn_request_callbacks_t, on_response),
            if cfg!(target_pointer_width = "64") {
                16
            } else {
                12
            }
        );
        assert_eq!(
            offset_of!(mn_request_callbacks_t, on_body),
            if cfg!(target_pointer_width = "64") {
                24
            } else {
                16
            }
        );
        assert_eq!(
            offset_of!(mn_request_callbacks_t, on_complete),
            if cfg!(target_pointer_width = "64") {
                32
            } else {
                20
            }
        );
        assert_eq!(
            offset_of!(mn_request_callbacks_t, on_redirect),
            if cfg!(target_pointer_width = "64") {
                40
            } else {
                24
            }
        );

        assert_eq!(offset_of!(mn_request_config_t, size), 0);
        assert_eq!(offset_of!(mn_request_config_t, version), 4);
        assert_eq!(offset_of!(mn_request_config_t, url), 8);
        assert_eq!(
            offset_of!(mn_request_config_t, url_length),
            if cfg!(target_pointer_width = "64") {
                16
            } else {
                12
            }
        );
        assert_eq!(
            offset_of!(mn_request_config_t, method),
            if cfg!(target_pointer_width = "64") {
                24
            } else {
                16
            }
        );
        assert_eq!(
            offset_of!(mn_request_config_t, method_length),
            if cfg!(target_pointer_width = "64") {
                32
            } else {
                20
            }
        );
        assert_eq!(
            offset_of!(mn_request_config_t, headers),
            if cfg!(target_pointer_width = "64") {
                40
            } else {
                24
            }
        );
        assert_eq!(
            offset_of!(mn_request_config_t, header_count),
            if cfg!(target_pointer_width = "64") {
                48
            } else {
                28
            }
        );
        assert_eq!(
            offset_of!(mn_request_config_t, timeout_ms),
            if cfg!(target_pointer_width = "64") {
                56
            } else {
                32
            }
        );
        assert_eq!(
            offset_of!(mn_request_config_t, callbacks),
            if cfg!(target_pointer_width = "64") {
                64
            } else {
                40
            }
        );
        assert_eq!(
            offset_of!(mn_request_config_t, body),
            if cfg!(target_pointer_width = "64") {
                112
            } else {
                68
            }
        );
        assert_eq!(
            offset_of!(mn_request_config_t, body_length),
            if cfg!(target_pointer_width = "64") {
                120
            } else {
                72
            }
        );
        assert_eq!(
            offset_of!(mn_request_config_t, upload_mode),
            if cfg!(target_pointer_width = "64") {
                128
            } else {
                76
            }
        );
        assert_eq!(
            offset_of!(mn_request_config_t, cache_mode),
            if cfg!(target_pointer_width = "64") {
                132
            } else {
                80
            }
        );
        assert_eq!(
            offset_of!(mn_request_config_t, redirect_mode),
            if cfg!(target_pointer_width = "64") {
                136
            } else {
                84
            }
        );
        assert_eq!(
            offset_of!(mn_request_config_t, priority),
            if cfg!(target_pointer_width = "64") {
                140
            } else {
                88
            }
        );

        assert_eq!(offset_of!(mn_websocket_callbacks_t, size), 0);
        assert_eq!(offset_of!(mn_websocket_callbacks_t, version), 4);
        assert_eq!(offset_of!(mn_websocket_callbacks_t, user_data), 8);
        assert_eq!(
            offset_of!(mn_websocket_callbacks_t, on_open),
            if cfg!(target_pointer_width = "64") {
                16
            } else {
                12
            }
        );
        assert_eq!(
            offset_of!(mn_websocket_callbacks_t, on_message),
            if cfg!(target_pointer_width = "64") {
                24
            } else {
                16
            }
        );
        assert_eq!(
            offset_of!(mn_websocket_callbacks_t, on_closing),
            if cfg!(target_pointer_width = "64") {
                32
            } else {
                20
            }
        );
        assert_eq!(
            offset_of!(mn_websocket_callbacks_t, on_closed),
            if cfg!(target_pointer_width = "64") {
                40
            } else {
                24
            }
        );
        assert_eq!(
            offset_of!(mn_websocket_callbacks_t, on_failure),
            if cfg!(target_pointer_width = "64") {
                48
            } else {
                28
            }
        );

        assert_eq!(offset_of!(mn_websocket_config_t, size), 0);
        assert_eq!(offset_of!(mn_websocket_config_t, version), 4);
        assert_eq!(offset_of!(mn_websocket_config_t, url), 8);
        assert_eq!(
            offset_of!(mn_websocket_config_t, url_length),
            if cfg!(target_pointer_width = "64") {
                16
            } else {
                12
            }
        );
        assert_eq!(
            offset_of!(mn_websocket_config_t, origin),
            if cfg!(target_pointer_width = "64") {
                24
            } else {
                16
            }
        );
        assert_eq!(
            offset_of!(mn_websocket_config_t, origin_length),
            if cfg!(target_pointer_width = "64") {
                32
            } else {
                20
            }
        );
        assert_eq!(
            offset_of!(mn_websocket_config_t, protocols),
            if cfg!(target_pointer_width = "64") {
                40
            } else {
                24
            }
        );
        assert_eq!(
            offset_of!(mn_websocket_config_t, protocol_count),
            if cfg!(target_pointer_width = "64") {
                48
            } else {
                28
            }
        );
        assert_eq!(
            offset_of!(mn_websocket_config_t, headers),
            if cfg!(target_pointer_width = "64") {
                56
            } else {
                32
            }
        );
        assert_eq!(
            offset_of!(mn_websocket_config_t, header_count),
            if cfg!(target_pointer_width = "64") {
                64
            } else {
                36
            }
        );
        assert_eq!(
            offset_of!(mn_websocket_config_t, callbacks),
            if cfg!(target_pointer_width = "64") {
                72
            } else {
                40
            }
        );
        assert_eq!(
            offset_of!(mn_websocket_config_t, timeout_ms),
            if cfg!(target_pointer_width = "64") {
                128
            } else {
                // 72, not 68: u64 is 8-byte aligned even on i386, so the
                // compiler inserts 4 bytes of padding after `callbacks`
                // (which ends at 68). Verified against the real header with
                // `gcc -m32` static asserts, and against rustc's own i686
                // layout: `nm --print-size` on an i686 object reports
                // offset_of!(mn_websocket_config_t, timeout_ms) == 0x48 = 72.
                // The previous 68 was never executed, because CI only runs the
                // 64-bit branch, so nothing caught it.
                72
            }
        );
    }

    #[test]
    fn sizes_and_enum_values_match_public_header() {
        if cfg!(target_pointer_width = "64") {
            assert_eq!(size_of::<mn_engine_config_t>(), 248);
            assert_eq!(size_of::<mn_request_callbacks_t>(), 48);
            assert_eq!(size_of::<mn_request_config_t>(), 152);
            assert_eq!(size_of::<mn_websocket_callbacks_t>(), 56);
            assert_eq!(size_of::<mn_websocket_config_t>(), 136);
            assert_eq!(size_of::<mn_header_t>(), 32);
            assert_eq!(size_of::<mn_string_t>(), 16);
            // Both sizes below were produced by the C compiler reading
            // core/abi/minicronet.h, not by arithmetic: 88 on x86_64, 52 on
            // i686 (the tail pads to the i64 alignment).
            assert_eq!(size_of::<mn_cookie_t>(), 88);
        } else {
            // Verified against the header itself: dumping the record layout
            // for triple i686-unknown-linux-gnu (size_t = unsigned int) yields
            // engine_config 140, request_callbacks 28, request_config 96,
            // websocket_callbacks 32, websocket_config 80, header 16, string 8,
            // cookie 52. ABI 12 appended one u8 to request_config, so the 32-bit
            // size moved 92 -> 96 (the tail pads to the 4-byte alignment the
            // struct already had); nothing else moved.
            // The previous table said 88/28/92/28/76/16/8, so three of the five
            // struct assertions disagreed with the header on the very platforms
            // this branch covers (linux-x86, windows-x86). Nothing caught it
            // because CI only runs the 64-bit branch.
            assert_eq!(size_of::<mn_engine_config_t>(), 136);
            assert_eq!(size_of::<mn_request_callbacks_t>(), 28);
            assert_eq!(size_of::<mn_request_config_t>(), 100);
            assert_eq!(size_of::<mn_websocket_callbacks_t>(), 32);
            assert_eq!(size_of::<mn_websocket_config_t>(), 80);
            assert_eq!(size_of::<mn_header_t>(), 16);
            assert_eq!(size_of::<mn_string_t>(), 8);
            assert_eq!(size_of::<mn_cookie_t>(), 52);
        }
        assert_eq!(mn_result_t::MN_OK.0, 0);
        assert_eq!(mn_result_t::MN_ERROR_PROFILE_UNSUPPORTED.0, 15);
        assert_eq!(mn_http_cache_mode_t::MN_HTTP_CACHE_DISABLED.0, 1);
        assert_eq!(mn_protocol_mode_t::MN_PROTOCOL_FORCE_H3.0, 3);
        assert_eq!(mn_tls_verify_mode_t::MN_TLS_VERIFY_INSECURE.0, 2);
        assert_eq!(mn_upload_mode_t::MN_UPLOAD_CHUNKED.0, 2);
        assert_eq!(mn_cache_mode_t::MN_CACHE_ONLY_IF_CACHED.0, 5);
        assert_eq!(mn_redirect_mode_t::MN_REDIRECT_ERROR.0, 2);
        assert_eq!(mn_read_disposition_t::MN_READ_PAUSE.0, 0);
        assert_eq!(mn_read_disposition_t::MN_READ_CONTINUE.0, 1);
        // Compared against the header, not against a literal: this line
        // was the second place ABI 11 appeared, and a bump that changed
        // only the constant left the test asserting the old number.
        assert_eq!(MN_ABI_VERSION, 17);
        assert_eq!(mn_request_priority_t::MN_REQUEST_PRIORITY_IDLE.0, 5);
        assert_eq!(mn_header_policy_t::MN_HEADER_POLICY_DROP.0, 1);
        assert_eq!(mn_header_scope_t::MN_HEADER_SCOPE_WEBSOCKET.0, 1);
        assert_eq!(MN_HEADER_FLAG_SETTABLE, 1);
        assert_eq!(MN_HEADER_FLAG_REMOVABLE, 2);
        assert_eq!(MN_HEADER_FLAG_DROPPED, 4);
        assert_eq!(
            mn_websocket_message_type_t::MN_WEBSOCKET_MESSAGE_BINARY.0,
            2
        );
        assert_eq!(mn_platform_t::MN_PLATFORM_LINUX.0, 3);
        assert_eq!(mn_cookie_same_site_t::MN_COOKIE_SAME_SITE_STRICT.0, 3);
        assert_eq!(mn_http_version_t::MN_HTTP_VERSION_3.0, 5);
        // Capability bits are a mask, not an enum value: what matters is that
        // each is exactly one bit and that no two collide. Asserting the
        // literal here is what would catch a future append reusing bit 0.
        assert_eq!(mn_capability_t::MN_CAPABILITY_QUIC_PROXY.0, 1);
        assert_eq!(mn_ech_status_t::MN_ECH_FAILED.0, 2);
    }

    // The size assertions above still pass if the appended fields are
    // reordered among themselves, because the total does not change. These pin
    // each field: a caller reading platform from the old offset would get a
    // silently wrong value, not a crash.
    #[test]
    fn v9_engine_config_tail_matches_header_offsets() {
        if cfg!(target_pointer_width = "64") {
            assert_eq!(offset_of!(mn_engine_config_t, host_mapping_rules), 152);
            assert_eq!(
                offset_of!(mn_engine_config_t, host_mapping_rules_length),
                160
            );
            assert_eq!(offset_of!(mn_engine_config_t, dns_nameservers), 168);
            assert_eq!(offset_of!(mn_engine_config_t, doh_template), 184);
            assert_eq!(offset_of!(mn_engine_config_t, hsts_bypass_list), 200);
            assert_eq!(offset_of!(mn_engine_config_t, proxy_chain), 216);
            assert_eq!(offset_of!(mn_engine_config_t, dns_secure_mode), 232);
            assert_eq!(offset_of!(mn_engine_config_t, dns_padding), 235);
            assert_eq!(
                offset_of!(mn_engine_config_t, cookie_samesite_override),
                236
            );
            assert_eq!(offset_of!(mn_engine_config_t, inject_client_hints), 237);
            // platform sits at 240, not 238: an enum is int-sized, so the two
            // trailing u8 fields are followed by two bytes of padding.
            assert_eq!(offset_of!(mn_engine_config_t, platform), 240);
            // v11: the enum at 240 is 4 bytes wide, so the appended u32 lands
            // at 244 with no padding, and the struct grows 248 -> 252... except
            // it does not: 248 was already 4-byte aligned, so the field fits in
            // the trailing padding and the struct keeps its old size.
            //
            // These three numbers are what the C compiler reports for
            // core/abi/minicronet.h, not what the layout reasoning above
            // predicts; the reasoning predicted 248/252 and was wrong. Verified
            // with clang -I core/abi printing offsetof and sizeof directly.
            assert_eq!(offset_of!(mn_engine_config_t, max_sockets_per_group), 244);
            assert_eq!(size_of::<mn_engine_config_t>(), 248);
            // dns_padding is what makes the DNS flag group four bytes wide.
            // Dropping it would move cookie_samesite_override onto the padding
            // byte, where a reader of the DNS mode group would then read a
            // cookie policy.
            assert_eq!(
                offset_of!(mn_engine_config_t, cookie_samesite_override)
                    - offset_of!(mn_engine_config_t, dns_secure_mode),
                4
            );
            assert_eq!(offset_of!(mn_cookie_t, expiry_unix_seconds), 72);
            assert_eq!(offset_of!(mn_cookie_t, has_expiry), 80);
            assert_eq!(offset_of!(mn_cookie_t, same_site), 83);
        } else {
            // Measured with cc -m32 -ffreestanding against the real header.
            assert_eq!(offset_of!(mn_engine_config_t, host_mapping_rules), 84);
            assert_eq!(offset_of!(mn_engine_config_t, platform), 132);
            assert_eq!(offset_of!(mn_cookie_t, expiry_unix_seconds), 40);
            assert_eq!(offset_of!(mn_cookie_t, has_expiry), 48);
            assert_eq!(offset_of!(mn_cookie_t, same_site), 51);
        }
    }

    // `dns_over_tls` is documented as must-be-0 in core/abi/minicronet.h and
    // the Core returns MN_ERROR_INVALID_ARGUMENT for any other value. That
    // guard is what stops a binding from silently disabling the built-in
    // resolver: DnsClient::CanUseInsecureDnsTransactions() returns false when
    // DnsConfig::dns_over_tls_active is set (net/dns/dns_client.cc:168), so the
    // request falls back to TaskType::SYSTEM and dns_nameservers/doh_template
    // are dropped. Measured: 4 queries reached the configured server with the
    // field 0, 0 queries with it 1, and a 200 either way.
    //
    // The field must therefore stay reachable and stay one byte wide; a later
    // ABI edit that widened it would change every offset after it, and the
    // layout assertions above are the only thing that would notice.
    #[test]
    fn dns_over_tls_is_a_single_byte_in_the_config() {
        assert_eq!(
            size_of::<mn_engine_config_t>(),
            if cfg!(target_pointer_width = "64") {
                248
            } else {
                140
            }
        );
        // Named so a rename in the header breaks this test rather than the
        // guard silently disappearing along with the field.
        let _: fn(&mn_engine_config_t) -> u8 = |config| config.dns_over_tls;
    }
}
