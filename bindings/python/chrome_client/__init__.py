"""Chromium Core HTTP/WebSocket client.

Two API shapes are exposed over one Chromium network stack:

* ``requests``-shaped:  ``Session``, ``Response``, ``session.cookies``,
  ``session.proxies``, the ``requests.exceptions`` hierarchy, ``codes``.
* ``curl_cffi``-shaped:  ``impersonate``, ``http_version`` for HTTP/1.1, HTTP/2 and
  HTTP/3, ``AsyncSession``, ``CurlMime``, ``Headers``, ``Cookies``, ``WebSocket``.

    import chrome_client
    with chrome_client.Session(impersonate="chrome_152") as session:
        session.get("https://example.com")

``chrome_client.requests`` mirrors the ``requests`` module namespace, including
``requests.Session``:

    from chrome_client import requests
    with requests.Session() as session:
        session.get("https://example.com")
"""

import sys as _sys

from . import _python_impl as _impl
from ._python_impl import *  # noqa: F401,F403
from ._python_impl import __all__ as _all

# Expose the implementation modules under the public package name so that
# `chrome_client.exceptions.Timeout` and `import chrome_client.utils` resolve the
# same way their requests counterparts do.
for _module in ("exceptions", "structures", "cookies", "models", "sessions",
                "adapters", "auth", "utils", "status_codes", "impersonate",
                "multipart", "websockets", "engine", "api"):
    _sys.modules[__name__ + "." + _module] = getattr(_impl, _module)
del _module

# Imported after the aliases above so `chrome_client.requests` is the real
# submodule rather than a stand-in object.
from . import requests  # noqa: E402,F401

_native_module = _impl._native
_native_name = _native_module.__name__.rsplit(".", 1)[-1]
if _native_name in ("chrome_client_native", "chrome_client_native36"):
    _sys.modules.setdefault(__name__ + "." + _native_name, _native_module)

__version__ = "0.2.2"
__all__ = list(_all) + ["requests", "__version__"]

del _all, _native_module, _native_name, _sys
