"""Request and response models.

``Response`` carries the requests surface (``reason``, ``history``, ``elapsed``,
``cookies``, ``links``, ``apparent_encoding``) plus the curl_cffi additions
(``http_version``, ``redirect_count``, ``charset``, ``infos``).  Anything ABI v8
cannot report is derived here and documented rather than faked: the Core sends a
numeric status with no reason phrase, so ``reason`` comes from the standard
table.
"""

import codecs
import datetime
import json as _json
from urllib.parse import urlsplit, urlunsplit

try:
    from collections.abc import Mapping
except ImportError:  # Python 3.6
    from collections import Mapping

from . import multipart
from .cookies import RequestsCookieJar, cookiejar_from_dict
from .exceptions import (HTTPError, InvalidURL, JSONDecodeError, MissingSchema,
                         StreamConsumedError, URLRequired)
from .status_codes import REASONS
from .structures import Headers
from .utils import (get_encoding_from_headers, get_encodings_from_content,
                    guess_json_utf, parse_header_links, requote_uri)

CONTENT_CHUNK_SIZE = 10 * 1024
ITER_CHUNK_SIZE = 512
REDIRECT_STATI = (301, 302, 303, 307, 308)
DEFAULT_REDIRECT_LIMIT = 30


def parse_raw_headers(raw, encoding="iso-8859-1"):
    """Parses the Core's raw header block into ``(status_line, Headers)``.

    The Core hands over Chromium's ``raw_headers()`` with NULs turned into
    newlines, so the first line is the status line and duplicates are separate
    lines -- which is why ``Set-Cookie`` survives intact here.
    """
    if isinstance(raw, (bytes, bytearray, memoryview)):
        raw = bytes(raw).decode(encoding, "replace")
    headers = Headers()
    status_line = ""
    for index, line in enumerate(str(raw).splitlines()):
        if not line.strip():
            continue
        if index == 0 and ":" not in line.split(" ", 1)[0]:
            status_line = line.strip()
            continue
        if ":" not in line:
            continue
        name, value = line.split(":", 1)
        headers.add(name.strip(), value.strip())
    return status_line, headers


def reason_from_status_line(status_line, status_code):
    parts = status_line.split(None, 2) if status_line else []
    if len(parts) >= 3 and parts[1].isdigit():
        return parts[2]
    return REASONS.get(status_code, "")


def http_version_from_status_line(status_line):
    if not status_line:
        return None
    token = status_line.split(None, 1)[0].upper()
    return {"HTTP/1.0": "HTTP/1.0", "HTTP/1.1": "HTTP/1.1",
            "HTTP/2": "HTTP/2", "HTTP/2.0": "HTTP/2",
            "HTTP/3": "HTTP/3", "HTTP/3.0": "HTTP/3"}.get(token, token or None)


def build_url(url, params, encoding="utf-8", quote=None):
    """Appends ``params`` to *url* with requests' encoding rules.

    ``quote=False`` skips the percent-encoding pass, matching curl_cffi's escape
    hatch for URLs that are already exactly as the caller wants them on the wire.
    """
    if isinstance(url, bytes):
        url = url.decode("utf-8")
    else:
        url = str(url)
    url = url.strip()
    if ":" in url and not url.lower().startswith("http"):
        # Opaque schemes (mailto:, data:) are passed through untouched, as
        # requests does, so the Core can reject them with its own message.
        return url
    if not url:
        raise URLRequired("a URL is required")
    parts = urlsplit(url)
    if not parts.scheme:
        raise MissingSchema(
            "Invalid URL %r: No scheme supplied. Perhaps you meant https://%s?"
            % (url, url))
    if not parts.netloc:
        raise InvalidURL("Invalid URL %r: No host supplied" % (url,))
    query = parts.query
    extra = _encode_params(params, encoding)
    if extra:
        query = "%s&%s" % (query, extra) if query else extra
    rebuilt = urlunsplit((parts.scheme, parts.netloc, parts.path or "/",
                          query, parts.fragment))
    return rebuilt if quote is False else requote_uri(rebuilt)


def _encode_params(params, encoding="utf-8"):
    if params is None or params == "" or params == b"":
        return ""
    if isinstance(params, bytes):
        return params.decode("ascii", "replace")
    if isinstance(params, str):
        return params
    return multipart.encode_params(params, encoding)


