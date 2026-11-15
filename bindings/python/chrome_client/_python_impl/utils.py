"""Helpers mirroring ``requests.utils``.

Only the functions that make sense on top of a Chromium Core are real; ones
whose behaviour would be a lie (urllib3 pool internals) are absent rather than
stubbed.
"""

import codecs
import os
import re
import socket
from netrc import NetrcParseError, netrc
from urllib.parse import quote, unquote, urlparse, urlunparse

try:
    from collections.abc import Mapping
except ImportError:  # Python 3.6
    from collections import Mapping

from .exceptions import InvalidHeader, InvalidURL
from .structures import CaseInsensitiveDict

DEFAULT_ACCEPT_ENCODING = "gzip, deflate, br, zstd"
UNRESERVED_SET = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
NETRC_FILES = (".netrc", "_netrc")


def default_headers():
    """Headers this module seeds: none.

    requests seeds ``User-Agent``, ``Accept``, ``Accept-Encoding`` and
    ``Connection`` here, unconditionally.  This module does not, because the
    header set is not a constant: it belongs to the profile, and it lives in
    :mod:`.browser_headers`, applied by
    :meth:`.sessions.BaseSession._browser_headers`.  A session with no profile
    still gets nothing from here -- Chromium's ``net/`` layer supplies ``Host``,
    ``Connection``, ``User-Agent`` and ``Accept-Encoding`` on its own, and that
    is the honest default for a client that is not claiming to be a browser.

    Returns:
        An empty mapping, so ``Session(headers=...)`` starts from nothing.
    """
    return CaseInsensitiveDict()


def to_key_val_list(value):
    if value is None:
        return None
    if isinstance(value, (str, bytes, bool, int)):
        raise ValueError("cannot encode objects that are not 2-tuples")
    if isinstance(value, Mapping):
        return list(value.items())
    return list(value)


def super_len(obj):
    for attribute in ("__len__", "len", "fileno", "tell"):
        if attribute == "__len__" and hasattr(obj, "__len__"):
            return len(obj)
        if attribute == "len" and hasattr(obj, "len"):
            return obj.len
        if attribute == "fileno" and hasattr(obj, "fileno"):
            try:
                return os.fstat(obj.fileno()).st_size
            except (OSError, TypeError):
                continue
        if attribute == "tell" and hasattr(obj, "tell") and hasattr(obj, "seek"):
            try:
                current = obj.tell()
                obj.seek(0, 2)
                total = obj.tell()
                obj.seek(current, 0)
                return total - current
            except (OSError, ValueError):
                continue
    return 0


def unquote_unreserved(uri):
    parts = uri.split("%")
    for index in range(1, len(parts)):
        chunk = parts[index][0:2]
        # Only an alphanumeric two-character escape can be a hex pair; anything
        # else ("%-1", "%%") is not an escape at all and stays literal. Testing
        # just the length let "%-1" through to int("-1", 16) == -1 and then
        # chr(-1), which raised a bare ValueError that no caller of get() can
        # catch: measured, session.get("http://host/%-1") leaked
        # "chr() arg not in range(0x110000)" instead of raising InvalidURL.
        if len(chunk) == 2 and chunk.isalnum():
            try:
                codepoint = int(chunk, 16)
            except ValueError:
                raise InvalidURL("Invalid percent-escape sequence: '%s'" % chunk)
            character = chr(codepoint)
            if character in UNRESERVED_SET:
                parts[index] = character + parts[index][2:]
            else:
                parts[index] = "%" + parts[index]
        else:
            parts[index] = "%" + parts[index]
    return "".join(parts)


def requote_uri(uri):
    safe_with_percent = "!#$%&'()*+,/:;=?@[]~"
    safe_without_percent = "!#$&'()*+,/:;=?@[]~"
    try:
        return quote(unquote_unreserved(uri), safe=safe_with_percent)
    except InvalidURL:
        return quote(uri, safe=safe_without_percent)


def get_encoding_from_headers(headers):
    content_type = headers.get("content-type")
    if not content_type:
        return None
    content_type, params = _parse_content_type_header(content_type)
    if "charset" in params:
        return params["charset"].strip("'\"")
    if "text" in content_type:
        # RFC 2616 section 3.7.1's default for text/* with no charset, and what
        # requests answers. Returning None instead sent ``.text`` and
        # ``iter_content(decode_unicode=True)`` down different fallbacks, so one
        # body decoded two ways (measured: U+FFFD where the accents were).
        return "ISO-8859-1"
    if "application/json" in content_type:
        return "utf-8"
    return None


