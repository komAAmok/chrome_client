"""Type surface of :mod:`chrome_client.multipart`.

Body encoding: form data, JSON, ``files=`` multipart, and ``CurlMime``.

``data=`` accepts every shape requests accepts -- a mapping, a sequence of pairs,
repeated values, ``None`` values being dropped -- and ``files=`` accepts the
2/3/4-tuple forms.  When a part is a file object, generator or async iterator the
body is uploaded chunked instead of buffered.
"""

from typing import Any, Iterable, Iterator, Mapping, Optional, Tuple

from typing_extensions import TypeAlias

from ._types import FilesLike

#: A rendered part plus the content type of the whole body.
EncodedMultipart: TypeAlias = Tuple[bytes, str]
#: Anything :func:`iter_body` can drain: a file object, an iterable of chunks,
#: bytes, str, or ``None``.
BodySource: TypeAlias = Any


class Part:
    """One multipart section.

    Attributes:
        name: The form field name.
        filename: The ``filename`` parameter, or ``None`` for a plain field.
        content_type: The part's ``Content-Type``, or ``None``.
        data: The part body.
        headers: Extra part headers, as a mapping or a sequence of pairs.
        content_transfer_encoding: Value for ``Content-Transfer-Encoding``.
    """

    name: str
    filename: Optional[str]
    content_type: Optional[str]
    data: Any
    headers: Any
    content_transfer_encoding: Optional[str]

    def __init__(
        self,
        name: str,
        data: Any = b"",
        filename: Optional[str] = None,
        content_type: Optional[str] = None,
        headers: Any = None,
        content_transfer_encoding: Optional[str] = None,
    ) -> None:
        """Describes one part.

        Args:
            name: Form field name.
            data: Part body; str is encoded as UTF-8 when rendered.
            filename: Optional filename.
            content_type: Optional part content type.
            headers: Extra headers for this part.
            content_transfer_encoding: Optional transfer encoding.
        """
        ...

    def render(self, encoding: str = ...) -> bytes:
        """Renders the part, including its headers and trailing CRLF.

        Args:
            encoding: Codec for str values.

        Returns:
            The part as bytes, ready to append after the boundary line.

        Example:
            >>> Part("title", "hello").render()
            b'Content-Disposition: form-data; name="title"\\r\\n\\r\\nhello\\r\\n'
        """
        ...


class CurlMime:
    """curl_cffi-compatible multipart builder.

    ``close()`` is accepted for source compatibility; nothing is held open
    because parts are read eagerly.

    Attributes:
        boundary: The boundary to use, or ``None`` to generate one per encode.

    Example:
        >>> mime = CurlMime()
        >>> mime.addpart(name="title", data="hello")
        CurlMime(...)
        >>> mime.addpart(name="photo", filename="p.jpg",
        ...              content_type="image/jpeg", local_path="/tmp/p.jpg")
        >>> session.post(url, multipart=mime)
    """

    boundary: Optional[str]

    def __init__(self, parts: Optional[Iterable[Mapping[str, Any]]] = None) -> None:
        """Creates the builder, optionally from ready-made part descriptions.

        Args:
            parts: Mappings of :meth:`addpart` keyword arguments.
        """
        ...

    def addpart(
        self,
        name: str,
        content_type: Optional[str] = None,
        filename: Optional[str] = None,
        local_path: Optional[str] = None,
        data: Any = None,
        content_transfer_encoding: Optional[str] = None,
        headers: Any = None,
    ) -> "CurlMime":
        """Adds one part.

        Args:
            name: Form field name.
            content_type: Part content type. Guessed from ``local_path`` or
                ``filename`` when omitted.
            filename: Filename to advertise.
            local_path: Path to read the part from; the file is read eagerly.
            data: Part body; a file object is read here.
            content_transfer_encoding: Optional transfer encoding.
            headers: Extra headers for this part.

        Returns:
            The builder itself, so calls chain.

        Example:
            >>> CurlMime().addpart("a", data="1").addpart("b", data="2")
        """
        ...

    @classmethod
    def from_list(cls, files: Iterable[Mapping[str, Any]]) -> "CurlMime":
        """Builds a multipart body from a list of part descriptions.

        Args:
            files: Mappings of :meth:`addpart` keyword arguments.

        Returns:
            The populated builder.

        Example:
            >>> CurlMime.from_list([{"name": "title", "data": "hello"}])
        """
        ...

    def attach(self, curl: Any = None) -> "CurlMime":
        """Present for curl_cffi source compatibility.

        Args:
            curl: Ignored; there is no libcurl handle here.

        Returns:
            The builder itself.
        """
        ...

    def close(self) -> None:
        """Drops the buffered parts, releasing their memory.

        Parts are read eagerly, so there is nothing else to close.
        """
        ...

    def __len__(self) -> int:
        """Returns how many parts were added.

        Returns:
            The part count.
        """
        ...

    def encode(self, boundary: Optional[str] = None, encoding: str = ...) -> EncodedMultipart:
        """Renders every part into a complete body.

        Args:
            boundary: Boundary to use; the instance's own, or a random hex one.
            encoding: Codec for str values.

        Returns:
            ``(body_bytes, content_type_header)``, where the content type already
            carries ``boundary=``.

        Example:
            >>> body, content_type = CurlMime([{"name": "a", "data": "1"}]).encode()
            >>> content_type.startswith("multipart/form-data; boundary=")
            True
        """
        ...


