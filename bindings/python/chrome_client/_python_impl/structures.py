"""Case-insensitive mappings.

``CaseInsensitiveDict`` matches ``requests.structures.CaseInsensitiveDict``:
lookups ignore case, iteration preserves the casing that was last written.
``Headers`` adds the duplicate-preserving surface ``curl_cffi`` exposes, and is
a subclass so ``isinstance(response.headers, CaseInsensitiveDict)`` holds for
requests-shaped code.
"""

from collections import OrderedDict

try:
    from collections.abc import Mapping, MutableMapping
except ImportError:  # Python 3.6 still re-exports these from `collections`
    from collections import Mapping, MutableMapping


def _text(value, encoding="utf-8"):
    # str first: it is what every header name and value is, and the ordering
    # saves one isinstance on that path (measured 62 calls per request).
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return value.decode(encoding, "replace")
    return str(value)


def _key(value):
    return _text(value).lower()


class CaseInsensitiveDict(MutableMapping):
    def __init__(self, data=None, **kwargs):
        self._store = OrderedDict()
        if data is not None:
            self.update(data)
        if kwargs:
            self.update(kwargs)

    def __setitem__(self, key, value):
        self._store[_key(key)] = (_text(key), value)

    def __getitem__(self, key):
        return self._store[_key(key)][1]

    def __delitem__(self, key):
        del self._store[_key(key)]

    def __iter__(self):
        return iter([name for name, _value in self._store.values()])

    def __len__(self):
        return len(self._store)

    def lower_items(self):
        return [(key, value[1]) for key, value in self._store.items()]

    def __eq__(self, other):
        if isinstance(other, Mapping):
            other = CaseInsensitiveDict(other)
        else:
            return NotImplemented
        return dict(self.lower_items()) == dict(other.lower_items())

    def __ne__(self, other):
        result = self.__eq__(other)
        return result if result is NotImplemented else not result

    def copy(self):
        return CaseInsensitiveDict(self._store.values())

    def __repr__(self):
        return "%s(%r)" % (type(self).__name__, dict(self.items()))


