"""Body encoding: form-urlencoded, JSON, and ``files=`` multipart.

The multipart renderer is private: ``files=`` is the only public spelling. CurlMime
was the curl_cffi-shaped second entry point and produced byte-identical bodies
for the same parts, so it went with the rest of that surface.
"""

import io
import json as _json
import mimetypes
import os
import uuid
from urllib.parse import urlencode

try:
    from collections.abc import Mapping
except ImportError:  # Python 3.6
    from collections import Mapping

from .exceptions import UnrewindableBodyError
from .utils import guess_filename, to_key_val_list


def _bytes(value, encoding="utf-8"):
    if value is None:
        return b""
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode(encoding)
    if isinstance(value, (bytearray, memoryview)):
        return bytes(value)
    return str(value).encode(encoding)


def encode_params(data, encoding="utf-8"):
    """Encodes ``data=`` as ``application/x-www-form-urlencoded``.

    Accepts mappings, sequences of pairs, and multi-valued entries -- the
    combinations requests accepts, including ``None`` values being dropped.
    """
    pairs = []
    for key, value in to_key_val_list(data) or ():
        if value is None:
            continue
        if isinstance(value, (list, tuple, set, frozenset)):
            items = list(value)
        else:
            items = [value]
        for item in items:
            if item is None:
                continue
            pairs.append((
                key.encode(encoding) if isinstance(key, str) else key,
                item.encode(encoding) if isinstance(item, str) else item,
            ))
    return urlencode(pairs, doseq=True)


#: Characters a ``Content-Disposition`` parameter must not carry literally.
#: ``"`` ends the quoted-string and CR/LF end the header line, so leaving any
#: of them in let a caller-supplied field name or filename inject extra part
#: headers (or a whole extra part) into the body.
_PARAM_ESCAPES = {34: "%22", 13: "%0D", 10: "%0A"}


def _escape_param(value, encoding="utf-8"):
    """Percent-escapes the characters that would break a quoted parameter.

    The same three replacements requests/urllib3 applies, so a caller porting
    from ``requests`` sees byte-identical bodies. Without this, a field name
    or filename containing a double quote or CRLF wrote raw header lines into
    the part: a field named with an embedded CRLF plus ``X-Injected: yes``
    emitted a real ``X-Injected: yes`` header inside the body.
    """
    text = value.decode(encoding) if isinstance(value, bytes) else str(value)
    return text.translate(_PARAM_ESCAPES)


class Part(object):
    """One multipart section."""

    __slots__ = ("name", "filename", "content_type", "data", "headers",
                 "content_transfer_encoding")

    def __init__(self, name, data=b"", filename=None, content_type=None,
                 headers=None, content_transfer_encoding=None):
        self.name = name
        self.filename = filename
        self.content_type = content_type
        self.data = data
        self.headers = headers
        self.content_transfer_encoding = content_transfer_encoding

    def render(self, encoding="utf-8"):
        disposition = 'form-data; name="%s"' % _escape_param(self.name, encoding)
        if self.filename is not None:
            disposition += '; filename="%s"' % _escape_param(self.filename, encoding)
        lines = [b"Content-Disposition: " + _bytes(disposition, encoding)]
        if self.content_type:
            lines.append(b"Content-Type: " + _bytes(self.content_type, encoding))
        if self.content_transfer_encoding:
            lines.append(b"Content-Transfer-Encoding: "
                         + _bytes(self.content_transfer_encoding, encoding))
        for name, value in (self.headers or {}).items() if isinstance(self.headers, Mapping) \
                else (self.headers or ()):
            lines.append(_bytes(name, encoding) + b": " + _bytes(value, encoding))
        return b"\r\n".join(lines) + b"\r\n\r\n" + _bytes(self.data, encoding) + b"\r\n"


class _MultipartBody(object):
    """Renderer for ``files=`` bodies. Private: no public entry point builds one.

    The class exists because ``encode_multipart`` needs somewhere to accumulate Part
    objects before serialising them. It used to be public as CurlMime, a
    curl_cffi-shaped second entry point; ``files=`` produces byte-identical bodies
    for the same parts, so the constructor and the ``multipart=`` keyword were
    removed together.
    """

    def __init__(self, parts=None):
        self._parts = []
        self.boundary = None
        for part in parts or ():
            self.addpart(**part)

    def addpart(self, name, content_type=None, filename=None, local_path=None,
                data=None, content_transfer_encoding=None, headers=None):
        if local_path is not None:
            with open(local_path, "rb") as handle:
                data = handle.read()
            if filename is None:
                filename = os.path.basename(local_path)
            if content_type is None:
                content_type = mimetypes.guess_type(local_path)[0] or \
                    "application/octet-stream"
        elif hasattr(data, "read"):
            handle = data
            data = handle.read()
            if filename is None:
                filename = guess_filename(handle)
        self._parts.append(Part(name, data if data is not None else b"", filename,
                                content_type, headers, content_transfer_encoding))
        return self

    def close(self):
        self._parts = []

    def __len__(self):
        return len(self._parts)

    def encode(self, boundary=None, encoding="utf-8"):
        """Returns ``(body_bytes, content_type_header)``."""
        boundary = boundary or self.boundary or uuid.uuid4().hex
        marker = _bytes("--" + boundary, encoding)
        chunks = []
        for part in self._parts:
            chunks.append(marker + b"\r\n" + part.render(encoding))
        chunks.append(marker + b"--\r\n")
        return b"".join(chunks), "multipart/form-data; boundary=" + boundary


