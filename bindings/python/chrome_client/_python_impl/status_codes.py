"""Status-code lookup, matching ``requests.status_codes``."""

from .structures import LookupDict

_TITLES = {
    100: ("continue",),
    101: ("switching_protocols",),
    102: ("processing", "early-hints"),
    103: ("checkpoint",),
    122: ("uri_too_long", "request_uri_too_long"),
    200: ("ok", "okay", "all_ok", "all_okay", "all_good", "\\o/", "✓"),
    201: ("created",),
    202: ("accepted",),
    203: ("non_authoritative_info", "non_authoritative_information"),
    204: ("no_content",),
    205: ("reset_content", "reset"),
    206: ("partial_content", "partial"),
    207: ("multi_status", "multiple_status", "multi_stati", "multiple_stati"),
    208: ("already_reported",),
    226: ("im_used",),
    300: ("multiple_choices",),
    301: ("moved_permanently", "moved", "\\o-"),
    302: ("found",),
    303: ("see_other", "other"),
    304: ("not_modified",),
    305: ("use_proxy",),
    306: ("switch_proxy",),
    307: ("temporary_redirect", "temporary_moved", "temporary"),
    308: ("permanent_redirect", "resume_incomplete", "resume"),
    400: ("bad_request", "bad"),
    401: ("unauthorized",),
    402: ("payment_required", "payment"),
    403: ("forbidden",),
    404: ("not_found", "-o-"),
    405: ("method_not_allowed", "not_allowed"),
    406: ("not_acceptable",),
    407: ("proxy_authentication_required", "proxy_auth", "proxy_authentication"),
    408: ("request_timeout", "timeout"),
    409: ("conflict",),
    410: ("gone",),
    411: ("length_required",),
    412: ("precondition_failed", "precondition"),
    413: ("request_entity_too_large", "content_too_large"),
    414: ("request_uri_too_large", "uri_too_long"),
    415: ("unsupported_media_type", "unsupported_media", "media_type"),
    416: ("requested_range_not_satisfiable", "requested_range", "range_not_satisfiable"),
    417: ("expectation_failed",),
    418: ("im_a_teapot", "teapot", "i_am_a_teapot"),
    421: ("misdirected_request",),
    422: ("unprocessable_entity", "unprocessable", "unprocessable_content"),
    423: ("locked",),
    424: ("failed_dependency", "dependency"),
    425: ("unordered_collection", "unordered", "too_early"),
    426: ("upgrade_required", "upgrade"),
    428: ("precondition_required", "precondition"),
    429: ("too_many_requests", "too_many"),
    431: ("header_fields_too_large", "fields_too_large"),
    444: ("no_response", "none"),
    449: ("retry_with", "retry"),
    450: ("blocked_by_windows_parental_controls", "parental_controls"),
    451: ("unavailable_for_legal_reasons", "legal_reasons"),
    499: ("client_closed_request",),
    500: ("internal_server_error", "server_error", "/o\\", "✗"),
    501: ("not_implemented",),
    502: ("bad_gateway",),
    503: ("service_unavailable", "unavailable"),
    504: ("gateway_timeout",),
    505: ("http_version_not_supported", "http_version"),
    506: ("variant_also_negotiates",),
    507: ("insufficient_storage",),
    509: ("bandwidth_limit_exceeded", "bandwidth"),
    510: ("not_extended",),
    511: ("network_authentication_required", "network_auth", "network_authentication"),
}

# Reason phrases the Core does not report; ``Response.reason`` falls back here
# because ABI v8 carries only the numeric status.
REASONS = {
    100: "Continue", 101: "Switching Protocols", 102: "Processing", 103: "Early Hints",
    200: "OK", 201: "Created", 202: "Accepted", 203: "Non-Authoritative Information",
    204: "No Content", 205: "Reset Content", 206: "Partial Content", 207: "Multi-Status",
    208: "Already Reported", 226: "IM Used",
    300: "Multiple Choices", 301: "Moved Permanently", 302: "Found", 303: "See Other",
    304: "Not Modified", 305: "Use Proxy", 307: "Temporary Redirect",
    308: "Permanent Redirect",
    400: "Bad Request", 401: "Unauthorized", 402: "Payment Required", 403: "Forbidden",
    404: "Not Found", 405: "Method Not Allowed", 406: "Not Acceptable",
    407: "Proxy Authentication Required", 408: "Request Timeout", 409: "Conflict",
    410: "Gone", 411: "Length Required", 412: "Precondition Failed",
    413: "Content Too Large", 414: "URI Too Long", 415: "Unsupported Media Type",
    416: "Range Not Satisfiable", 417: "Expectation Failed", 418: "I'm a teapot",
    421: "Misdirected Request", 422: "Unprocessable Content", 423: "Locked",
    424: "Failed Dependency", 425: "Too Early", 426: "Upgrade Required",
    428: "Precondition Required", 429: "Too Many Requests",
    431: "Request Header Fields Too Large", 451: "Unavailable For Legal Reasons",
    500: "Internal Server Error", 501: "Not Implemented", 502: "Bad Gateway",
    503: "Service Unavailable", 504: "Gateway Timeout",
    505: "HTTP Version Not Supported", 506: "Variant Also Negotiates",
    507: "Insufficient Storage", 508: "Loop Detected", 510: "Not Extended",
    511: "Network Authentication Required",
}

codes = LookupDict(name="status_codes")
for _code, _titles in _TITLES.items():
    for _title in _titles:
        setattr(codes, _title, _code)
        codes[_title] = _code
        if not _title.startswith(("\\", "/", "✓", "✗")):
            upper = _title.upper()
            setattr(codes, upper, _code)
            codes[upper] = _code

del _code, _titles, _title