def _parse_content_type_header(header):
    tokens = header.split(";")
    content_type = tokens[0].strip()
    params = {}
    for token in tokens[1:]:
        if "=" in token:
            name, value = token.split("=", 1)
            params[name.strip().lower()] = value.strip()
        elif token.strip():
            params[token.strip().lower()] = True
    return content_type, params


_CHARSET_RE = re.compile(r'<meta.*?charset=["\']*(.+?)["\'>]', flags=re.I)
_PRAGMA_RE = re.compile(r'<meta.*?content=["\']*;?charset=(.+?)["\'>]', flags=re.I)
_XML_RE = re.compile(r'^<\?xml.*?encoding=["\']*(.+?)["\'>]')


def get_encodings_from_content(content):
    if isinstance(content, bytes):
        content = content.decode("ascii", "replace")
    return (_CHARSET_RE.findall(content)
            + _PRAGMA_RE.findall(content)
            + _XML_RE.findall(content))


def guess_json_utf(data):
    sample = data[:4]
    if sample in (codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE):
        return "utf-32"
    if sample[:3] == codecs.BOM_UTF8:
        return "utf-8-sig"
    if sample[:2] in (codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE):
        return "utf-16"
    null_count = sample.count(b"\x00")
    if null_count == 0:
        return "utf-8"
    if null_count == 2:
        # Only the two shapes that really are UTF-16 ASCII characters identify
        # anything; claiming one for the rest decoded garbage as text.
        if sample[::2] == b"\x00\x00":
            return "utf-16-be"
        if sample[1::2] == b"\x00\x00":
            return "utf-16-le"
        return None
    if null_count == 3:
        if sample[:3] == b"\x00\x00\x00":
            return "utf-32-be"
        if sample[1:] == b"\x00\x00\x00":
            return "utf-32-le"
        return None
    return None


def parse_header_links(value):
    links = []
    replace_chars = " '\""
    value = value.strip(replace_chars)
    if not value:
        return links
    for entry in re.split(r', *<', value):
        try:
            url, params = entry.split(";", 1)
        except ValueError:
            url, params = entry, ""
        link = {"url": url.strip("<> '\"")}
        for param in params.split(";"):
            try:
                key, item = param.split("=")
            except ValueError:
                break
            link[key.strip(replace_chars)] = item.strip(replace_chars)
        links.append(link)
    return links


def guess_filename(obj):
    name = getattr(obj, "name", None)
    if name and isinstance(name, (str, bytes)) and name[0] != "<" and name[-1] != ">":
        return os.path.basename(name)
    return None


def get_auth_from_url(url):
    parsed = urlparse(url)
    try:
        return unquote(parsed.username or ""), unquote(parsed.password or "")
    except (AttributeError, TypeError):
        return "", ""


def urldefragauth(url):
    """Strips credentials and the fragment, as requests does before logging."""
    scheme, netloc, path, params, query, _fragment = _urlparse6(url)
    if not netloc:
        netloc, path = path, netloc
    netloc = netloc.rsplit("@", 1)[-1]
    return urlunparse((scheme, netloc, path, params, query, ""))


def _urlparse6(url):
    parsed = urlparse(url)
    return (parsed.scheme, parsed.netloc, parsed.path,
            getattr(parsed, "params", ""), parsed.query, parsed.fragment)


def prepend_scheme_if_needed(url, new_scheme):
    """Adds *new_scheme* to a URL that has none, keeping one it already has.

    ``urlparse`` reads a bare ``host:port/...`` as a scheme named after the host.
    Swapping netloc and path on that reading produced ``example.com://8080/x``,
    which no client can fetch; a scheme is never followed by a port number.

    Args:
        url: The URL, which may or may not carry a scheme.
        new_scheme: The scheme to add when there is none.

    Returns:
        The absolute URL.

    Example:
        >>> prepend_scheme_if_needed("example.com:8080/x", "http")
        'http://example.com:8080/x'
    """
    parsed = urlparse(url, scheme=new_scheme)
    if not parsed.netloc:
        _head, colon, tail = url.partition(":")
        if colon and tail[:1].isdigit():
            parsed = urlparse("%s://%s" % (new_scheme, url))
        else:
            parsed = parsed._replace(netloc=parsed.path, path="")
    return parsed.geturl()


