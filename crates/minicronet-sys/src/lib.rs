#![no_std]
#![allow(non_camel_case_types)]
#![allow(non_upper_case_globals)]

use core::ffi::c_void;

pub const MN_ABI_VERSION: u32 = 7;

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

pub type mn_request_response_fn =
    unsafe extern "C" fn(*mut c_void, *mut mn_request_t, i32, *const i8, usize);
pub type mn_request_body_fn =
    unsafe extern "C" fn(*mut c_void, *mut mn_request_t, *const u8, usize);
pub type mn_request_complete_fn =
    unsafe extern "C" fn(*mut c_void, *mut mn_request_t, mn_result_t, i32);
pub type mn_request_redirect_fn = unsafe extern "C" fn(
    *mut c_void,
    *mut mn_request_t,
    i32,
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
                68
            }
        );
    }

    #[test]
    fn sizes_and_enum_values_match_public_header() {
        if cfg!(target_pointer_width = "64") {
            assert_eq!(size_of::<mn_engine_config_t>(), 152);
            assert_eq!(size_of::<mn_request_callbacks_t>(), 48);
            assert_eq!(size_of::<mn_request_config_t>(), 144);
            assert_eq!(size_of::<mn_websocket_callbacks_t>(), 56);
            assert_eq!(size_of::<mn_websocket_config_t>(), 136);
            assert_eq!(size_of::<mn_header_t>(), 32);
            assert_eq!(size_of::<mn_string_t>(), 16);
        } else {
            assert_eq!(size_of::<mn_engine_config_t>(), 88);
            assert_eq!(size_of::<mn_request_callbacks_t>(), 28);
            assert_eq!(size_of::<mn_request_config_t>(), 92);
            assert_eq!(size_of::<mn_websocket_callbacks_t>(), 28);
            assert_eq!(size_of::<mn_websocket_config_t>(), 76);
            assert_eq!(size_of::<mn_header_t>(), 16);
            assert_eq!(size_of::<mn_string_t>(), 8);
        }
        assert_eq!(mn_result_t::MN_OK.0, 0);
        assert_eq!(mn_result_t::MN_ERROR_PROFILE_UNSUPPORTED.0, 15);
        assert_eq!(mn_http_cache_mode_t::MN_HTTP_CACHE_DISABLED.0, 1);
        assert_eq!(mn_protocol_mode_t::MN_PROTOCOL_FORCE_H3.0, 3);
        assert_eq!(mn_tls_verify_mode_t::MN_TLS_VERIFY_INSECURE.0, 2);
        assert_eq!(mn_upload_mode_t::MN_UPLOAD_CHUNKED.0, 2);
        assert_eq!(mn_cache_mode_t::MN_CACHE_ONLY_IF_CACHED.0, 5);
        assert_eq!(mn_redirect_mode_t::MN_REDIRECT_ERROR.0, 2);
        assert_eq!(mn_request_priority_t::MN_REQUEST_PRIORITY_IDLE.0, 5);
        assert_eq!(
            mn_websocket_message_type_t::MN_WEBSOCKET_MESSAGE_BINARY.0,
            2
        );
    }
}
