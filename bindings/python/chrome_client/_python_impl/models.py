"""Request and response models.

``Response`` carries the requests surface (``reason``, ``history``, ``elapsed``,
``cookies``, ``links``, ``apparent_encoding``) plus the curl_cffi additions
(``http_version``, ``redirect_count``, ``charset``, ``infos``).  Anything ABI v8
cannot report is derived here and documented rather than faked: the Core sends a
numeric status with no reason phrase, so ``reason`` comes from the standard
table.
"""

import asyncio
import codecs
import datetime
import json as _json
from collections import deque
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
#: requests' own default redirect cap (30), kept because
#: ``requests.models.DEFAULT_REDIRECT_LIMIT`` is a public name callers import.
#: This package no longer uses it: Chromium enforces 20 hops inside one
#: ``URLRequest`` (``net::URLRequest::kMaxRedirects``), so a cap at or above 20
#: is rejected with ``UnsupportedFeature`` instead of being clamped to 20, and
#: the facade's own default is ``None`` -- "use Chromium's ceiling". Passing 30
#: here would advertise a limit this build cannot enforce.
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
    """Maps a status line onto the protocol token it literally carries.

    This is only trustworthy when the caller knows the response was not
    normalized by an intermediary. Chromium rewrites an HTTP/2 status line as
    ``HTTP/1.1 200``, so on its own this function cannot tell h1 from h2.
    Prefer :func:`_negotiated_http_version`.

    Args:
        status_line: e.g. ``"HTTP/2 200"``.

    Returns:
        The mapped name, or ``None`` when the line is empty.

    Example:
        >>> http_version_from_status_line("HTTP/2 200")
        'HTTP/2'
    """
    parts = status_line.split(None, 1) if status_line else []
    if not parts:
        return None
    token = parts[0].upper()
    return {"HTTP/1.0": "HTTP/1.0", "HTTP/1.1": "HTTP/1.1",
            "HTTP/2": "HTTP/2", "HTTP/2.0": "HTTP/2",
            "HTTP/3": "HTTP/3", "HTTP/3.0": "HTTP/3"}.get(token, token or None)


def _decode_failure(error, response):
    """Rebuilds a parser failure as this package's :class:`JSONDecodeError`.

    ``json.JSONDecodeError`` carries ``msg``/``doc``/``pos``, so passing them
    on keeps both the formatted message ("Expecting value: line 1 column 6
    (char 5)") and the position attributes the stdlib error offers. A bare
    ``ValueError`` -- from a caller's own ``parse_float``, say -- has no
    position to report, so it is re-raised unchanged rather than wrapped in a
    fabricated "line 1 column 1"; it still reaches ``except ValueError``.
    """
    if isinstance(error, _json.JSONDecodeError):
        return JSONDecodeError(error.msg, error.doc, error.pos, response=response)
    return error


def compose_status_line(status_code, reason_phrase):
    """Builds ``HTTP/1.1 NNN Reason`` from the fields the Core sends.

    Chromium normalizes every protocol onto an HTTP/1.1 status line
    (``net/spdy/spdy_http_utils.cc``), so this reproduces exactly what the raw
    block used to carry. It exists because ``Response.status_line`` is public
    surface and the response callback no longer forwards the line itself --
    the reason phrase is now its own ABI parameter.

    Args:
        status_code: The numeric status.
        reason_phrase: The reason the Core extracted, or ``None``/empty.

    Returns:
        e.g. ``"HTTP/1.1 200 OK"``.
    """
    phrase = reason_phrase or ""
    return ("HTTP/1.1 %d %s" % (status_code, phrase)).strip()


