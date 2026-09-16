"""Type stub for ``chrome_client.sessions``.

At runtime this module is an alias of ``chrome_client._python_impl.sessions``
installed into ``sys.modules`` on import; these explicit same-name re-exports
expose the alias to type checkers (PEP 561).
"""

from ._python_impl.sessions import (
    AsyncClient as AsyncClient,
    AsyncSession as AsyncSession,
    BaseSession as BaseSession,
    Client as Client,
    RetryStrategy as RetryStrategy,
    Session as Session,
    merge_setting as merge_setting,
    proxy_from_proxies as proxy_from_proxies,
)
