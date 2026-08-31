"""Requests-shaped Python facade for the Chromium Core native extension.

The native Core owns all network work.  Async methods are event-driven:
Core callbacks wake the asyncio loop with ``call_soon_threadsafe`` and no
Python worker thread is created per request.
"""

import asyncio
from collections import deque
import importlib
import importlib.util
import json as _json
import pathlib
import sys
from http.cookies import SimpleCookie
from urllib.parse import urlencode, urlsplit, urlunsplit

def _load_native():
    _is_py36 = sys.version_info < (3, 7)
    _module_name = "chrome_client_native36" if _is_py36 else "chrome_client_native"
    try:
        return importlib.import_module("chrome_client." + _module_name)
    except ImportError:
        try:
            return importlib.import_module(_module_name)
        except ImportError:
            pass
    # Source checkout root (the wheel does not need this fallback). The source
    # tree is one level deeper than the wheel package.
    _root = pathlib.Path(__file__).resolve().parents[4]
    _search_dirs = list(sys.path) + [
        str(_root / "target" / "debug"),
        str(_root / "bindings" / "python36" / "target" / "debug"),
    ]
    _patterns = ["lib%s*.so" % _module_name, "%s*.pyd" % _module_name]
    for _directory in _search_dirs:
        for _pattern in _patterns:
            for _path in pathlib.Path(_directory).glob(_pattern):
                _spec = importlib.util.spec_from_file_location(_module_name, str(_path))
                if _spec and _spec.loader:
                    _module = importlib.util.module_from_spec(_spec)
                    _spec.loader.exec_module(_module)
                    sys.modules.setdefault(_module_name, _module)
                    return _module
    raise ImportError("chrome_client native extension is not installed")


_native = _load_native()


class RequestException(Exception):
    pass


class Timeout(RequestException):
    pass


class ResponseTooLarge(RequestException):
    pass


def _validate_max_response_bytes(value):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("max_response_bytes must be a non-negative integer or None")
    return value


class _SyncBodyReader:
    def __init__(self, request, limit):
        self._request = request
        self._limit = limit
        self._total = 0
        self._closed = False

    def __iter__(self):
        while not self._closed:
            chunk = self._request.next_body()
            if chunk is None:
                self.close()
                return
            self._total += len(chunk)
            if self._limit is not None and self._total > self._limit:
                try:
                    self._request.cancel()
                finally:
                    self.close()
                raise ResponseTooLarge("response exceeded max_response_bytes=%d" % self._limit)
            yield bytes(chunk)

    def close(self):
        if not self._closed:
            self._closed = True
            try:
                self._request.detach_callback()
            except RuntimeError:
                pass


class Response:
    def __init__(self, status_code, headers, content, url=None, body_reader=None):
        self.status_code = status_code
        self.headers = CaseInsensitiveDict(_parse_headers(headers))
        self._content = bytes(content)
        self.url = url
        self._body_reader = body_reader

    @property
    def content(self):
        if self._body_reader is not None:
            self._content = b"".join(self._body_reader)
            self._body_reader = None
        return self._content

    @content.setter
    def content(self, value):
        self._content = bytes(value)

    @property
    def ok(self):
        return self.status_code < 400

    @property
    def encoding(self):
        value = self.headers.get("content-type", "")
        return value.lower().split("charset=", 1)[1].split(";", 1)[0].strip() if "charset=" in value.lower() else None

    @encoding.setter
    def encoding(self, value):
        self.headers["content-type"] = "%s; charset=%s" % (self.headers.get("content-type", "text/plain"), value)

    @property
    def text(self):
        value = self.headers.get("content-type", "")
        charset = self.encoding or "utf-8"
        if "charset=" in value.lower():
            charset = value.lower().split("charset=", 1)[1].split(";", 1)[0].strip()
        try:
            return self.content.decode(charset, "replace")
        except (LookupError, UnicodeError):
            return self.content.decode("utf-8", "replace")

    def json(self, **kwargs):
        return _json.loads(self.text, **kwargs)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RequestException("HTTP %s for %s" % (self.status_code, self.url or ""))
        return self

    def iter_content(self, chunk_size=8192):
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        if self._body_reader is not None:
            for chunk in self._body_reader:
                for offset in range(0, len(chunk), chunk_size):
                    yield chunk[offset : offset + chunk_size]
            self._body_reader = None
            return
        for offset in range(0, len(self.content), chunk_size):
            yield self.content[offset : offset + chunk_size]

    def close(self):
        if self._body_reader is not None:
            self._body_reader.close()
            self._body_reader = None

    def iter_lines(self, chunk_size=8192, decode_unicode=False):
        pending = b""
        for chunk in self.iter_content(chunk_size):
            pending += chunk
            lines = pending.splitlines(True)
            pending = lines.pop() if lines and not lines[-1].endswith((b"\n", b"\r")) else b""
            for line in lines:
                line = line.rstrip(b"\r\n")
                yield line.decode("utf-8", "replace") if decode_unicode else line
        if pending:
            yield pending.decode("utf-8", "replace") if decode_unicode else pending


