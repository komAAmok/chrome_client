"""``requests``-shaped namespace over the Chromium Core.

Written so that code targeting either ``requests`` or ``curl_cffi.requests``
imports unchanged::

    from chrome_client.requests import Session, AsyncSession
    from chrome_client.requests.exceptions import Timeout

Divergences from ``requests`` are limited to what the Chromium Core imposes and
are listed in ``docs/COMPATIBILITY_BOUNDARY.md``.  The one that surprises people:
module-level ``get``/``post`` share a session, so they share a connection pool and
cookie store.
"""

import sys as _sys

from .. import _python_impl as _impl
from .._python_impl import *  # noqa: F401,F403
from .._python_impl import __all__ as _all

for _module in ("exceptions", "structures", "cookies", "models", "sessions",
                "adapters", "auth", "utils", "status_codes", "impersonate",
                "multipart", "websockets", "engine", "api"):
    _sys.modules[__name__ + "." + _module] = getattr(_impl, _module)
del _module

__all__ = list(_all)

del _all, _sys
