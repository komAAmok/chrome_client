"""Module-level API.

``requests`` creates a throwaway ``Session`` for every module-level call.  That
is not viable here: one session owns a Chromium ``URLRequestContext`` with its own
threads, socket pools, and caches, so a per-call session would cost megabytes and
a thread spin-up per request.

So ``chrome_client.get(...)`` and friends share one process-wide session.  The
deliberate consequence is that module-level calls also share a connection pool
and a cookie store, unlike requests.  Use an explicit ``Session`` when isolation
matters, and ``close_shared_session()`` to drop the shared one.
"""

import os
import threading

from .sessions import AsyncSession, Session

#: Constructor-only options; passing one of these builds a private session for
#: that call rather than reconfiguring the shared one.
_SESSION_ONLY = ("base_url", "default_headers", "trust_env", "max_engines",
                 "user_agent", "accept_language", "cache", "response_class",
                 "proxy_auth", "max_clients")

_lock = threading.Lock()
_shared = {"session": None, "pid": None}


def shared_session():
    """Returns the process-wide session, creating it on first use.

    The pid check makes a forked child build its own: Chromium's threads do not
    survive ``fork()``.
    """
    with _lock:
        current = os.getpid()
        if _shared["session"] is None or _shared["pid"] != current:
            _shared["session"] = Session()
            _shared["pid"] = current
        return _shared["session"]


def close_shared_session():
    with _lock:
        session = _shared["session"]
        _shared["session"] = None
        _shared["pid"] = None
    if session is not None:
        session.close()


def session(**kwargs):
    """``requests.session()`` factory."""
    return Session(**kwargs)


def async_session(**kwargs):
    return AsyncSession(**kwargs)


def request(method, url, **kwargs):
    # Only a value actually supplied builds a private session; `params=None` from
    # the `get()` shim must not cost a whole Chromium context.
    constructor = {name: kwargs.pop(name) for name in _SESSION_ONLY
                   if kwargs.get(name) is not None}
    if constructor:
        with Session(**constructor) as private:
            return private.request(method, url, **kwargs)
    return shared_session().request(method, url, **kwargs)


def get(url, params=None, **kwargs):
    return request("GET", url, params=params, **kwargs)


def options(url, **kwargs):
    return request("OPTIONS", url, **kwargs)


def head(url, **kwargs):
    kwargs.setdefault("allow_redirects", False)
    return request("HEAD", url, **kwargs)


def post(url, data=None, json=None, **kwargs):
    return request("POST", url, data=data, json=json, **kwargs)


def put(url, data=None, **kwargs):
    return request("PUT", url, data=data, **kwargs)


def patch(url, data=None, **kwargs):
    return request("PATCH", url, data=data, **kwargs)


def delete(url, **kwargs):
    return request("DELETE", url, **kwargs)


def trace(url, **kwargs):
    return request("TRACE", url, **kwargs)


def query(url, **kwargs):
    return request("QUERY", url, **kwargs)
