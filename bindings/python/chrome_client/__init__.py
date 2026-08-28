"""Public Python package for chrome_client.

The implementation is kept in a private subpackage while the package name
remains the stable public import:

    import chrome_client
"""

from ._python_impl import *  # noqa: F401,F403
from ._python_impl import __all__ as _all
from ._python_impl import _native as _native_module
import sys as _sys

_native_name = _native_module.__name__.rsplit(".", 1)[-1]
if _native_name in ("chrome_client_native", "chrome_client_native36"):
    _sys.modules.setdefault(__name__ + "." + _native_name, _native_module)

__all__ = list(_all)

del _all, _native_module, _native_name, _sys