def is_ipv4_address(value):
    """Reports whether the address parser accepts the value as IPv4.

    The dotted-quad test this used to add is not requests' rule and it changes
    the NO_PROXY answer: ``inet_aton`` also takes the short forms ``1.2.3`` and
    ``127.1``, which resolve as addresses, so both branches must agree on them.

    Args:
        value: The candidate string.

    Returns:
        ``True`` when the address parser accepts it.
    """
    try:
        socket.inet_aton(value)
    except OSError:
        return False
    return True


def address_in_network(address, network):
    try:
        ip_int = int.from_bytes(socket.inet_aton(address), "big")
        network_address, bits = network.split("/")
        netmask = (0xFFFFFFFF << (32 - int(bits))) & 0xFFFFFFFF
        network_int = int.from_bytes(socket.inet_aton(network_address), "big")
    except (OSError, ValueError):
        return False
    return (ip_int & netmask) == (network_int & netmask)


def should_bypass_proxies(url, no_proxy=None):
    """Applies ``NO_PROXY`` semantics, including CIDR and suffix matching.

    Two spellings both used to fail OPEN, routing through the proxy the caller
    had excluded: ``*`` (never proxy) matched no suffix, and a leading dot
    (``.example.com``) stayed in the comparison so the bare host never matched.

    Args:
        url: The target URL.
        no_proxy: The NO_PROXY value, or ``None`` to read the environment.

    Returns:
        ``True`` when no proxy may be used for *url*.
    """
    if no_proxy is None:
        no_proxy = os.environ.get("no_proxy") or os.environ.get("NO_PROXY")
    parsed = urlparse(url)
    if parsed.hostname is None:
        return True
    if no_proxy:
        # The wildcard answers before any matching: it excludes everything.
        if no_proxy.strip() == "*":
            return True
        entries = (host for host in no_proxy.replace(" ", "").split(",") if host)
        # A leading dot is a suffix marker. A bare "." keeps its dots rather than
        # becoming an empty entry that would match every host.
        entries = (entry.lstrip(".") if entry.strip(".") else entry
                   for entry in entries)
        if is_ipv4_address(parsed.hostname):
            for entry in entries:
                if "/" in entry:
                    if address_in_network(parsed.hostname, entry):
                        return True
                elif parsed.hostname == entry:
                    return True
        else:
            host_with_port = parsed.hostname
            if parsed.port:
                host_with_port = "%s:%d" % (parsed.hostname, parsed.port)
            for entry in entries:
                if parsed.hostname.endswith(entry) or host_with_port.endswith(entry):
                    return True
    return bool(proxy_bypass(parsed.hostname))


def proxy_bypass(host):
    """Platform proxy-bypass hook; the Core does not read platform settings."""
    return False


def get_environ_proxies(url, no_proxy=None):
    if should_bypass_proxies(url, no_proxy=no_proxy):
        return {}
    proxies = {}
    for name, value in os.environ.items():
        name = name.lower()
        if name.endswith("_proxy") and value:
            proxies[name[:-6]] = value
    return proxies


def environ_proxy_map():
    """Every ``*_proxy`` environment variable, keyed by scheme.

    One scan serves both callers. The guard and the lookup used to be separate
    functions that each walked the environment, so a request with a proxy set
    paid for two scans of the same four-to-forty variables. Returning ``None``
    rather than ``{}`` keeps "no proxy configured" distinguishable from "a proxy
    variable is set to the empty string", which the old guard already treated as
    absent.

    Keys are the scheme (``http``, ``https``, ``all``); a name like
    ``HTTP_PROXY`` becomes ``http`` because the suffix is stripped after
    lower-casing, exactly as ``requests`` does it.

    Returns:
        ``{scheme: url}``, or ``None`` when no proxy variable carries a value.

    Example:
        >>> environ_proxy_map()      # doctest: +SKIP
        {'http': 'http://127.0.0.1:8080'}
    """
    found = None
    for name in os.environ:
        # Keys-only iteration: os.environ.items() materialises every pair, which
        # measured 2.1 us against 1.1 us for the keys alone.
        lowered = name.lower()
        if not lowered.endswith("_proxy"):
            continue
        value = os.environ.get(name)
        if not value:
            continue
        if found is None:
            found = {}
        found[lowered[:-6]] = value
    return found