def encode_params(data: Any, encoding: str = ...) -> str:
    """Encodes ``data=`` as ``application/x-www-form-urlencoded``.

    Accepts mappings, sequences of pairs and multi-valued entries -- the
    combinations requests accepts, with ``None`` values dropped.

    Args:
        data: The form data.
        encoding: Codec for keys and values.

    Returns:
        The encoded query string, without a leading separator.

    Example:
        >>> encode_params({"q": "a b", "n": [1, 2]})
        'q=a+b&n=1&n=2'
    """
    ...


def encode_multipart(
    data: Any,
    files: FilesLike,
    boundary: Optional[str] = None,
    encoding: str = ...,
) -> EncodedMultipart:
    """Builds a ``multipart/form-data`` body from ``data=`` plus ``files=``.

    Args:
        data: Plain fields; a file-like value among them becomes its own part.
        files: File parts, alone or alongside ``data``.
        boundary: Boundary to use; generated when omitted.
        encoding: Codec for str values.

    Returns:
        ``(body_bytes, content_type_header)``.

    Raises:
        UnrewindableBodyError: A file object cannot be rewound to be read.
        ValueError: A ``files`` entry tuple has other than 2--4 elements.

    Example:
        >>> encode_multipart({"title": "t"}, {"f": ("a.txt", b"...")})[1]
        'multipart/form-data; boundary=...'
    """
    ...


def json_body(value: Any, encoding: str = ..., dumps: Optional[Any] = None) -> bytes:
    """Serialises a JSON body.

    Args:
        value: Any JSON-serialisable object.
        encoding: Codec for the result when the serialiser returns ``str``.
        dumps: Serialiser, ``json.dumps`` by default. When the default is used,
            ``allow_nan=False`` is applied so ``NaN``/``Infinity`` are rejected
            rather than emitted as invalid JSON.

    Returns:
        The body bytes.

    Raises:
        TypeError: ``value`` is not serialisable.
        ValueError: The default serialiser was used and ``value`` contains
            ``NaN`` or ``Infinity``.
    """
    ...


def iter_body(source: Any, chunk_size: int = ...) -> Iterator[bytes]:
    """Yields chunks from a file object, iterable, or bytes-like body.

    Args:
        source: A file object, an iterable of chunks, bytes, str, or ``None``.
        chunk_size: Bytes read per call when ``source`` is a file object.

    Yields:
        Non-empty byte chunks. ``None`` yields nothing, and a streamed body is
        what makes the Core upload with ``Transfer-Encoding: chunked``.

    Example:
        >>> list(iter_body(b"abc"))
        [b'abc']
    """
    ...


def is_stream_body(value: Any) -> bool:
    """Reports whether a body must be uploaded chunked rather than buffered.

    Sequences of pairs are form data, not streams -- only file objects,
    generators and other one-shot iterators upload chunked. Getting this wrong
    turns ``data=[("a", "1")]`` into the repr of its tuples on the wire.

    Args:
        value: The ``data=`` argument.

    Returns:
        ``True`` for a file object or iterator, ``False`` for bytes/str,
        mappings, lists, tuples and sets.

    Example:
        >>> is_stream_body([("a", "1")]), is_stream_body(open("/dev/null", "rb"))
        (False, True)
    """
    ...


def body_length(value: Any) -> Optional[int]:
    """Returns the byte length of a body, when it can be known without reading it.

    Args:
        value: The body.

    Returns:
        The length for bytes/str and seekable files, or ``None`` for a stream
        whose size is unknown -- which is what makes the upload chunked.

    Example:
        >>> body_length("abc"), body_length(iter([b"a"]))
        (3, None)
    """
    ...
