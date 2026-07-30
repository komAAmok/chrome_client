"""
Response and exception classes for chrome_client.
"""

import json as json_lib
from typing import Dict, List, Any, Optional
from dataclasses import dataclass, field
from datetime import timedelta

from ._cookies import CookieJar


class CaseInsensitiveDict(dict):
    """Small dict-compatible header mapping with case-insensitive lookup."""

    def __getitem__(self, key):
        match = next((name for name in self if name.lower() == key.lower()), None)
        if match is None:
            raise KeyError(key)
        return super().__getitem__(match)

    def __contains__(self, key):
        return isinstance(key, str) and any(
            name.lower() == key.lower() for name in self.keys()
        )

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default


def _split_complete_lines(data, separator=None):
    if separator is not None:
        lines = data.split(separator)
        return lines[:-1], lines[-1]
    lines = []
    start = 0
    index = 0
    while index < len(data):
        byte = data[index]
        if byte == 10:
            lines.append(data[start:index])
            start = index + 1
        elif byte == 13:
            if index + 1 == len(data):
                break
            lines.append(data[start:index])
            start = index + 2 if data[index + 1] == 10 else index + 1
            if data[index + 1] == 10:
                index += 1
        index += 1
    return lines, data[start:]


def _iter_lines(chunks, encoding, decode_unicode=False, delimiter=None):
    separator = delimiter.encode(encoding) if isinstance(delimiter, str) else delimiter
    pending = b""
    for chunk in chunks:
        lines, pending = _split_complete_lines(pending + chunk, separator)
        for line in lines:
            yield line.decode(encoding, errors="replace") if decode_unicode else line
    if pending:
        line = pending.rstrip(b"\r") if separator is None else pending
        yield line.decode(encoding, errors="replace") if decode_unicode else line

class RequestError(Exception):
    """Base request error, compatible with requests.RequestException."""


class Timeout(RequestError):
    pass


class ConnectionError(RequestError):
    pass


class ProxyError(ConnectionError):
    pass


class SSLError(ConnectionError):
    pass


class HTTPStatusError(RequestError):
    """HTTP status code error, compatible with requests.HTTPError."""

    def __init__(self, message: str, response=None, request=None):
        super().__init__(message)
        self.response = response
        self.request = request


class Request:
    """User request model with the common requests.Request constructor."""

    def __init__(self, method=None, url=None, headers=None, files=None, data=None,
                 params=None, auth=None, cookies=None, hooks=None, json=None):
        self.method = method
        self.url = url
        self.headers = dict(headers or {})
        self.files = files
        self.data = data
        self.params = params
        self.auth = auth
        self.cookies = cookies
        self.hooks = hooks
        self.json = json


@dataclass
class PreparedRequest:
    method: str
    url: str
    headers: Dict[str, str] = field(default_factory=dict)
    body: Any = None