def resolve_proxies(request, proxies, trust_env=True):
    """Merges caller proxies with the environment, honouring ``NO_PROXY``."""
    proxies = dict(proxies) if proxies is not None else {}
    if not trust_env:
        return proxies
    environment = environ_proxy_map()
    if environment is None:
        return proxies
    url = getattr(request, "url", request)
    scheme = urlparse(url).scheme
    no_proxy = proxies.get("no_proxy")
    if not should_bypass_proxies(url, no_proxy=no_proxy):
        # Not delegating to get_environ_proxies: that helper starts with the
        # identical should_bypass_proxies call this branch just answered, so
        # using it ran the NO_PROXY match twice per request. Measured 2 calls of
        # that function where 1 suffices. get_environ_proxies keeps its own check
        # because it is public and may be called on its own.
        proxy = environment.get(scheme, environment.get("all"))
        if proxy:
            proxies.setdefault(scheme, proxy)
    return proxies


#: Sentinel for "nothing cached yet". ``None`` is a real value in the cache below,
#: so "empty" and "not computed" need different markers.
_UNSET = object()

#: Expanded ``.netrc`` candidate paths, keyed by what ``expanduser`` depends on.
#:
#: ``expanduser`` is the expensive half: with ``HOME`` unset -- the normal case in
#: a container or a CI job -- it falls back to ``pwd.getpwuid``, which costs
#: 0.11 ms per call, twice per lookup. Measured on a 2.08 ms request that was
#: 0.15 ms per request, i.e. 7% of the whole request and 0.92 ms per coroutine of
#: the async synchronous prefix.
#:
#: Only the EXPANSION is cached, never the existence check. A ``stat`` costs about
#: 0.005 ms and is the part that can change while the process runs, so caching it
#: would mean a ``.netrc`` created or deleted mid-run was never noticed -- which
#: the tests in test_netrc_cache.py check for.
_netrc_candidates_cache = {"key": _UNSET, "paths": None}


def _netrc_candidates():
    """The expanded ``.netrc`` candidate paths, in lookup order.

    Cached because ``expanduser`` consults the password database on every call
    when ``HOME`` is unset. The key is everything ``expanduser`` reads: the
    ``NETRC`` value and ``HOME``.

    Returns:
        A tuple of absolute paths, possibly empty.
    """
    netrc_file = os.environ.get("NETRC")
    key = (netrc_file, os.environ.get("HOME"))
    cached = _netrc_candidates_cache
    if cached["key"] == key:
        return cached["paths"]
    if netrc_file:
        paths = (os.path.expanduser(netrc_file),)
    else:
        paths = tuple(os.path.expanduser(os.path.join("~", name))
                      for name in NETRC_FILES)
    cached["key"] = key
    cached["paths"] = paths
    return paths


def _netrc_path():
    """Resolves the ``.netrc`` path, or ``None`` when there is no file.

    Returns:
        The first existing candidate, or ``None``.
    """
    for path in _netrc_candidates():
        if os.path.exists(path):
            return path
    return None

def get_netrc_auth(url, raise_errors=False):
    """Returns ``(login, password)`` from ``.netrc`` for *url*, or ``None``."""
    path = _netrc_path()
    if path is None:
        return None
    try:
        parsed = urlparse(url)
        if parsed.hostname is None:
            return None
        entry = netrc(path).authenticators(parsed.hostname)
        if entry:
            login_index = 0 if entry[0] else 1
            return entry[login_index], entry[2]
    except (NetrcParseError, OSError):
        if raise_errors:
            raise
    return None


def check_header_validity(header):
    """Rejects header values that would let a caller inject a second header."""
    name, value = header
    for part, kind in ((name, "name"), (value, "value")):
        if part is None:
            raise InvalidHeader("Header %s must not be None" % kind)
        text = part.decode("ascii", "replace") if isinstance(part, bytes) else str(part)
        if "\n" in text or "\r" in text or "\x00" in text:
            raise InvalidHeader(
                "Invalid leading whitespace, reserved character(s), or return "
                "character(s) in header %s: %r" % (kind, part))
    return None


def default_user_agent(name="chrome_client"):
    """Kept for source compatibility; the profile owns the real User-Agent."""
    return name
