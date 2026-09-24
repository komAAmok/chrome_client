"""``Session``, ``AsyncSession``, ``Client`` and ``AsyncClient``.

At runtime this module *is* ``chrome_client._python_impl.sessions``: the package aliases it
into ``sys.modules`` on import. The names below are re-exported
explicitly, so a type checker follows the same path instead of
seeing ``Any`` (PEP 561).
"""

from ._python_impl.sessions import (
    ASYNC_POLL_BATCH as ASYNC_POLL_BATCH,
    STREAM_BUFFER_LIMIT as STREAM_BUFFER_LIMIT,
    Backoff as Backoff,
    RetryStrategy as RetryStrategy,
    BaseSession as BaseSession,
    Session as Session,
    AsyncSession as AsyncSession,
    Client as Client,
    AsyncClient as AsyncClient,
    split_proxy_credentials as split_proxy_credentials,
    proxy_from_proxies as proxy_from_proxies,
    merge_setting as merge_setting,
    merge_hooks as merge_hooks,
)