class AsyncResponse(Response):
    def __init__(self, status_code, headers, content, url=None, body_reader=None):
        super().__init__(status_code, headers, content, url)
        self._async_body_reader = body_reader

    async def aiter_bytes(self, chunk_size=8192):
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        if self._async_body_reader is not None:
            async for chunk in self._async_body_reader:
                for offset in range(0, len(chunk), chunk_size):
                    yield chunk[offset : offset + chunk_size]
            self._async_body_reader = None
            return
        for chunk in self.iter_content(chunk_size):
            yield chunk
            await asyncio.sleep(0)

    async def aclose(self):
        if self._async_body_reader is not None:
            await self._async_body_reader.aclose()
            self._async_body_reader = None


class CaseInsensitiveDict(dict):
    def __init__(self, values=None, **kwargs):
        super().__init__()
        self.update(values or {}, **kwargs)

    def __setitem__(self, key, value):
        super().__setitem__(str(key).lower(), value)

    def __getitem__(self, key):
        return super().__getitem__(str(key).lower())

    def __contains__(self, key):
        return super().__contains__(str(key).lower())

    def get(self, key, default=None):
        return super().get(str(key).lower(), default)

    def update(self, values=None, **kwargs):
        if values:
            for key, value in (values.items() if hasattr(values, "items") else values):
                self[key] = value
        for key, value in kwargs.items():
            self[key] = value


def _parse_headers(raw):
    headers = CaseInsensitiveDict()
    if isinstance(raw, bytes):
        raw = raw.decode("iso-8859-1", "replace")
    for line in str(raw).splitlines():
        if ":" in line:
            name, value = line.split(":", 1)
            headers[name.strip().lower()] = value.strip()
    return headers


def _headers(headers, cookies):
    result = []
    if headers:
        result.extend((str(key), str(value)) for key, value in headers.items())
    if cookies:
        cookie = SimpleCookie()
        for key, value in cookies.items():
            cookie[str(key)] = str(value)
        result.append(("Cookie", "; ".join(morsel.OutputString() for morsel in cookie.values())))
    return result


def _merge_headers(defaults, overrides):
    merged = CaseInsensitiveDict(defaults or {})
    merged.update(overrides or {})
    return merged


def _merge_cookies(defaults, overrides):
    merged = {}
    if defaults:
        merged.update(defaults)
    if overrides:
        merged.update(overrides)
    return merged


def _proxy_from_proxies(url, proxies):
    """Select a Requests-style proxy mapping entry for *url*."""
    if proxies is None:
        return None
    if not hasattr(proxies, "items"):
        raise TypeError("proxies must be a mapping of schemes to proxy URLs")
    values = {}
    for key, value in proxies.items():
        key = str(key).lower()
        if value is not None and not isinstance(value, str):
            raise TypeError("proxy values must be strings or None")
        values[key] = value
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    proxy_scheme = {"ws": "http", "wss": "https"}.get(scheme, scheme)
    keys = []
    if parts.hostname:
        keys.extend((scheme + "://" + parts.hostname,
                     proxy_scheme + "://" + parts.hostname))
    keys.extend((scheme, proxy_scheme))
    if parts.hostname:
        keys.append("all://" + parts.hostname)
    keys.append("all")
    for key in keys:
        if key in values:
            return values[key]
    return None


