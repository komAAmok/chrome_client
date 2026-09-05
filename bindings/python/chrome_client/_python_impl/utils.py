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
    """Headers the facade adds on its own: none.

    requests seeds ``User-Agent``, ``Accept``, ``Accept-Encoding`` and
    ``Connection`` here.  Doing that would corrupt the very thing this client
    exists for: the impersonation profile and Chromium own the default header
    set, their values, and their order, and an injected ``Accept: */*`` is
    visible to any fingerprinter.  Chromium still emits its own defaults, so the
    request is complete without them.
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
        if len(chunk) == 2:
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
        return "utf-16-be" if sample[::2] == b"\x00\x00" else "utf-16-le"
    if null_count == 3:
        return "utf-32-be" if sample[:3] == b"\x00\x00\x00" else "utf-32-le"
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
    parsed = urlparse(url, scheme=new_scheme)
    if not parsed.netloc:
        parsed = parsed._replace(netloc=parsed.path, path="")
    return parsed.geturl()


def is_ipv4_address(value):
    try:
        socket.inet_aton(value)
    except OSError:
        return False
    return value.count(".") == 3


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
    """Applies ``NO_PROXY`` semantics, including CIDR and suffix matching."""
    if no_proxy is None:
        no_proxy = os.environ.get("no_proxy") or os.environ.get("NO_PROXY")
    parsed = urlparse(url)
    if parsed.hostname is None:
        return True
    if no_proxy:
        entries = (host for host in no_proxy.replace(" ", "").split(",") if host)
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


def resolve_proxies(request, proxies, trust_env=True):
    """Merges caller proxies with the environment, honouring ``NO_PROXY``."""
    proxies = dict(proxies) if proxies is not None else {}
    url = getattr(request, "url", request)
    scheme = urlparse(url).scheme
    no_proxy = proxies.get("no_proxy")
    if trust_env and not should_bypass_proxies(url, no_proxy=no_proxy):
        environment = get_environ_proxies(url, no_proxy=no_proxy)
        proxy = environment.get(scheme, environment.get("all"))
        if proxy:
            proxies.setdefault(scheme, proxy)
    return proxies


def get_netrc_auth(url, raise_errors=False):
    """Returns ``(login, password)`` from ``.netrc`` for *url*, or ``None``."""
    netrc_file = os.environ.get("NETRC")
    candidates = (netrc_file,) if netrc_file else \
        tuple(os.path.join("~", name) for name in NETRC_FILES)
    try:
        for candidate in candidates:
            path = os.path.expanduser(candidate)
            if os.path.exists(path):
                break
        else:
            return None
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
