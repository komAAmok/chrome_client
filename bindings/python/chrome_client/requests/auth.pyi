"""Authentication handlers, matching ``requests.auth``.

At runtime this module *is* ``chrome_client._python_impl.auth``: the package aliases it
into ``sys.modules`` on import. The names below are re-exported
explicitly, so a type checker follows the same path instead of
seeing ``Any`` (PEP 561).
"""

from .._python_impl.auth import (
    AuthCredentials as AuthCredentials,
    AuthBase as AuthBase,
    HTTPBasicAuth as HTTPBasicAuth,
    HTTPProxyAuth as HTTPProxyAuth,
    HTTPDigestAuth as HTTPDigestAuth,
    build_auth as build_auth,
)