def _url(url, params):
    if not params:
        return url
    parts = urlsplit(url)
    query = urlencode(params, doseq=True)
    old = parts.query
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "%s&%s" % (old, query) if old else query, parts.fragment))


def _body(data, json_value):
    if json_value is not None:
        return _json.dumps(json_value, separators=(",", ":")).encode("utf-8")
    if data is None:
        return None
    if isinstance(data, bytes):
        return data
    if isinstance(data, str):
        return data.encode("utf-8")
    if hasattr(data, "items"):
        return urlencode(data, doseq=True).encode("ascii")
    return bytes(data)


class Client:
    def __init__(self, impersonate=None, proxy=None, verify=True, timeout=None,
                 headers=None, cookies=None, max_response_bytes=None, proxies=None):
        self.impersonate, self.proxy, self.verify = impersonate, proxy, verify
        self.proxies = proxies
        self._engine = _native.PyEngine(impersonate, proxy, verify)
        self.timeout = timeout
        self.headers = CaseInsensitiveDict(headers or {})
        self.cookies = dict(cookies or {})
        self.max_response_bytes = _validate_max_response_bytes(max_response_bytes)
        self._closed = False

    def _ensure_open(self):
        if self._closed or self._engine is None:
            raise RequestException("Client is closed")

    def request(self, method, url, params=None, data=None, json=None, headers=None, cookies=None,
                timeout=None, allow_redirects=True, stream=False, impersonate=None,
                proxy=None, proxies=None, verify=None, max_response_bytes=None):
        self._ensure_open()
        response_limit = self.max_response_bytes if max_response_bytes is None else _validate_max_response_bytes(max_response_bytes)
        proxy = proxy if proxy is not None else self.proxy
        if proxy is None:
            proxy = _proxy_from_proxies(url, self.proxies if proxies is None else proxies)
        request_options = {
            key: value for key, value in (
                ("impersonate", impersonate),
                ("proxy", proxy),
                ("verify", verify),
            ) if value is not None
        }
        if request_options and any(request_options.get(key, getattr(self, key)) != getattr(self, key) for key in request_options):
            child = Client(impersonate=request_options.get("impersonate", self.impersonate),
                           proxy=request_options.get("proxy", self.proxy),
                           verify=request_options.get("verify", self.verify),
                           timeout=self.timeout, headers=self.headers, cookies=self.cookies,
                           max_response_bytes=response_limit)
            return child.request(method, url, params=params, data=data, json=json, headers=headers,
                                 cookies=cookies, timeout=timeout, allow_redirects=allow_redirects,
                                 stream=stream, max_response_bytes=response_limit)
        url = _url(url, params)
        body = _body(data, json)
        request_args = (
            str(method).upper(), url,
            _headers(_merge_headers(self.headers, headers), _merge_cookies(self.cookies, cookies)), body,
            self.timeout if timeout is None else timeout, allow_redirects,
        )
        request = self._engine.request(*request_args)
        try:
            native = request.start_stream()
            reader = _SyncBodyReader(request, response_limit)
            if stream:
                return Response(native.status_code, native.headers, b"", url, reader)
            content = b"".join(reader)
        except RuntimeError as error:
            raise RequestException(str(error))
        return Response(native.status_code, native.headers, content, url)

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)

    def options(self, url, **kwargs):
        return self.request("OPTIONS", url, **kwargs)

    def head(self, url, **kwargs):
        return self.request("HEAD", url, **kwargs)

    def post(self, url, data=None, json=None, **kwargs):
        return self.request("POST", url, data=data, json=json, **kwargs)

    def put(self, url, data=None, **kwargs):
        return self.request("PUT", url, data=data, **kwargs)

    def patch(self, url, data=None, **kwargs):
        return self.request("PATCH", url, data=data, **kwargs)

    def delete(self, url, **kwargs):
        return self.request("DELETE", url, **kwargs)

    def websocket(self, url, origin="", headers=None, timeout=None, proxy=None, proxies=None):
        self._ensure_open()
        proxy = proxy if proxy is not None else self.proxy
        if proxy is None:
            proxy = _proxy_from_proxies(url, self.proxies if proxies is None else proxies)
        if proxy != self.proxy:
            return Client(impersonate=self.impersonate, proxy=proxy, verify=self.verify).websocket(
                url, origin, headers, timeout)
        socket = self._engine.websocket(url, origin, _headers(headers, None), timeout)
        socket.connect()
        return socket

    def close(self):
        self._closed = True
        self._engine = None
        return None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()