def _normalize_file_entry(field, value):
    """Turns any requests-accepted ``files=`` value into a ``Part``."""
    filename = None
    content_type = None
    headers = None
    if isinstance(value, (tuple, list)):
        if len(value) == 2:
            filename, handle = value
        elif len(value) == 3:
            filename, handle, content_type = value
        elif len(value) == 4:
            filename, handle, content_type, headers = value
        else:
            raise ValueError("files entry %r must have 2 to 4 elements" % (field,))
    else:
        handle = value
        # requests falls back to the field name when the object carries no
        # usable .name attribute. Without that fallback the part has no
        # filename parameter at all, so the server sees a plain form field
        # instead of a file upload -- measured: files={"f": BytesIO(b"BODY")}
        # rendered name="f" where requests renders name="f"; filename="f".
        filename = guess_filename(handle) or field
    if hasattr(handle, "read"):
        if hasattr(handle, "seek") and hasattr(handle, "tell"):
            try:
                handle.seek(handle.tell())
            except (OSError, ValueError):
                raise UnrewindableBodyError("files entry %r is not rewindable" % (field,))
        data = handle.read()
    else:
        data = handle
    # Deliberately no Content-Type guess here. requests -- and therefore
    # curl_cffi, which this module is compatible with -- never invents a part
    # type: a 2-tuple renders as bare Content-Disposition, and only a caller
    # who passes one explicitly gets a Content-Type line. Guessing from the
    # filename looked harmless and was not: measured against a server that
    # stores what it receives, files={"f": ("a.html", b"<script>...")} arrived
    # as `text/html` while requests sends it with no type at all, so an upload
    # endpoint that echoes the stored type started serving attacker HTML.
    # The old fallback also labelled every unknown suffix
    # `application/octet-stream`, which is a second invented claim. A part with
    # no type is the browser behaviour: RFC 7578 §4.4 makes the type optional.
    return Part(field, data if data is not None else b"", filename, content_type, headers)


def encode_multipart(data, files, boundary=None, encoding="utf-8"):
    """Builds a ``multipart/form-data`` body from ``data=`` plus ``files=``."""
    mime = _MultipartBody()
    for key, value in to_key_val_list(data) or ():
        if value is None:
            continue
        items = value if isinstance(value, (list, tuple, set, frozenset)) else [value]
        for item in items:
            if item is None:
                continue
            if hasattr(item, "read"):
                mime._parts.append(_normalize_file_entry(key, item))
            else:
                mime._parts.append(Part(key, _bytes(item, encoding)))
    for field, value in to_key_val_list(files) or ():
        entries = value if isinstance(value, list) and value and \
            isinstance(value[0], (tuple, list)) else [value]
        for entry in entries:
            mime._parts.append(_normalize_file_entry(field, entry))
    return mime.encode(boundary=boundary, encoding=encoding)


def json_body(value, encoding="utf-8", dumps=None):
    dumps = dumps or _json.dumps
    text = dumps(value, allow_nan=False) if dumps is _json.dumps else dumps(value)
    if isinstance(text, bytes):
        return text
    return text.encode(encoding)


def iter_body(source, chunk_size=65536):
    """Yields chunks from a file object, iterable, or bytes-like body."""
    if source is None:
        return
    if isinstance(source, (bytes, bytearray, memoryview)):
        yield bytes(source)
        return
    if isinstance(source, str):
        yield source.encode("utf-8")
        return
    if hasattr(source, "read"):
        while True:
            chunk = source.read(chunk_size)
            if not chunk:
                return
            yield _bytes(chunk)
        return
    for chunk in source:
        yield _bytes(chunk)


def is_stream_body(value):
    """True when a body must be uploaded chunked rather than as a fixed buffer.

    Sequences of pairs are form data, not streams -- only file objects,
    generators, and other one-shot iterators upload chunked.  Getting this wrong
    turns ``data=[("a", "1")]`` into the repr of its tuples on the wire.
    """
    if value is None or isinstance(value, (bytes, bytearray, memoryview, str)):
        return False
    if isinstance(value, (Mapping, list, tuple, set, frozenset)):
        return False
    return hasattr(value, "read") or hasattr(value, "__next__") \
        or hasattr(value, "__aiter__")


def body_length(value):
    if isinstance(value, (bytes, bytearray, memoryview)):
        return len(value)
    if isinstance(value, str):
        return len(value.encode("utf-8"))
    if isinstance(value, io.IOBase):
        try:
            position = value.tell()
            value.seek(0, os.SEEK_END)
            total = value.tell()
            value.seek(position)
            return total - position
        except (OSError, ValueError):
            return None
    return None