@dataclass
class Response:
    """HTTP response object - compatible with requests.Response"""
    status_code: int
    _headers: Dict[str, List[str]]
    content: bytes
    url: str = ""
    _cookies: CookieJar = field(default_factory=CookieJar)
    encoding: Optional[str] = None
    reason: str = ""
    history: List['Response'] = field(default_factory=list)
    request: Optional[PreparedRequest] = None
    elapsed: timedelta = field(default_factory=timedelta)
    raw: Any = None

    @property
    def headers(self) -> Dict[str, str]:
        """Return headers dictionary (take first value)"""
        return CaseInsensitiveDict(
            (k, v[0] if v else "") for k, v in self._headers.items()
        )

    @property
    def cookies(self) -> CookieJar:
        """Return response cookies (CookieJar object)"""
        return self._cookies

    def _get_encoding(self) -> str:
        """Get response encoding"""
        if self.encoding:
            return self.encoding

        # Try to get encoding from Content-Type header
        content_type = self.headers.get('content-type', '').lower()
        if 'charset=' in content_type:
            try:
                charset = content_type.split('charset=')[1].split(';')[0].strip()
                return charset
            except (IndexError, LookupError):
                pass

        # Default to utf-8
        return 'utf-8'

    @property
    def text(self) -> str:
        """Return response text"""
        encoding = self._get_encoding()
        return self.content.decode(encoding, errors='replace')

    def json(self, **kwargs) -> Any:
        """Parse JSON response"""
        return json_lib.loads(self.text, **kwargs)

    @property
    def ok(self) -> bool:
        """Check if status code indicates success"""
        return self.status_code < 400

    def raise_for_status(self):
        """Raise exception if status code indicates error"""
        if 400 <= self.status_code < 600:
            raise HTTPStatusError(
                f"{self.status_code} Error", response=self, request=self.request
            )

    @property
    def is_redirect(self) -> bool:
        return self.status_code in (301, 302, 303, 307, 308) and bool(
            self.headers.get("location")
        )

    def iter_content(self, chunk_size: Optional[int] = 1, decode_unicode: bool = False):
        if chunk_size is None:
            chunk_size = len(self.content) or 1
        if not isinstance(chunk_size, int) or chunk_size <= 0:
            raise ValueError("chunk_size must be a positive integer or None")
        for offset in range(0, len(self.content), chunk_size):
            chunk = self.content[offset:offset + chunk_size]
            yield chunk.decode(self._get_encoding(), errors="replace") if decode_unicode else chunk

    def iter_lines(self, chunk_size: int = 512, decode_unicode: bool = False,
                   delimiter=None):
        return _iter_lines(
            self.iter_content(chunk_size), self._get_encoding(),
            decode_unicode, delimiter,
        )

    def close(self):
        pass

    def __bool__(self):
        return self.ok