Session = Client


class _AsyncBodyReader:
    def __init__(self, request, wake, chunks, state, timeout, limit):
        self._request = request
        self._wake = wake
        self._chunks = chunks
        self._state = state
        self._timeout = timeout
        self._limit = limit
        self._total = 0
        self._closed = False

    async def _next_event(self):
        while not self._closed:
            if self._chunks:
                return ("body", 0, b"", self._chunks.popleft(), None)
            if self._state["error"] is not None:
                return ("error", 0, b"", b"", self._state["error"])
            if self._state["done"]:
                return ("done", 0, b"", b"", None)
            self._wake.clear()
            try:
                if self._timeout:
                    await asyncio.wait_for(self._wake.wait(), self._timeout)
                else:
                    await self._wake.wait()
            except asyncio.CancelledError:
                await self.aclose(cancel=True)
                raise
            except asyncio.TimeoutError:
                await self.aclose(cancel=True)
                raise Timeout("request timed out")
        return ("done", 0, b"", b"", None)

    def __aiter__(self):
        return self

    async def __anext__(self):
        while True:
            kind, _code, _headers, chunk, error = await self._next_event()
            if kind == "body":
                self._total += len(chunk)
                if self._limit is not None and self._total > self._limit:
                    await self.aclose(cancel=True)
                    raise ResponseTooLarge("response exceeded max_response_bytes=%d" % self._limit)
                return bytes(chunk)
            if kind == "done":
                await self.aclose()
                raise StopAsyncIteration
            if kind == "error":
                await self.aclose()
                raise RequestException(error or "request failed")

    async def aclose(self, cancel=False):
        if self._closed:
            return
        self._closed = True
        if cancel:
            try:
                self._request.cancel()
            except RuntimeError:
                pass
        try:
            self._request.detach_callback()
        except RuntimeError:
            pass
        self._wake.set()


