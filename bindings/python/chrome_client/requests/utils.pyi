"""Helpers mirroring ``requests.utils``.

At runtime this module *is* ``chrome_client._python_impl.utils``: the package aliases it
into ``sys.modules`` on import. The names below are re-exported
explicitly, so a type checker follows the same path instead of
seeing ``Any`` (PEP 561).
"""

from .._python_impl.utils import (
    DEFAULT_ACCEPT_ENCODING as DEFAULT_ACCEPT_ENCODING,
    UNRESERVED_SET as UNRESERVED_SET,
    NETRC_FILES as NETRC_FILES,
    HeaderLink as HeaderLink,
    default_headers as default_headers,
    to_key_val_list as to_key_val_list,
    super_len as super_len,
    unquote_unreserved as unquote_unreserved,
    requote_uri as requote_uri,
    get_encoding_from_headers as get_encoding_from_headers,
    get_encodings_from_content as get_encodings_from_content,
    guess_json_utf as guess_json_utf,
    parse_header_links as parse_header_links,
    guess_filename as guess_filename,
    get_auth_from_url as get_auth_from_url,
    urldefragauth as urldefragauth,
    prepend_scheme_if_needed as prepend_scheme_if_needed,
    is_ipv4_address as is_ipv4_address,
    address_in_network as address_in_network,
    should_bypass_proxies as should_bypass_proxies,
    proxy_bypass as proxy_bypass,
    get_environ_proxies as get_environ_proxies,
    resolve_proxies as resolve_proxies,
    get_netrc_auth as get_netrc_auth,
    check_header_validity as check_header_validity,
    default_user_agent as default_user_agent,
)