def _negotiated_http_version(status_line):
    """The protocol the response actually arrived over, or ``None``.

    ABI v8 reports only a numeric status plus the header block, and Chromium
    normalizes every HTTP/2 and HTTP/3 response onto an ``HTTP/1.1`` status
    line (``net/spdy/spdy_http_utils.cc`` builds ``"HTTP/1.1 " + status``).

    That makes the status line almost useless as a protocol witness:

    * ``HTTP/1.1`` is ambiguous -- it is what a real HTTP/1.1 response says
      *and* what a normalized HTTP/2 or HTTP/3 response says, so it proves
      nothing.
    * ``HTTP/1.0`` is unambiguous: nothing normalizes down to 1.0.
    * ``HTTP/2``/``HTTP/3`` can only appear if the peer wrote them verbatim,
      which Chromium does not do, so they are reported when seen.

    ``None`` therefore means "not knowable from this ABI", which is the honest
    answer and the same treatment ``reason`` gets. Callers that need the real
    value need the ABI field tracked in docs/NEXT_STEPS.md; until then this
    must not be guessed, because a wrong fingerprint claim is worse than an
    absent one.
    """
    token = http_version_from_status_line(status_line)
    if token in ("HTTP/1.0", "HTTP/2", "HTTP/3"):
        return token
    return None


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
        #: Cookies the caller supplied for THIS request only, as opposed to the
        #: session jar merged into _cookies for requests-compatibility. The store
        #: already holds the session own, so only these are written through per
        #: request (see BaseSession._resolve_cookies).
        self._cookie_additions = None
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
        clone._cookie_additions = (list(self._cookie_additions)
                                   if self._cookie_additions is not None else None)
        clone.body = self.body
        clone.hooks = self.hooks
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
        #: The reader an ``iter_content()`` call has already claimed. Kept so
        #: ``close()`` can still cancel it: the claim transfers ownership out
        #: of ``_body_reader``, and without this the body kept downloading in
        #: the background after ``close()`` returned.
        self._claimed_reader = None
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
        # ``__attrs__`` carries only the serialisable payload. ``__init__`` also
        # establishes live handles and derived metadata that are deliberately
        # left out of it, so a restored response used to be missing them
        # entirely: ``.text``/``.charset`` raised AttributeError on
        # ``default_encoding``, and ``http_version``/``status_line``/``infos``/
        # ``redirect_count``/``redirect_url``/``history_headers`` were gone even
        # though this class's type surface declares them. Re-establish every
        # default from a fresh instance before applying the payload, so the
        # set cannot drift as fields are added.
        for name, value in type(self)().__dict__.items():
            setattr(self, name, value)
        for name, value in state.items():
            setattr(self, name, value)
        self._content_consumed = True

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
        """Returns the body as bytes.

        Draining it here consumes a stream, so ``iter_content()`` cannot be used
        afterwards. Reading a body that is already in memory is free.

        Raises:
            StreamConsumedError: A stream reader already took the body, or
                ``close()`` cancelled it. Returning ``b""`` instead would claim
                an empty body for one that had bytes.
        """
        if self._content is False:
            if self._content_consumed:
                # A reader already took the body: streaming consumed it, or
                # close() cancelled it. Returning b"" here reported an empty
                # body for a response that had one.
                raise StreamConsumedError(
                    "the response body was already consumed")
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
        # A live reader would otherwise keep serving the native body, so the
        # object answered with two different bodies: ``.content`` and
        # ``.text`` returned the assigned bytes while ``iter_content()`` and
        # ``iter_lines()`` streamed the discarded one. Release it.
        if self._body_reader is not None:
            reader, self._body_reader = self._body_reader, None
            reader.close(cancel=True)
        # The same applies to a reader an ``iter_content()`` call already
        # claimed: leaving it live let the abandoned native body keep
        # streaming while ``.content`` answered with the assigned bytes.
        if self._claimed_reader is not None:
            reader, self._claimed_reader = self._claimed_reader, None
            reader.close(cancel=True)

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
            raise JSONDecodeError("Expecting value", "", 0, response=self)
        if self.encoding is None and len(self.content) > 3:
            guessed = guess_json_utf(self.content)
            if guessed is not None:
                try:
                    return _json.loads(self.content.decode(guessed), **kwargs)
                except UnicodeDecodeError:
                    pass
                except ValueError as error:
                    raise _decode_failure(error, self)
        try:
            return _json.loads(self.text, **kwargs)
        except ValueError as error:
            raise _decode_failure(error, self)

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
        """Iterates the body in chunks.

        Three cases, in this order:

        1. A stream reader is still live: it is claimed by this call (so a
           second ``iter_content()`` raises rather than racing), and the body
           is streamed from the Core.
        2. The body is gone -- already streamed away, or cancelled by
           ``close()``: raises :class:`StreamConsumedError`.
        3. The body is in memory: it is sliced, so the call is repeatable.

        Args:
            chunk_size: Bytes per yielded block. ``None`` means
                :data:`CONTENT_CHUNK_SIZE`.
            decode_unicode: Decode with the response encoding and yield ``str``.
                Decoding is incremental, so a multi-byte character split across
                two chunks is carried over rather than replaced.

        Returns:
            An iterator of ``bytes``, or of ``str`` when ``decode_unicode``.

        Raises:
            TypeError: ``chunk_size`` is not an int or ``None``.
            ValueError: ``chunk_size`` is not positive.
            StreamConsumedError: The body was already consumed.
        """
        if chunk_size is not None and not isinstance(chunk_size, int):
            raise TypeError("chunk_size must be an int or None")
        if chunk_size is not None and chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        chunk_size = chunk_size or CONTENT_CHUNK_SIZE
        if self._body_reader is not None:
            # Bind the reader here, not on the generator's first next(): the
            # body runs lazily, so two iterators over one response used to race
            # for self._body_reader and the loser dereferenced None.
            reader, self._body_reader = self._body_reader, None
            self._claimed_reader = reader
            self._content_consumed = True
            generator = self._stream_chunks(reader, chunk_size)
        elif self._content_consumed and self._content is False:
            raise StreamConsumedError("the response body was already consumed")
        else:
            body = self.content
            generator = (body[offset:offset + chunk_size]
                         for offset in range(0, len(body), chunk_size))
        if decode_unicode:
            return self._decode_stream(generator)
        return generator

    def _stream_chunks(self, reader, chunk_size):
        """Re-chunks the Core's buffers without re-copying them.

        The Core hands over one immutable buffer per read.  A naive re-chunker
        appends those to a bytes accumulator and slices one block off at a time,
        which copies the whole remaining accumulator on every slice: a 4 MiB
        body read in 512-byte blocks copies roughly 16 GiB.  This one keeps the
        buffers in a deque with a read offset and hands a buffer out unchanged
        whenever it fills a block exactly, so the common case -- a Core read at
        least as large as the requested block -- costs one view and no copy at
        all.  Only a block that straddles two buffers is joined.

        Args:
            reader: The body reader to drain.
            chunk_size: Bytes per yielded block.

        Yields:
            Blocks of exactly ``chunk_size`` bytes, then the remainder.
        """
        pending = deque()
        available = 0
        try:
            for chunk in reader:
                if chunk:
                    # [buffer, offset]: the offset is where the next unread byte
                    # of that buffer starts.  A list, not a tuple, so advancing
                    # does not allocate.
                    pending.append([chunk, 0])
                    available += len(chunk)
                while available >= chunk_size:
                    block, consumed = self._take(pending, chunk_size)
                    available -= consumed
                    yield block
            if available:
                block, _consumed = self._take(pending, available)
                yield block
        finally:
            pending.clear()
            reader.close()
            if self._claimed_reader is reader:
                self._claimed_reader = None

    @staticmethod
    def _take(pending, size):
        """Takes exactly *size* bytes off the front of *pending*.

        Args:
            pending: A deque of ``[buffer, offset]`` entries, oldest first.
            size: How many bytes to take.

        Returns:
            A ``(block, consumed)`` pair.  ``consumed`` is how many bytes left
            the deque, which is *size* except when a short final buffer was
            handed out whole.
        """
        head, offset = pending[0]
        remaining_in_head = len(head) - offset
        if remaining_in_head == size:
            pending.popleft()
            # The buffer already is the block: hand it over without copying.
            return (head if offset == 0 else head[offset:]), size
        if remaining_in_head > size:
            end = offset + size
            pending[0][1] = end
            return head[offset:end], size
        pieces = [head[offset:]]
        taken = remaining_in_head
        pending.popleft()
        while taken < size:
            head, offset = pending[0]
            needed = size - taken
            remaining_in_head = len(head) - offset
            if remaining_in_head <= needed:
                pieces.append(head if offset == 0 else head[offset:])
                taken += remaining_in_head
                pending.popleft()
            else:
                end = offset + needed
                pieces.append(head[offset:end])
                pending[0][1] = end
                taken += needed
        return b"".join(pieces), size

    def _text_decoder(self):
        """Returns an incremental decoder for the resolved encoding.

        Both the synchronous and the asynchronous stream decode through this,
        so a multi-byte character split across two chunks is carried over in
        each: decoding each block on its own turned the tail of every split
        character into U+FFFD.
        """
        encoding = self.encoding or self.charset_encoding or self.default_encoding
        return codecs.getincrementaldecoder(
            encoding if isinstance(encoding, str) else "utf-8")(errors="replace")

    def _decode_stream(self, generator):
        decoder = self._text_decoder()
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
        """Releases the body, cancelling a live stream.

        Afterwards the body is gone: ``.content`` and ``iter_content()`` raise
        :class:`StreamConsumedError`. requests raises a bare ``RuntimeError`` for
        the same state; that is not a ``RequestException``, so this is stricter and
        catchable the same way as every other failure here.
        """
        if self._body_reader is not None:
            self._body_reader.close(cancel=True)
            self._body_reader = None
        # An ``iter_content()`` call has taken ownership of the reader, so
        # ``_body_reader`` is already None here and the cancellation above
        # did nothing. Measured: a 400 KB body kept arriving in full after
        # ``close()`` returned when the caller held an iterator. Cancel the
        # claimed reader too; the generator's own ``finally`` then finds it
        # closed and is a no-op.
        if self._claimed_reader is not None:
            reader, self._claimed_reader = self._claimed_reader, None
            reader.close(cancel=True)
        self._content_consumed = True

    def _release_async_body(self):
        """Cancels an abandoned asynchronous stream, if this is one.

        Overridden by :class:`AsyncResponse`. The base response has no async
        reader, so this is a no-op for the synchronous class.
        """