class AsyncClient(Client):
    async def websocket(self, url, origin="", headers=None, timeout=None,
                        proxy=None, proxies=None):
        self._ensure_open()
        proxy = proxy if proxy is not None else self.proxy
        if proxy is None:
            proxy = _proxy_from_proxies(url, self.proxies if proxies is None else proxies)
        if proxy != self.proxy:
            child = AsyncClient(impersonate=self.impersonate, proxy=proxy, verify=self.verify)
            return await child.websocket(url, origin, headers, timeout)
        socket = self._engine.websocket(url, origin, _headers(headers, None), timeout)
        return await AsyncWebSocket._open(socket)

    async def request(self, method, url, params=None, data=None, json=None, headers=None, cookies=None,
                      timeout=None, allow_redirects=True, stream=False, impersonate=None,
                      proxy=None, proxies=None, verify=None, max_response_bytes=None):
        self._ensure_open()
        response_limit = self.max_response_bytes if max_response_bytes is None else _validate_max_response_bytes(max_response_bytes)
        proxy = proxy if proxy is not None else self.proxy
        if proxy is None:
            proxy = _proxy_from_proxies(url, self.proxies if proxies is None else proxies)
        request_options = {
            key: value for key, value in (
                ("impersonate", impersonate),
                ("proxy", proxy),
                ("verify", verify),
            ) if value is not None
        }
        if request_options and any(request_options.get(key, getattr(self, key)) != getattr(self, key) for key in request_options):
            child = AsyncClient(impersonate=request_options.get("impersonate", self.impersonate),
                                proxy=request_options.get("proxy", self.proxy),
                                verify=request_options.get("verify", self.verify),
                                timeout=self.timeout, headers=self.headers, cookies=self.cookies,
                                max_response_bytes=response_limit)
            return await child.request(method, url, params=params, data=data, json=json, headers=headers,
                                       cookies=cookies, timeout=timeout, allow_redirects=allow_redirects,
                                       stream=stream, max_response_bytes=response_limit)
        url = _url(url, params)
        request = self._engine.request(
            str(method).upper(), url,
            _headers(_merge_headers(self.headers, headers), _merge_cookies(self.cookies, cookies)),
            _body(data, json),
            self.timeout if timeout is None else timeout, allow_redirects,
        )
        loop = asyncio.get_running_loop() if hasattr(asyncio, "get_running_loop") else asyncio.get_event_loop()
        future = loop.create_future()
        wake = asyncio.Event()
        status = [0]
        response_headers = [b""]
        body = bytearray()
        body_total = [0]
        stream_chunks = deque()
        stream_state = {"done": False, "error": None}

        def notify():
            while True:
                event = request.poll_event()
                if event is None:
                    return
                kind, code, raw_headers, chunk, error = event
                if kind == "response":
                    status[0], response_headers[0] = code, raw_headers
                    if stream:
                        wake.set()
                        return
                elif kind == "body":
                    body_total[0] += len(chunk)
                    if response_limit is not None and body_total[0] > response_limit:
                        stream_state["error"] = "response exceeded max_response_bytes=%d" % response_limit
                        try:
                            request.cancel()
                        except RuntimeError:
                            pass
                        if not future.done():
                            future.set_exception(ResponseTooLarge(stream_state["error"]))
                        wake.set()
                        return
                    if stream:
                        stream_chunks.append(bytes(chunk))
                        wake.set()
                        return
                    else:
                        body.extend(chunk)
                elif kind == "error":
                    stream_state["error"] = error or "request failed"
                    if not future.done():
                        future.set_exception(RequestException(error or "request failed"))
                    return
                elif kind == "done":
                    stream_state["done"] = True
                    if not future.done():
                        future.set_result(AsyncResponse(status[0], response_headers[0], body, url))
                    wake.set()
                    return
                wake.set()

        try:
            request.start_async(loop, notify)
            if stream:
                while not status[0]:
                    await asyncio.wait_for(wake.wait(), timeout) if timeout else await wake.wait()
                    wake.clear()
                    if stream_state["error"] is not None:
                        raise RequestException(stream_state["error"])
                return AsyncResponse(status[0], response_headers[0], b"", url,
                                     _AsyncBodyReader(request, wake, stream_chunks,
                                                       stream_state, timeout, response_limit))
            return await asyncio.wait_for(future, timeout) if timeout else await future
        except asyncio.TimeoutError:
            try:
                request.cancel()
            except RuntimeError:
                pass
            try:
                request.detach_callback()
            except RuntimeError:
                pass
            raise Timeout("request timed out")
        except asyncio.CancelledError:
            try:
                request.cancel()
            except RuntimeError:
                pass
            try:
                request.detach_callback()
            except RuntimeError:
                pass
            raise

    async def get(self, url, **kwargs):
        return await self.request("GET", url, **kwargs)

    async def options(self, url, **kwargs):
        return await self.request("OPTIONS", url, **kwargs)

    async def head(self, url, **kwargs):
        return await self.request("HEAD", url, **kwargs)

    async def post(self, url, data=None, json=None, **kwargs):
        return await self.request("POST", url, data=data, json=json, **kwargs)

    async def put(self, url, data=None, **kwargs):
        return await self.request("PUT", url, data=data, **kwargs)

    async def patch(self, url, data=None, **kwargs):
        return await self.request("PATCH", url, data=data, **kwargs)

    async def delete(self, url, **kwargs):
        return await self.request("DELETE", url, **kwargs)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        await self.aclose()

    async def aclose(self):
        self.close()


AsyncSession = AsyncClient


class _RequestsFacade:
    def __init__(self):
        self._client = None

    def _get_client(self):
        if self._client is None:
            self._client = Client()
        return self._client

    def request(self, method, url, **kwargs):
        options = {}
        for key in ("impersonate", "proxy", "proxies", "verify", "timeout"):
            if key in kwargs:
                options[key] = kwargs.pop(key)
        if options:
            return Client(**options).request(method, url, **kwargs)
        return self._get_client().request(method, url, **kwargs)

    def __getattr__(self, name):
        if name in ("get", "options", "head", "post", "put", "patch", "delete"):
            return lambda url, **kwargs: self.request(name.upper(), url, **kwargs)
        return getattr(self._get_client(), name)