class Request(object):
    """User-facing request description, as in ``requests.models.Request``."""

    def __init__(self, method=None, url=None, headers=None, files=None, data=None,
                 params=None, auth=None, cookies=None, hooks=None, json=None):
        self.method = method
        self.url = url
        self.headers = headers
        self.files = files
        self.data = data if data is not None else {}
        self.params = params if params is not None else {}
        self.auth = auth
        self.cookies = cookies
        self.json = json
        self.hooks = {"response": []}
        for event, hook in (hooks or {}).items():
            self.register_hook(event, hook)

    def __repr__(self):
        return "<Request [%s]>" % (self.method,)

    def register_hook(self, event, hook):
        if event not in self.hooks:
            raise ValueError('Unsupported event specified, with event name "%s"' % event)
        if callable(hook):
            self.hooks[event].append(hook)
        elif hasattr(hook, "__iter__"):
            self.hooks[event].extend(item for item in hook if callable(item))

    def deregister_hook(self, event, hook):
        try:
            self.hooks[event].remove(hook)
            return True
        except ValueError:
            return False

    def prepare(self):
        prepared = PreparedRequest()
        prepared.prepare(method=self.method, url=self.url, headers=self.headers,
                         files=self.files, data=self.data, json=self.json,
                         params=self.params, auth=self.auth, cookies=self.cookies,
                         hooks=self.hooks)
        return prepared


class PreparedRequest(object):
    """The wire-ready request the Core is handed.

    ``body`` is either ``bytes`` (fixed upload) or a chunk iterator/file object,
    in which case ``stream_body`` is set and the Core uploads chunked.
    """

    def __init__(self):
        self.method = None
        self.url = None
        self.headers = None
        self.body = None
        self.hooks = {"response": []}
        self._cookies = None
        self._body_position = None
        #: Set when ``body`` must be uploaded chunk by chunk.
        self.stream_body = False
        #: Emission order for headers, when a caller pins one.
        self.header_order = None
        #: ``False`` disables the percent-encoding pass over the URL.
        self.quote = None

    def __repr__(self):
        return "<PreparedRequest [%s]>" % (self.method,)

    def copy(self):
        clone = PreparedRequest()
        clone.method = self.method
        clone.url = self.url
        clone.headers = self.headers.copy() if self.headers is not None else None
        clone._cookies = self._cookies.copy() if self._cookies is not None else None
        clone.body = self.body
        clone.hooks = self.hooks
        clone._body_position = self._body_position
        clone.stream_body = self.stream_body
        clone.header_order = self.header_order
        clone.quote = self.quote
        return clone

    def prepare(self, method=None, url=None, headers=None, files=None, data=None,
                params=None, auth=None, cookies=None, hooks=None, json=None):
        self.prepare_method(method)
        self.prepare_url(url, params)
        self.prepare_headers(headers)
        self.prepare_cookies(cookies)
        self.prepare_body(data, files, json)
        self.prepare_auth(auth, self.url)
        self.prepare_hooks(hooks)

    def prepare_method(self, method):
        self.method = method.upper() if method is not None else None

    def prepare_url(self, url, params):
        self.url = build_url(url, params, quote=self.quote)

    def prepare_headers(self, headers):
        self.headers = Headers()
        if headers:
            if isinstance(headers, Headers):
                for name, value in headers.multi_items():
                    if value is None:
                        continue
                    self.headers.add(name, value)
            else:
                for name, value in (headers.items() if isinstance(headers, Mapping)
                                    else headers):
                    if value is None:
                        continue
                    self.headers[name] = value

    def prepare_body(self, data, files, json=None):
        body = None
        content_type = None
        if not data and json is not None:
            content_type = "application/json"
            body = multipart.json_body(json)
        elif files:
            body, content_type = multipart.encode_multipart(data, files)
        elif data:
            if multipart.is_stream_body(data):
                body = data
                self.stream_body = True
            elif isinstance(data, (bytes, bytearray, memoryview)):
                body = bytes(data)
            elif isinstance(data, str):
                body = data.encode("utf-8")
            else:
                body = multipart.encode_params(data).encode("utf-8")
                content_type = "application/x-www-form-urlencoded"
        elif isinstance(data, (bytes, bytearray, str)):
            body = data.encode("utf-8") if isinstance(data, str) else bytes(data)
        self.body = body
        self.prepare_content_length(body)
        if content_type and "content-type" not in self.headers:
            self.headers["Content-Type"] = content_type

    def prepare_content_length(self, body):
        if self.stream_body:
            self.headers.pop("Content-Length", None)
            self.headers["Transfer-Encoding"] = "chunked"
            return
        if body is not None:
            self.headers["Content-Length"] = str(len(body))
        elif self.method not in ("GET", "HEAD") and \
                "content-length" not in self.headers:
            self.headers["Content-Length"] = "0"

    def prepare_auth(self, auth, url=""):
        if auth is None:
            return
        if isinstance(auth, tuple) and len(auth) == 2:
            from .auth import HTTPBasicAuth
            auth = HTTPBasicAuth(*auth)
        if not callable(auth):
            raise TypeError("auth must be a 2-tuple or a callable")
        prepared = auth(self)
        if prepared is not None:
            self.__dict__.update(prepared.__dict__)
        self.prepare_content_length(self.body)

    def prepare_cookies(self, cookies):
        if isinstance(cookies, RequestsCookieJar):
            self._cookies = cookies
        else:
            self._cookies = cookiejar_from_dict(cookies)

    def prepare_hooks(self, hooks):
        for event, hook in (hooks or {}).items():
            if event not in self.hooks:
                self.hooks[event] = []
            if callable(hook):
                self.hooks[event].append(hook)
            elif hasattr(hook, "__iter__"):
                self.hooks[event].extend(item for item in hook if callable(item))

    @property
    def path_url(self):
        parts = urlsplit(self.url)
        path = parts.path or "/"
        return "%s?%s" % (path, parts.query) if parts.query else path

    def wire_headers(self):
        """Header pairs in emission order.

        ``header_order`` only reorders what the caller already supplied; it never
        invents a header, because the Chromium profile owns the default set and
        their ordering.
        """
        pairs = self.headers.multi_items()
        if not self.header_order:
            return pairs
        ranking = {name.lower(): index for index, name in enumerate(self.header_order)}
        fallback = len(ranking)
        return sorted(pairs, key=lambda item: ranking.get(item[0].lower(), fallback))