class Headers(CaseInsensitiveDict):
    """Header mapping that keeps duplicate fields.

    ``headers["set-cookie"]`` returns the comma-joined value like requests does,
    while ``get_list("set-cookie")`` returns each field separately -- which is
    what cookie parsing needs, since ``Expires`` values contain commas.
    """

    def __init__(self, headers=None, encoding=None):
        self._list = []
        #: Lower-cased names the caller removed, in removal order.
        #: ``requests`` spells deletion as ``headers={"Name": None}``, and the
        #: intent has to survive the merge: a deletion applied only to this
        #: object would let a session-level default of the same name come back
        #: (measured: ``Session(headers={"X-Gone": "yes"}).get(url,
        #: headers={"X-Gone": None})`` still sent ``X-Gone: yes``).
        self._deleted = []
        self.encoding = encoding or "utf-8"
        CaseInsensitiveDict.__init__(self)
        if headers is not None:
            self._ingest(headers)

    def _ingest(self, headers):
        if isinstance(headers, Headers):
            for name, value in headers.multi_items():
                self.add(name, value)
            # A source built from ``{"Name": None}`` carries its removals in a
            # record, not in its items. Copying the items alone revived the field
            # on the way to the wire; the same deletion as a plain dict worked.
            for name in headers.deleted_names:
                self._deleted.append(name)
            return
        if isinstance(headers, Mapping):
            for name, value in headers.items():
                self[name] = value
            return
        for entry in headers:
            if isinstance(entry, (bytes, str)):
                text = _text(entry, self.encoding)
                if ":" not in text:
                    continue
                name, value = text.split(":", 1)
                self.add(name.strip(), value.strip())
            else:
                name, value = entry
                self.add(name, value)

    def _store_add(self, lowered, name, value):
        """Appends one field to the joined value of *lowered* in ``_store``.

        This replaced a full rescan of ``_list`` on every append, which made
        building a header block quadratic in its field count -- and one request
        builds several (the captured profile set, the caller's merge, the policy
        filter). The joined value is the same string the old ", ".join(...)
        produced, because :meth:`add` only ever appends: joining left to right
        is associative, so ``existing + ", " + value`` equals re-joining from
        the list.

        Args:
            lowered: The lower-cased field name.
            name: The field name to report, used only when *lowered* is new.
            value: The field value to append.
        """
        existing = self._store.get(lowered)
        if existing is None:
            self._store[lowered] = (name, value)
        else:
            self._store[lowered] = (existing[0], existing[1] + ", " + value)

    def add(self, key, value):
        """Appends a field without replacing an existing one of the same name.

        A ``None`` value means "remove this header", which is how ``requests``
        spells deletion (``merge_setting`` drops such a key from the merge).
        Storing it as the four characters ``None`` -- what ``_text`` used to do --
        sent ``X-Gone: None`` on the wire, so a caller deleting a session header
        instead invented a new one.

        Args:
            key: The field name.
            value: The field value, or ``None`` to remove the field.
        """
        name = _text(key)
        lowered = name.lower()
        if value is None:
            self._remove(lowered)
            return
        text = _text(value, self.encoding)
        self._list.append((lowered, name, text))
        self._store_add(lowered, name, text)

    def __setitem__(self, key, value):
        """Replaces every field of this name, or removes it when *value* is None.

        A replacement also drops any earlier "the caller removed this" record:
        the object now holds the field, so a later merge must not treat the name
        as deleted. requests spells both cases with one assignment, and leaving
        the record in place made a plain assignment look like a removal.

        Args:
            key: The field name.
            value: The value, or ``None`` to remove the field.
        """
        name = _text(key)
        lowered = name.lower()
        self._deleted = [recorded for recorded in self._deleted
                         if recorded != lowered]
        if value is None:
            self._remove(lowered)
            return
        self._discard(lowered)
        text = _text(value, self.encoding)
        self._list.append((lowered, name, text))
        self._store_add(lowered, name, text)

    def _discard(self, lowered):
        """Drops every field called *lowered*, recording nothing.

        The counterpart of :meth:`_remove` for callers that are replacing a
        value rather than deleting one.

        Args:
            lowered: The lower-cased field name.
        """
        self._list = [entry for entry in self._list if entry[0] != lowered]
        self._store.pop(lowered, None)

    def _remove(self, lowered):
        """Drops every field called *lowered* and remembers the removal.

        Args:
            lowered: The lower-cased field name.
        """
        self._discard(lowered)
        if lowered not in self._deleted:
            self._deleted.append(lowered)

    @property
    def deleted_names(self):
        """The lower-cased field names this object was asked to remove.

        Returns:
            A frozenset, so a caller cannot mutate the record by accident.
        """
        return frozenset(self._deleted)

    def __delitem__(self, key):
        lowered = _key(key)
        if lowered not in self._store:
            raise KeyError(key)
        self._remove(lowered)

    def multi_items(self):
        return [(name, value) for _key, name, value in self._list]

    def get_list(self, key, split_commas=False):
        lowered = _key(key)
        values = [value for entry_key, _name, value in self._list if entry_key == lowered]
        if not split_commas:
            return values
        split = []
        for value in values:
            split.extend(item.strip() for item in value.split(",") if item.strip())
        return split

    getlist = get_list

    @property
    def raw(self):
        return [(name.encode(self.encoding), value.encode(self.encoding))
                for _key, name, value in self._list]

    def copy(self):
        """Returns an independent copy, deletions included.

        The constructor replays every surviving field through :meth:`add`, which
        records no deletion, so the record is carried over explicitly. Losing it
        made a caller's deletion vanish when the facade copied the headers on
        its way to the wire (measured: the header was sent anyway).

        Returns:
            A new :class:`Headers`.
        """
        clone = Headers(self, encoding=self.encoding)
        clone._deleted = list(self._deleted)
        return clone

    def update(self, headers=None, **kwargs):
        """Merges another mapping in, replacing field by field.

        Replacing a field is not a deletion. ``del self[name]`` cleared the old
        value and recorded the name in ``deleted_names`` as a side effect, so a
        caller who built headers the ordinary way (defaults, then refine one)
        ended up with a record saying "remove User-Agent" while holding a good
        one. Both readers of that record acted on it:
        ``_reject_core_owned_removals`` refused the request, and
        ``merge_setting`` dropped the session's value (measured: the session's
        ``Accept`` vanished instead of being overridden).

        Args:
            headers: A :class:`Headers`, a mapping, or an iterable of pairs.
            **kwargs: Extra fields, applied the same way as mapping entries.
        """
        if headers is not None:
            if isinstance(headers, Headers):
                # The first occurrence of a name replaces, the rest append, so
                # duplicates survive. Going through __setitem__ is what keeps a
                # replacement from being recorded as a removal.
                seen = set()
                for name, value in headers.multi_items():
                    lowered = _key(name)
                    if lowered in seen:
                        self.add(name, value)
                    else:
                        seen.add(lowered)
                        self[name] = value
                for name in headers.deleted_names:
                    self._remove(name)
            elif isinstance(headers, Mapping):
                for name, value in headers.items():
                    self[name] = value
            else:
                for name, value in headers:
                    self[name] = value
        for name, value in kwargs.items():
            self[name] = value


class LookupDict(dict):
    """Attribute-addressable mapping, matching ``requests.structures``."""

    def __init__(self, name=None):
        self.name = name
        super(LookupDict, self).__init__()

    def __repr__(self):
        return "<lookup '%s'>" % (self.name,)

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name)

    def get(self, key, default=None):
        return self.__dict__.get(key, dict.get(self, key, default))