requests = _RequestsFacade()


class WebSocket:
    def __init__(self, url, client=None, impersonate=None, proxy=None, proxies=None, verify=True,
                 origin="", headers=None, timeout=None):
        client = client or Client(impersonate=impersonate, proxy=proxy,
                                  proxies=proxies, verify=verify)
        self._inner = client.websocket(url, origin, headers, timeout,
                                       proxy=proxy, proxies=proxies)

    def send(self, data):
        if isinstance(data, str):
            return self._inner.send_text(data)
        return self._inner.send_bytes(bytes(data))

    def recv(self):
        event = self._inner.recv()
        if event is None:
            return None
        kind, data, _code, error = event
        if kind == "error":
            raise RequestException(error or "WebSocket failure")
        return data.decode("utf-8", "replace") if kind == "text" else data

    def close(self, code=1000, reason=""):
        return self._inner.close(code, reason)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()


class AsyncWebSocket:
    def __init__(self, socket, loop):
        self._socket, self._loop = socket, loop
        self._events = deque()
        self._event_bytes = 0
        self._wake = asyncio.Event()
        self._recv_active = False
        self._closed = False

    @classmethod
    async def _open(cls, socket):
        loop = asyncio.get_running_loop() if hasattr(asyncio, "get_running_loop") else asyncio.get_event_loop()
        instance = cls(socket, loop)

        def notify():
            while True:
                event = socket.poll_event()
                if event is None:
                    return
                loop.call_soon_threadsafe(instance._enqueue, event)

        socket.start_async(loop, notify)
        return instance

    def _enqueue(self, event):
        if self._closed:
            return
        kind, data, _code, _error = event
        size = len(data)
        if len(self._events) >= 1024 or self._event_bytes + size > 4 * 1024 * 1024:
            self._closed = True
            try:
                self._socket.cancel()
            except RuntimeError:
                pass
            self._events.append(("error", b"", None, "WebSocket event buffer limit exceeded"))
        else:
            self._events.append(event)
            self._event_bytes += size
        self._wake.set()

    async def recv(self):
        if self._recv_active:
            raise RequestException("concurrent recv() is not supported")
        self._recv_active = True
        try:
            while not self._events:
                if self._closed:
                    return None
                self._wake.clear()
                await self._wake.wait()
            kind, data, _code, error = self._events.popleft()
            self._event_bytes = max(0, self._event_bytes - len(data))
        finally:
            self._recv_active = False
        if kind == "error":
            raise RequestException(error or "WebSocket failure")
        if kind in ("closed", "closing"):
            return None
        return data.decode("utf-8", "replace") if kind == "text" else data

    async def send(self, data):
        if isinstance(data, str):
            return self._socket.send_text(data)
        return self._socket.send_bytes(bytes(data))

    async def close(self, code=1000, reason=""):
        self._closed = True
        self._wake.set()
        try:
            self._socket.detach_callback()
        except RuntimeError:
            pass
        return self._socket.close(code, reason)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        await self.close()


def get(url, **kwargs):
    return requests.get(url, **kwargs)


def options(url, **kwargs):
    return requests.options(url, **kwargs)


def head(url, **kwargs):
    return requests.head(url, **kwargs)


def post(url, data=None, json=None, **kwargs):
    return requests.post(url, data=data, json=json, **kwargs)


def put(url, data=None, **kwargs):
    return requests.put(url, data=data, **kwargs)


def patch(url, data=None, **kwargs):
    return requests.patch(url, data=data, **kwargs)


def delete(url, **kwargs):
    return requests.delete(url, **kwargs)


__all__ = [
    "AsyncClient", "AsyncResponse", "AsyncSession", "AsyncWebSocket", "Client", "RequestException",
    "CaseInsensitiveDict", "Response", "ResponseTooLarge", "Session", "Timeout", "WebSocket", "delete", "get", "head",
    "options", "patch", "post", "put", "requests",
]