class RawStream(object):
    """Minimal file-like view over a streaming body.

    ``requests`` exposes ``response.raw`` as a urllib3 ``HTTPResponse``.  There is
    no urllib3 here, so this provides the part callers actually use -- ``read``,
    ``stream``, and ``close`` -- over the Core's body stream.  urllib3-specific
    attributes are absent rather than faked.
    """

    def __init__(self, response):
        self._response = response
        self._buffer = b""
        self._iterator = None
        self._closed = False
        self.decode_content = True

    def _chunks(self):
        if self._iterator is None:
            self._iterator = self._response.iter_content(CONTENT_CHUNK_SIZE)
        return self._iterator

    def read(self, amt=None, decode_content=None, cache_content=False):
        if self._closed:
            return b""
        if amt is None:
            data = self._buffer + b"".join(self._chunks())
            self._buffer = b""
            self._closed = True
            return data
        while len(self._buffer) < amt:
            try:
                self._buffer += next(self._chunks())
            except StopIteration:
                break
        data, self._buffer = self._buffer[:amt], self._buffer[amt:]
        return data

    def stream(self, amt=CONTENT_CHUNK_SIZE, decode_content=None):
        while True:
            block = self.read(amt)
            if not block:
                return
            yield block

    def readinto(self, target):
        data = self.read(len(target))
        target[:len(data)] = data
        return len(data)

    def readable(self):
        return True

    def writable(self):
        return False

    def seekable(self):
        return False

    def tell(self):
        raise IOError("the Core body stream is not seekable")

    @property
    def closed(self):
        return self._closed

    def close(self):
        self._closed = True
        self._response.close()


