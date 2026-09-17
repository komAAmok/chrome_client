"""Type surface of :mod:`chrome_client.adapters`.

``requests`` exposes adapters as the mount point for third-party transports
(``requests_mock``, retry policies, test doubles).  That extension point is kept:
:meth:`Session.send` delegates to a mounted adapter whose ``send`` is not
``HTTPAdapter``'s, so code that mounts a custom adapter keeps working.

:class:`HTTPAdapter` itself is a marker.  Its urllib3 pool arguments are recorded
but cannot be honoured: Chromium owns the socket pools, and reporting otherwise
would be a lie.
"""

from typing import Any, Mapping, Optional

from ._types import Proxies, Timeout, Verify
from .models import PreparedRequest, Response


class BaseAdapter:
    """Transport interface ``requests`` defines.

    Subclass it and implement :meth:`send` to plug in a different transport; mount
    it with :meth:`Session.mount`.

    Example:
        >>> class Recording(BaseAdapter):
        ...     def send(self, request, **kwargs):
        ...         return super().send(request, **kwargs)
    """

    def __init__(self) -> None:
        """Creates the adapter. Subclasses add their own state here."""
        ...

    def send(
        self,
        request: PreparedRequest,
        stream: bool = False,
        timeout: Timeout = None,
        verify: Verify = True,
        cert: Any = None,
        proxies: Optional[Proxies] = None,
    ) -> Response:
        """Sends a prepared request and returns a response.

        Args:
            request: The wire-ready request.
            stream: Whether the body should be left unread.
            timeout: Deadline for the exchange.
            verify: Verification setting.
            cert: Client certificate, unused by this package.
            proxies: Proxy mapping.

        Returns:
            The response.

        Raises:
            NotImplementedError: Always, on the base class.
        """
        ...

    def close(self) -> None:
        """Releases any resources the adapter holds.

        Raises:
            NotImplementedError: Always, on the base class.
        """
        ...


class HTTPAdapter(BaseAdapter):
    """Default adapter; a marker that the Core should handle the request.

    ``pool_connections``, ``pool_maxsize`` and ``pool_block`` are accepted for
    source compatibility and recorded on the instance.  Chromium's
    ``HttpNetworkSession`` owns connection limits, so setting them here changes
    nothing -- pass ``max_engines`` to the session if the intent was to bound
    resource use.

    Attributes:
        max_retries: Recorded for compatibility; retries come from the session's
            ``retry=`` setting.
        config: Empty mapping, present for parity with requests.
        proxy_manager: Mapping of proxy URL to a placeholder, present for parity.
    """

    max_retries: Any
    config: Mapping[str, Any]
    proxy_manager: Mapping[str, Any]

    def __init__(
        self,
        pool_connections: int = 10,
        pool_maxsize: int = 10,
        max_retries: Any = 0,
        pool_block: bool = False,
    ) -> None:
        """Records the pool settings that this transport cannot apply.

        Args:
            pool_connections: Accepted and stored; Chromium decides connection
                reuse.
            pool_maxsize: Accepted and stored; Chromium decides the per-host
                ceiling (six for HTTP/1.1).
            max_retries: Accepted and stored.
            pool_block: Accepted and stored.
        """
        ...

    def __repr__(self) -> str:
        """Returns a short description of the adapter.

        Returns:
            e.g. ``"<HTTPAdapter>"``."""
        ...

    def init_poolmanager(self, connections: int, maxsize: int, block: bool = False, **pool_kwargs: Any) -> None:
        """Records pool sizing; the Core owns the real pools.

        Args:
            connections: Number of pools; recorded.
            maxsize: Connections per pool; recorded.
            block: Whether to block when the pool is full; recorded.
            **pool_kwargs: Ignored.
        """
        ...

    def proxy_manager_for(self, proxy: str, **proxy_kwargs: Any) -> Any:
        """Returns a placeholder proxy manager for a URL.

        Args:
            proxy: The proxy URL.
            **proxy_kwargs: Ignored.

        Returns:
            The placeholder stored for this proxy; proxies are configured on the
            engine instead, through ``Session(proxy=...)`` or ``proxies=``.
        """
        ...

    def cert_verify(self, conn: Any, url: str, verify: Verify, cert: Any) -> None:
        """No-op: verification is an engine-level Chromium setting.

        Args:
            conn: Ignored.
            url: Ignored.
            verify: Ignored; use ``Session(verify=...)``.
            cert: Ignored; client certificates are unsupported.
        """
        ...

    def build_response(self, request: PreparedRequest, response: Response) -> Response:
        """Returns the response unchanged.

        Args:
            request: The request that produced it.
            response: The response.

        Returns:
            The same response; there is no urllib3 response to wrap.
        """
        ...

    def send(
        self,
        request: PreparedRequest,
        stream: bool = False,
        timeout: Timeout = None,
        verify: Verify = True,
        cert: Any = None,
        proxies: Optional[Proxies] = None,
    ) -> Response:
        """Always raises: :meth:`Session.send` recognises this class and uses the Core.

        Args:
            request: Ignored.
            stream: Ignored.
            timeout: Ignored.
            verify: Ignored.
            cert: Ignored.
            proxies: Ignored.

        Returns:
            Never returns.

        Raises:
            NotImplementedError: Always.
        """
        ...

    def close(self) -> None:
        """Clears the placeholder proxy manager."""
        ...