class AsyncResponse(Response):
    """Response whose streaming body is consumed with ``async for``."""

    def __init__(self):
        Response.__init__(self)
        self._async_body_reader = None
        #: The reader an ``aiter_content()`` call has already claimed. Same
        #: reason as the synchronous ``_claimed_reader``: the claim moves the
        #: reader out of ``_async_body_reader``, so every cancellation path
        #: that only looks there silently skips a stream the caller already
        #: started. Measured: a claimed 400 KB body kept arriving in full
        #: after ``aclose()`` returned.
        self._claimed_async_reader = None

    def __del__(self):
        """Releases a stream the caller never closed.

        Every explicit path (``aclose()``, the ``content`` setter,
        ``_release_async_body``) requires the caller to act. A response that is
        simply dropped has no such hook, and its reader stays reachable from the
        Core callback (``notify -> _AsyncState -> native request``), so neither
        the socket nor the buffered body is ever collected.

        Measured over a never-ending chunked body: 20 abandoned responses left
        46 file descriptors and 20 server-side connections open for the life of
        the process, while 20 properly closed ones left the count unchanged.

        ``__del__`` can run on a GC thread with no running loop, which is why
        the synchronous release is used: scheduling a coroutine on a loop that
        will never run it would leak the very request this is releasing.
        """
        try:
            self._release_async_body()
        except Exception:
            # A destructor that raises is reported as "Exception ignored in
            # __del__" and the object still leaks, so failure here is swallowed
            # deliberately rather than allowed to escape.
            pass

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
        """Iterates the body in chunks, without a worker thread.

        Same contract as :meth:`iter_content`, and the same zero-copy
        re-chunking: the Core hands over one buffer per read and this hands it
        out unchanged whenever it fills a block exactly.  The earlier version
        kept a bytes accumulator and sliced one block off at a time, which
        copied the whole remainder on every slice.

        Args:
            chunk_size: Bytes per yielded block. ``None`` means
                :data:`CONTENT_CHUNK_SIZE`.
            decode_unicode: Decode with the response encoding and yield ``str``.

        Yields:
            Blocks of ``bytes``, or of ``str`` when ``decode_unicode``.

        Raises:
            TypeError: ``chunk_size`` is not an int or ``None``.
            ValueError: ``chunk_size`` is not positive.
            StreamConsumedError: The body was already consumed.
        """
        # Same contract as the synchronous twin. Without it a negative value
        # made the two slices below leave the pending buffer unchanged while
        # still satisfying the loop condition, so the inner loop yielded empty
        # blocks forever: measured 20 chunks of [95, 0, 0, 0, ...] for a
        # 100-byte body, with no end in sight. A zero was silently rewritten
        # to the default block size instead of being rejected.
        if chunk_size is not None and not isinstance(chunk_size, int):
            raise TypeError("chunk_size must be an int or None")
        if chunk_size is not None and chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        chunk_size = chunk_size or CONTENT_CHUNK_SIZE
        if self._async_body_reader is not None:
            reader, self._async_body_reader = self._async_body_reader, None
            self._claimed_async_reader = reader
            self._content_consumed = True
            decoder = self._text_decoder() if decode_unicode else None
            try:
                async for block in self._astream_blocks(reader, chunk_size):
                    if decoder is None:
                        yield block
                    else:
                        text = decoder.decode(block)
                        if text:
                            yield text
            finally:
                # cancel=True: an abandoned generator (the caller broke out of
                # `async for` or dropped the iterator) runs this block during
                # GC. Without the cancel the reader only detaches its callback
                # and the Core request keeps its socket in the engine pool.
                # Measured: 20 abandoned streams over a never-ending body left
                # 46 file descriptors and 20 server connections open forever.
                await reader.aclose(cancel=True)
                if self._claimed_async_reader is reader:
                    self._claimed_async_reader = None
            if decoder is not None:
                tail = decoder.decode(b"", True)
                if tail:
                    yield tail
            return
        for chunk in self.iter_content(chunk_size, decode_unicode=decode_unicode):
            yield chunk

    async def _astream_blocks(self, reader, chunk_size):
        """Re-chunks the asynchronous body reader without re-copying it.

        The mirror of :meth:`_stream_chunks`, sharing its :meth:`_take` so the
        two paths cannot drift: same alignment rule, same remainder handling,
        same "hand an aligned buffer out unchanged" case.

        Args:
            reader: The asynchronous body reader to drain.
            chunk_size: Bytes per yielded block.

        Yields:
            Blocks of exactly ``chunk_size`` bytes, then the remainder.
        """
        pending = deque()
        available = 0
        async for chunk in reader:
            if chunk:
                pending.append([chunk, 0])
                available += len(chunk)
            while available >= chunk_size:
                block, consumed = self._take(pending, chunk_size)
                available -= consumed
                yield block
        if available:
            block, _consumed = self._take(pending, available)
            yield block

    #: curl_cffi spells the byte stream ``aiter_content``; earlier releases of
    #: this package spelled it ``aiter_bytes``.  Both are kept.
    aiter_bytes = aiter_content

    @Response.content.setter
    def content(self, value):
        # Same rule as the synchronous setter, for the asynchronous reader:
        # leaving it alive made ``aiter_content()`` stream the discarded native
        # body while ``.content`` reported the assigned bytes. ``aclose()`` is a
        # coroutine and a property setter cannot await one, so the synchronous
        # release path is used: it cancels the Core request without a loop.
        if self._async_body_reader is not None:
            reader, self._async_body_reader = self._async_body_reader, None
            reader.release_without_loop(cancel=True)
        # And the reader an ``aiter_content()`` call already claimed, for the
        # same reason: it is no longer reachable through the field above.
        if self._claimed_async_reader is not None:
            reader, self._claimed_async_reader = self._claimed_async_reader, None
            reader.release_without_loop(cancel=True)
        Response.content.fset(self, value)

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
        # ``aiter_content()`` has taken ownership of the reader, so the field
        # above is already None when the caller holds an iterator. Without
        # this the cancellation was skipped and the body kept downloading.
        if self._claimed_async_reader is not None:
            reader, self._claimed_async_reader = self._claimed_async_reader, None
            await reader.aclose(cancel=True)
        self._content_consumed = True

    def _release_async_body(self):
        reader, self._async_body_reader = self._async_body_reader, None
        # The reader a started ``aiter_content()`` owns is no longer in the
        # field above; abandoning that response leaks exactly what the
        # comment below describes. Release both, preferring the claimed one
        # when a caller somehow left both set.
        if self._claimed_async_reader is not None:
            reader, self._claimed_async_reader = self._claimed_async_reader, None
        self._content_consumed = True
        if reader is None:
            return
        # The request has to be cancelled from the loop that owns it. Without
        # this an abandoned stream stays reachable from the Core callback (the
        # callback holds notify -> _AsyncState -> the native request) and can
        # never be collected, so the socket, the loop timer and up to
        # STREAM_BUFFER_LIMIT of buffered body leak for the process lifetime.
        try:
            loop = reader.state.loop
        except AttributeError:
            return
        if loop.is_closed():
            return
        # `asyncio.get_running_loop` is 3.7+. The package still installs on 3.6
        # (cp36-abi3 wheel), where calling it raised AttributeError past the
        # `except RuntimeError` guard and leaked the coroutine. Ask for the
        # loop the way the other 3.6-compatible call sites do.
        try:
            get_running = asyncio.get_running_loop
        except AttributeError:
            get_running = None
        try:
            running = get_running() if get_running else asyncio.get_event_loop()
        except RuntimeError:
            running = None
        if running is loop:
            loop.create_task(reader.aclose(cancel=True))
            return
        if loop.is_running():
            # Another thread owns the loop; hop onto it.
            asyncio.run_coroutine_threadsafe(reader.aclose(cancel=True), loop)
            return
        # The loop exists but is not running, so nothing would ever await a
        # scheduled coroutine: `run_coroutine_threadsafe` succeeds, the future
        # is never resolved, and the reader keeps its native request (and the
        # socket, and the buffered body) alive. Drive the cancellation to
        # completion here instead.
        #
        # `cancel=True` is not optional here: without it the reader only detaches
        # its callback and never cancels the Core request, so the socket stays in
        # the engine's pool. Measured: 20 abandoned streams over a never-ending
        # body leaked 46 file descriptors and 20 server connections with the
        # cancel-less call, and released every one of them with `cancel=True`.
        reader.release_without_loop(cancel=True)

    def close(self):
        Response.close(self)
        self._release_async_body()