class Response(object):
    """HTTP response with the requests and curl_cffi read surfaces."""

    __attrs__ = ["_content", "status_code", "headers", "url", "history",
                 "encoding", "reason", "cookies", "elapsed", "request"]

    def __init__(self):
        self._content = False
        self._content_consumed = False
        self._body_reader = None
        self._raw = None
        self._next = None
        self.status_code = None
        self.headers = Headers()
        self.url = None
        self.encoding = None
        self.history = []
        self.reason = None
        self.cookies = RequestsCookieJar()
        self.elapsed = datetime.timedelta(0)
        self.request = None
        self.default_encoding = "utf-8"
        self.http_version = None
        self.redirect_count = 0
        self.redirect_url = None
        self.infos = {}
        self.status_line = ""
        #: ``(url, Headers)`` per redirect hop, used to mirror per-hop cookies.
        self.history_headers = []

    def __repr__(self):
        return "<Response [%s]>" % (self.status_code,)

    def __bool__(self):
        return self.ok

    __nonzero__ = __bool__

    def __iter__(self):
        return self.iter_content(128)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    def __getstate__(self):
        if not self._content_consumed:
            self.content
        return {name: getattr(self, name, None) for name in self.__attrs__}

    def __setstate__(self, state):
        for name, value in state.items():
            setattr(self, name, value)
        self._content_consumed = True
        self._body_reader = None
        self._raw = None
        self._next = None

    @property
    def ok(self):
        try:
            self.raise_for_status()
        except HTTPError:
            return False
        return True

    @property
    def raw(self):
        """File-like view over the body, as ``requests`` exposes it."""
        if self._raw is None:
            self._raw = RawStream(self)
        return self._raw

    @raw.setter
    def raw(self, value):
        self._raw = value

    @property
    def is_redirect(self):
        return "location" in self.headers and self.status_code in REDIRECT_STATI

    @property
    def is_permanent_redirect(self):
        return "location" in self.headers and self.status_code in (301, 308)

    @property
    def next(self):
        return self._next

    @property
    def apparent_encoding(self):
        """Encoding guessed from the body, matching requests' fallback order."""
        content = self._content if isinstance(self._content, bytes) else b""
        if not content:
            return None
        detected = get_encodings_from_content(content[:4096])
        if detected:
            return detected[0]
        for candidate in ("utf-8", "gb18030", "big5", "shift_jis", "euc-kr"):
            try:
                content.decode(candidate)
                return candidate
            except (UnicodeDecodeError, LookupError):
                continue
        return "iso-8859-1"

    @property
    def charset(self):
        """curl_cffi alias for the resolved response encoding."""
        fallback = self.default_encoding if isinstance(self.default_encoding, str) \
            else "utf-8"
        return self.encoding or self.charset_encoding or fallback

    @property
    def charset_encoding(self):
        return get_encoding_from_headers(self.headers)

    @property
    def content(self):
        if self._content is False:
            if self._body_reader is None:
                self._content = b""
            else:
                self._content = b"".join(self._body_reader)
                self._body_reader = None
            self._content_consumed = True
        return self._content

    @content.setter
    def content(self, value):
        self._content = bytes(value)
        self._content_consumed = True

    @property
    def text(self):
        if not self.content:
            return ""
        encoding = self.encoding or self.charset_encoding
        if encoding is None:
            encoding = self.apparent_encoding or self.default_encoding
        if callable(self.default_encoding) and self.encoding is None \
                and self.charset_encoding is None:
            encoding = self.default_encoding(self.content)
        try:
            return self.content.decode(encoding, "replace")
        except (LookupError, TypeError):
            return self.content.decode(self.default_encoding
                                       if isinstance(self.default_encoding, str)
                                       else "utf-8", "replace")

    def json(self, **kwargs):
        if not self.content:
            raise JSONDecodeError("Expecting value: line 1 column 1 (char 0)")
        if self.encoding is None and len(self.content) > 3:
            guessed = guess_json_utf(self.content)
            if guessed is not None:
                try:
                    return _json.loads(self.content.decode(guessed), **kwargs)
                except UnicodeDecodeError:
                    pass
                except ValueError as error:
                    raise JSONDecodeError(str(error))
        try:
            return _json.loads(self.text, **kwargs)
        except ValueError as error:
            raise JSONDecodeError(str(error))

    @property
    def links(self):
        header = self.headers.get("link")
        links = {}
        if header:
            for link in parse_header_links(header):
                key = link.get("rel") or link.get("url")
                links[key] = link
        return links

    def raise_for_status(self):
        reason = self.reason or ""
        if isinstance(reason, bytes):
            reason = reason.decode("utf-8", "replace")
        message = ""
        if self.status_code is not None and 400 <= self.status_code < 500:
            message = "%s Client Error: %s for url: %s" % (self.status_code, reason, self.url)
        elif self.status_code is not None and 500 <= self.status_code < 600:
            message = "%s Server Error: %s for url: %s" % (self.status_code, reason, self.url)
        if message:
            raise HTTPError(message, response=self)
        return self

    def iter_content(self, chunk_size=1, decode_unicode=False):
        if chunk_size is not None and not isinstance(chunk_size, int):
            raise TypeError("chunk_size must be an int or None")
        if chunk_size is not None and chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        chunk_size = chunk_size or CONTENT_CHUNK_SIZE
        if self._body_reader is not None:
            generator = self._stream_chunks(chunk_size)
        elif self._content_consumed and self._content is False:
            raise StreamConsumedError("the response body was already consumed")
        else:
            body = self.content
            generator = (body[offset:offset + chunk_size]
                         for offset in range(0, len(body), chunk_size))
        if decode_unicode:
            return self._decode_stream(generator)
        return generator

    def _stream_chunks(self, chunk_size):
        reader, self._body_reader = self._body_reader, None
        self._content_consumed = True
        pending = b""
        try:
            for chunk in reader:
                pending += chunk
                while len(pending) >= chunk_size:
                    yield pending[:chunk_size]
                    pending = pending[chunk_size:]
        finally:
            reader.close()
        if pending:
            yield pending

    def _decode_stream(self, generator):
        encoding = self.encoding or self.charset_encoding or self.default_encoding
        decoder = codecs.getincrementaldecoder(
            encoding if isinstance(encoding, str) else "utf-8")(errors="replace")
        for chunk in generator:
            text = decoder.decode(chunk)
            if text:
                yield text
        tail = decoder.decode(b"", True)
        if tail:
            yield tail

    def iter_lines(self, chunk_size=ITER_CHUNK_SIZE, decode_unicode=False,
                   delimiter=None):
        pending = None
        for chunk in self.iter_content(chunk_size=chunk_size,
                                       decode_unicode=decode_unicode):
            if pending is not None:
                chunk = pending + chunk
            lines = chunk.split(delimiter) if delimiter else chunk.splitlines()
            if lines and lines[-1] and chunk and lines[-1][-1] == chunk[-1]:
                pending = lines.pop()
            else:
                pending = None
            for line in lines:
                yield line
        if pending is not None:
            yield pending

    def close(self):
        if self._body_reader is not None:
            self._body_reader.close(cancel=True)
            self._body_reader = None
        self._content_consumed = True


