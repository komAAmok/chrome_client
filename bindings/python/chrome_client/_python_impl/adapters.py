"""Transport adapters.

``requests`` exposes adapters as the mount point for third-party transports
(``requests_mock``, retry policies, test doubles).  That extension point is kept:
``Session.send`` delegates to a mounted adapter whose ``send`` is not
``HTTPAdapter``'s, so code that mounts a custom adapter keeps working.

``HTTPAdapter`` itself is a marker.  Its urllib3 pool arguments are recorded but
cannot be honoured: Chromium owns the socket pools, and reporting otherwise would
be a lie.
"""


class BaseAdapter(object):
    def __init__(self):
        super(BaseAdapter, self).__init__()

    def send(self, request, stream=False, timeout=None, verify=True, cert=None,
             proxies=None):
        raise NotImplementedError

    def close(self):
        raise NotImplementedError


class HTTPAdapter(BaseAdapter):
    """Default adapter; a marker that the Core should handle the request.

    ``pool_connections``, ``pool_maxsize`` and ``pool_block`` are accepted for
    source compatibility and recorded on the instance.  Chromium's
    ``HttpNetworkSession`` owns connection limits, so setting them here changes
    nothing -- pass ``max_engines`` to the session if the intent was to bound
    resource use.
    """

    __attrs__ = ["max_retries", "config", "_pool_connections", "_pool_maxsize",
                 "_pool_block"]

    def __init__(self, pool_connections=10, pool_maxsize=10, max_retries=0,
                 pool_block=False):
        super(HTTPAdapter, self).__init__()
        self.max_retries = max_retries
        self.config = {}
        self.proxy_manager = {}
        self._pool_connections = pool_connections
        self._pool_maxsize = pool_maxsize
        self._pool_block = pool_block

    def __repr__(self):
        return "<%s>" % type(self).__name__

    def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
        self._pool_connections = connections
        self._pool_maxsize = maxsize
        self._pool_block = block

    def proxy_manager_for(self, proxy, **proxy_kwargs):
        return self.proxy_manager.setdefault(proxy, {})

    def cert_verify(self, conn, url, verify, cert):
        return None

    def build_response(self, request, response):
        return response

    def send(self, request, stream=False, timeout=None, verify=True, cert=None,
             proxies=None):
        # Never called: `Session.send` recognises this class and uses the Core.
        raise NotImplementedError(
            "HTTPAdapter is a marker; the Chromium Core performs the transfer")

    def close(self):
        self.proxy_manager.clear()