class StreamResponse:
    """Streaming HTTP response - compatible with requests stream=True style.

    Usage (sync):
        response = session.get(url, stream=True)
        for chunk in response.iter_content(8192):
            f.write(chunk)

    Usage (async):
        response = await session.get(url, stream=True)
        async for chunk in response.aiter_content(8192):
            f.write(chunk)
    """

    def __init__(self, reader, url: str = "", cookies: Optional['CookieJar'] = None,
                 encoding: Optional[str] = None, session=None):
        self._reader = reader
        self._status_code: int = reader.status_code
        self._raw_headers: List = list(reader.headers)
        self.url: str = url
        self._cookies: CookieJar = cookies or CookieJar()
        self.encoding: Optional[str] = encoding
        self._content: Optional[bytes] = None
        self._closed: bool = False
        self._session = session
        self.reason: str = ""
        self.history: List[Response] = []
        self.request = None
        self.elapsed = timedelta()
        self.raw = reader

        # Parse raw headers into dict
        self._headers: Dict[str, List[str]] = {}
        for name, value in self._raw_headers:
            lower_name = name.lower()
            if lower_name not in self._headers:
                self._headers[lower_name] = []
            self._headers[lower_name].append(value)

    @property
    def status_code(self) -> int:
        return self._status_code

    @property
    def headers(self) -> Dict[str, str]:
        """Return headers dictionary (take first value)"""
        return CaseInsensitiveDict(
            (k, v[0] if v else "") for k, v in self._headers.items()
        )

    @property
    def cookies(self) -> 'CookieJar':
        return self._cookies

    @property
    def ok(self) -> bool:
        return self._status_code < 400

    def _get_encoding(self) -> str:
        if self.encoding:
            return self.encoding
        content_type = self.headers.get('content-type', '').lower()
        if 'charset=' in content_type:
            try:
                charset = content_type.split('charset=')[1].split(';')[0].strip()
                return charset
            except (IndexError, LookupError):
                pass
        return 'utf-8'

    def raise_for_status(self):
        if 400 <= self._status_code < 600:
            raise HTTPStatusError(
                f"{self._status_code} Error", response=self, request=self.request
            )

    @property
    def is_redirect(self) -> bool:
        return self.status_code in (301, 302, 303, 307, 308) and bool(
            self.headers.get("location")
        )

    # ---- Sync iteration ----

    def iter_content(self, chunk_size: Optional[int] = None):
        """Iterate over response data chunks (sync generator).

        Args:
            chunk_size: If set, re-chunk data into pieces of this size.
                        If None, yield raw chunks as received from network.
        """
        if self._closed:
            return
        if chunk_size is not None and (
            not isinstance(chunk_size, int) or chunk_size <= 0
        ):
            raise ValueError("chunk_size must be a positive integer or None")

        if self._content is not None:
            size = chunk_size or len(self._content) or 1
            for offset in range(0, len(self._content), size):
                yield self._content[offset:offset + size]
            return

        buf = b""
        while True:
            chunk = self._reader.next_chunk_sync()
            if chunk is None:
                if buf:
                    yield buf
                break
            if chunk_size is None:
                yield chunk
            else:
                buf += chunk
                while len(buf) >= chunk_size:
                    yield buf[:chunk_size]
                    buf = buf[chunk_size:]

    def iter_lines(self, chunk_size: int = 512, decode_unicode: bool = False,
                   delimiter: Optional[str] = None):
        """Iterate over response lines (sync generator).

        Args:
            chunk_size: Internal read buffer size.
            delimiter: Line delimiter. Default: None (auto-detect \\n or \\r\\n).
        """
        return _iter_lines(
            self.iter_content(chunk_size), self._get_encoding(),
            decode_unicode, delimiter,
        )

    # ---- Async iteration ----

    async def aiter_content(self, chunk_size: Optional[int] = None):
        """Iterate over response data chunks (async generator)."""
        if self._closed:
            return
        if chunk_size is not None and (
            not isinstance(chunk_size, int) or chunk_size <= 0
        ):
            raise ValueError("chunk_size must be a positive integer or None")

        if self._content is not None:
            size = chunk_size or len(self._content) or 1
            for offset in range(0, len(self._content), size):
                yield self._content[offset:offset + size]
            return

        buf = b""
        while True:
            chunk = await self._reader.next_chunk()
            if chunk is None:
                if buf:
                    yield buf
                break
            if chunk_size is None:
                yield chunk
            else:
                buf += chunk
                while len(buf) >= chunk_size:
                    yield buf[:chunk_size]
                    buf = buf[chunk_size:]

    async def aiter_lines(self, chunk_size: int = 512, decode_unicode: bool = False,
                          delimiter: Optional[str] = None):
        """Iterate over response lines (async generator)."""
        pending = b""
        delim_bytes = delimiter.encode(self._get_encoding()) if isinstance(delimiter, str) else delimiter

        async for chunk in self.aiter_content(chunk_size=chunk_size):
            lines, pending = _split_complete_lines(pending + chunk, delim_bytes)
            for line in lines:
                yield line.decode(self._get_encoding(), errors='replace') if decode_unicode else line

        if pending:
            if delim_bytes is None:
                pending = pending.rstrip(b'\r')
            yield pending.decode(self._get_encoding(), errors='replace') if decode_unicode else pending

    async def acontent(self) -> bytes:
        if self._content is None:
            self._content = b"".join([chunk async for chunk in self.aiter_content()])
        return self._content

    async def atext(self) -> str:
        return (await self.acontent()).decode(self._get_encoding(), errors='replace')

    # ---- Drain helpers ----

    @property
    def content(self) -> bytes:
        """Read entire remaining body. Drains the stream."""
        if self._content is None:
            chunks = []
            for chunk in self.iter_content():
                chunks.append(chunk)
            self._content = b"".join(chunks)
        return self._content

    @property
    def text(self) -> str:
        return self.content.decode(self._get_encoding(), errors='replace')

    def json(self, **kwargs) -> Any:
        return json_lib.loads(self.text, **kwargs)

    # ---- Resource management ----

    def close(self):
        if not self._closed:
            self._closed = True
            if self._reader is not None:
                self._reader.close()
            if self._session is not None:
                try:
                    close = getattr(self._session, "_close_sync", self._session.close)
                    close()
                except Exception:
                    pass
                self._session = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.aclose()

    async def aclose(self):
        if not self._closed:
            self._closed = True
            if self._reader is not None:
                self._reader.close()
            if self._session is not None:
                close = self._session.close()
                if hasattr(close, "__await__"):
                    await close
                self._session = None

    def __del__(self):
        self.close()

    def __bool__(self):
        return self.ok