class AsyncResponse(Response):
    """Response whose streaming body is consumed with ``async for``."""

    def __init__(self):
        Response.__init__(self)
        self._async_body_reader = None

    async def acontent(self):
        if self._async_body_reader is not None:
            chunks = []
            async for chunk in self._async_body_reader:
                chunks.append(chunk)
            self._async_body_reader = None
            self._content = b"".join(chunks)
            self._content_consumed = True
        return self.content

    async def atext(self):
        await self.acontent()
        return self.text

    async def ajson(self, **kwargs):
        await self.acontent()
        return self.json(**kwargs)

    async def aiter_content(self, chunk_size=None, decode_unicode=False):
        chunk_size = chunk_size or CONTENT_CHUNK_SIZE
        if self._async_body_reader is not None:
            reader, self._async_body_reader = self._async_body_reader, None
            self._content_consumed = True
            pending = b""
            try:
                async for chunk in reader:
                    pending += chunk
                    while len(pending) >= chunk_size:
                        block = pending[:chunk_size]
                        pending = pending[chunk_size:]
                        yield block.decode(self.charset, "replace") if decode_unicode \
                            else block
            finally:
                await reader.aclose()
            if pending:
                yield pending.decode(self.charset, "replace") if decode_unicode else pending
            return
        for chunk in self.iter_content(chunk_size, decode_unicode=decode_unicode):
            yield chunk

    #: curl_cffi spells the byte stream ``aiter_content``; earlier releases of
    #: this package spelled it ``aiter_bytes``.  Both are kept.
    aiter_bytes = aiter_content

    async def aiter_lines(self, chunk_size=None, decode_unicode=False, delimiter=None):
        pending = None
        async for chunk in self.aiter_content(chunk_size=chunk_size,
                                             decode_unicode=decode_unicode):
            if pending is not None:
                chunk = pending + chunk
            lines = chunk.split(delimiter) if delimiter else chunk.splitlines()
            if lines and lines[-1] and chunk and lines[-1][-1] == chunk[-1]:
                pending = lines.pop()
            else:
                pending = None
            for line in lines:
                yield line
        if pending is not None:
            yield pending

    async def aclose(self):
        if self._async_body_reader is not None:
            await self._async_body_reader.aclose(cancel=True)
            self._async_body_reader = None
        self._content_consumed = True
