"""Type surface of :mod:`chrome_client.structures`.

``CaseInsensitiveDict`` matches ``requests.structures.CaseInsensitiveDict``:
lookups ignore case, iteration preserves the casing that was last written.
``Headers`` adds the duplicate-preserving surface ``curl_cffi`` exposes, and is a
subclass so ``isinstance(response.headers, CaseInsensitiveDict)`` holds for
requests-shaped code.
"""

from typing import Any, Iterator, List, Mapping, MutableMapping, Optional, Tuple, Union

from typing_extensions import TypeAlias

from ._types import HeaderItems, HeadersLike

#: ``[(name, value), ...]`` as :meth:`Headers.multi_items` returns it.
HeaderPairs: TypeAlias = List[Tuple[str, str]]


class CaseInsensitiveDict(MutableMapping[str, Any]):
    """A case-insensitive mapping, as ``requests`` defines one.

    Lookups lower-case the key; iteration yields the casing of the last write, so
    ``headers["ACCEPT"] = "text/html"`` then ``list(headers)`` gives
    ``["ACCEPT"]``.

    Example:
        >>> headers = CaseInsensitiveDict({"Accept": "application/json"})
        >>> headers["accept"]
        'application/json'
        >>> headers == {"ACCEPT": "application/json"}
        True
    """

    def __init__(
        self, data: Optional[Union[Mapping[Any, Any], HeaderItems]] = None, **kwargs: Any
    ) -> None:
        """Builds the mapping.

        Args:
            data: A mapping or an iterable of pairs.
            **kwargs: Extra entries, applied after ``data``.
        """
        ...

    def __setitem__(self, key: str, value: Any) -> None:
        """Stores a value, preserving the casing of ``key`` for iteration.

        Args:
            key: The name; matched case-insensitively.
            value: The value."""
        ...
    def __getitem__(self, key: str) -> Any:
        """Looks a value up case-insensitively.

        Args:
            key: The name.

        Returns:
            The stored value.

        Raises:
            KeyError: The name is absent."""
        ...
    def __delitem__(self, key: str) -> None:
        """Removes an entry.

        Args:
            key: The name.

        Raises:
            KeyError: The name is absent."""
        ...
    def __iter__(self) -> Iterator[str]:
        """Iterates the names with the casing of the last write.

        Returns:
            An iterator of header names."""
        ...
    def __len__(self) -> int:
        """Returns how many entries the mapping holds.

        Returns:
            The entry count."""
        ...

    def lower_items(self) -> List[Tuple[str, Any]]:
        """Returns ``(lowercased_name, value)`` pairs.

        Returns:
            The entries with lower-cased keys, used for comparison.

        Example:
            >>> CaseInsensitiveDict({"Accept": "x"}).lower_items()
            [('accept', 'x')]
        """
        ...

    def __eq__(self, other: object) -> bool:
        """Compares two mappings ignoring key case.

        Args:
            other: The other mapping.

        Returns:
            ``True`` when every lower-cased entry matches."""
        ...
    def __ne__(self, other: object) -> bool:
        """Negation of :meth:`__eq__`.

        Args:
            other: The other mapping.

        Returns:
            ``True`` when the mappings differ."""
        ...

    def copy(self) -> "CaseInsensitiveDict":
        """Returns a shallow copy with the same casing.

        Returns:
            A new mapping.
        """
        ...

    def __repr__(self) -> str:
        """Returns ``"CaseInsensitiveDict({...})"``.

        Returns:
            The repr string."""
        ...


