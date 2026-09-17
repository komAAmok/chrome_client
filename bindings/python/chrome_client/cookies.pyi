"""``RequestsCookieJar`` and the Core cookie-store bridge.

At runtime this module *is* ``chrome_client._python_impl.cookies``: the package aliases it
into ``sys.modules`` on import. The names below are re-exported
explicitly, so a type checker follows the same path instead of
seeing ``Any`` (PEP 561).
"""

from ._python_impl.cookies import (
    Cookie as Cookie,
    MockRequest as MockRequest,
    MockResponse as MockResponse,
    create_cookie as create_cookie,
    morsel_to_cookie as morsel_to_cookie,
    RequestsCookieJar as RequestsCookieJar,
    cookiejar_from_dict as cookiejar_from_dict,
    dict_from_cookiejar as dict_from_cookiejar,
    add_dict_to_cookiejar as add_dict_to_cookiejar,
    merge_cookies as merge_cookies,
    CookieJar as CookieJar,
    Cookies as Cookies,
)
