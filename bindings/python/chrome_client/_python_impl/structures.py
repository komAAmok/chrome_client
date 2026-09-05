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
    if isinstance(value, bytes):
        return value.decode(encoding, "replace")
    return value if isinstance(value, str) else str(value)


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
        self.encoding = encoding or "utf-8"
        CaseInsensitiveDict.__init__(self)
        if headers is not None:
            self._ingest(headers)

    def _ingest(self, headers):
        if isinstance(headers, Headers):
            for name, value in headers.multi_items():
                self.add(name, value)
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

    def _refresh(self, lowered):
        values = [value for key, _name, value in self._list if key == lowered]
        if not values:
            self._store.pop(lowered, None)
            return
        name = next(name for key, name, _value in self._list if key == lowered)
        self._store[lowered] = (name, ", ".join(values))

    def add(self, key, value):
        """Appends a field without replacing an existing one of the same name."""
        lowered = _key(key)
        self._list.append((lowered, _text(key), _text(value, self.encoding)))
        self._refresh(lowered)

    def __setitem__(self, key, value):
        lowered = _key(key)
        self._list = [entry for entry in self._list if entry[0] != lowered]
        self._list.append((lowered, _text(key), _text(value, self.encoding)))
        self._refresh(lowered)

    def __delitem__(self, key):
        lowered = _key(key)
        if lowered not in self._store:
            raise KeyError(key)
        self._list = [entry for entry in self._list if entry[0] != lowered]
        self._store.pop(lowered, None)

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
        return Headers(self, encoding=self.encoding)

    def update(self, headers=None, **kwargs):
        if headers is not None:
            if isinstance(headers, Headers):
                for name in list(headers.keys()):
                    if _key(name) in self._store:
                        del self[name]
                for name, value in headers.multi_items():
                    self.add(name, value)
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