class Headers(CaseInsensitiveDict):
    """Header mapping that keeps duplicate fields.

    ``headers["set-cookie"]`` returns the comma-joined value like requests does,
    while :meth:`get_list` returns each field separately -- which is what cookie
    parsing needs, since ``Expires`` values contain commas.

    Attributes:
        encoding: Codec used when a header arrives as text to be split, and when
            :attr:`raw` encodes names and values.

    Example:
        >>> headers = Headers({"Set-Cookie": "a=1"})
        >>> headers.add("Set-Cookie", "b=2; Expires=Wed, 21 Oct 2026 07:28:00 GMT")
        >>> headers.get("set-cookie")            # joined, requests-style
        'a=1, b=2; Expires=Wed, 21 Oct 2026 07:28:00 GMT'
        >>> headers.get_list("set-cookie")       # separate fields
        ['a=1', 'b=2; Expires=Wed, 21 Oct 2026 07:28:00 GMT']
    """

    encoding: str

    def __init__(
        self,
        headers: Optional[Union[HeadersLike, Headers]] = None,
        encoding: Optional[str] = None,
    ) -> None:
        """Builds the mapping.

        Args:
            headers: A mapping, an iterable of pairs, another :class:`Headers`,
                or raw header lines (``"Name: value"``), which are split for you.
            encoding: Codec for bytes input; ``"utf-8"`` by default.
        """
        ...

    def add(self, key: str, value: str) -> None:
        """Appends a field without replacing an existing one of the same name.

        Args:
            key: Header name; its casing is kept for iteration.
            value: Header value.

        Example:
            >>> headers.add("Accept", "application/json")
        """
        ...

    def __setitem__(self, key: str, value: Any) -> None:
        """Replaces every existing field with this name.

        Args:
            key: Header name.
            value: Header value.
        """
        ...

    def __delitem__(self, key: str) -> None:
        """Removes every field with this name.

        Args:
            key: Header name.

        Raises:
            KeyError: The header is absent.
        """
        ...

    def multi_items(self) -> HeaderPairs:
        """Returns every field as its own pair, in insertion order.

        Returns:
            ``[(name, value), ...]`` including duplicates, which is the form the
            Core's request builder takes.

        Example:
            >>> headers.multi_items()
            [('Accept', 'application/json')]
        """
        ...

    def get_list(self, key: str, split_commas: bool = False) -> List[str]:
        """Returns every value stored for a field, separately.

        Args:
            key: Header name.
            split_commas: Also split each value on commas -- useful for
                ``Cache-Control``-style fields, wrong for ``Set-Cookie``.

        Returns:
            One string per field, in insertion order.

        Example:
            >>> headers.get_list("set-cookie")
            ['a=1', 'b=2']
        """
        ...

    def getlist(self, key: str, split_commas: bool = False) -> List[str]:
        """Alias of :meth:`get_list`.

        Args:
            key: Header name.
            split_commas: Also split each value on commas.

        Returns:
            One string per field.
        """
        ...

    @property
    def raw(self) -> List[Tuple[bytes, bytes]]:
        """Returns the headers as bytes pairs, for wire-level inspection.

        Returns:
            ``[(name_bytes, value_bytes), ...]`` encoded with :attr:`encoding`.

        Example:
            >>> Headers({"Accept": "application/json"}).raw
            [(b'Accept', b'application/json')]
        """
        ...

    def copy(self) -> "Headers":
        """Returns an independent copy with duplicates and encoding preserved.

        Returns:
            A new :class:`Headers`.
        """
        ...

    def update(  # type: ignore[override]  # requests takes a Headers, a mapping or pairs
        self,
        headers: Optional[Union[HeadersLike, "Headers", Any]] = None,
        **kwargs: Any,
    ) -> None:
        """Merges fields in, replacing same-named ones.

        Args:
            headers: A :class:`Headers` (whose duplicates are preserved), a
                mapping, or an iterable of pairs.
            **kwargs: Extra ``name=value`` fields.

        Example:
            >>> headers.update({"Accept-Encoding": "gzip, deflate, br, zstd"})
        """
        ...


class LookupDict(dict):
    """Attribute-addressable mapping, matching ``requests.structures``.

    Both spellings work, which is what makes ``codes.ok`` and ``codes["ok"]``
    interchangeable: ``requests.status_codes`` is one of these.

    Attributes:
        name: Label used by :meth:`__repr__`.

    Example:
        >>> from chrome_client import codes
        >>> codes.get("not_found"), codes.not_found
        (404, 404)
    """

    name: Optional[str]

    def __init__(self, name: Optional[str] = None) -> None:
        """Creates the mapping.

        Args:
            name: Label shown in the repr.
        """
        ...

    def __repr__(self) -> str:
        """Returns ``"<lookup 'name'>"``.

        Returns:
            The repr string."""
        ...
    def __getattr__(self, name: str) -> Any:
        """Looks an entry up as an attribute.

        Args:
            name: The entry name, e.g. ``not_found``.

        Returns:
            The stored value.

        Raises:
            AttributeError: The entry is absent."""
        ...
    def get(self, key: str, default: Any = None) -> Any:
        """Returns an entry, looking at instance attributes too.

        Args:
            key: Entry name.
            default: Returned when the key is absent.

        Returns:
            The value, or ``default``.
        """
        ...
