"""Authentication handlers, matching ``requests.auth``."""

import base64
import hashlib
import os
import re
import time
from urllib.parse import urlparse

from .exceptions import UnsupportedFeature


def _basic_auth_str(username, password):
    if isinstance(username, str):
        username = username.encode("latin1")
    if isinstance(password, str):
        password = password.encode("latin1")
    token = base64.b64encode(b":".join((username, password))).strip()
    return "Basic " + token.decode("ascii")


class AuthBase(object):
    """Base class for authentication handlers."""

    def __call__(self, request):
        raise NotImplementedError("Auth hooks must be callable")


class HTTPBasicAuth(AuthBase):
    def __init__(self, username, password):
        self.username = username
        self.password = password

    def __eq__(self, other):
        return (self.username == getattr(other, "username", None)
                and self.password == getattr(other, "password", None))

    def __ne__(self, other):
        return not self == other

    def __call__(self, request):
        request.headers["Authorization"] = _basic_auth_str(self.username, self.password)
        return request


class HTTPProxyAuth(HTTPBasicAuth):
    def __call__(self, request):
        request.headers["Proxy-Authorization"] = _basic_auth_str(self.username,
                                                                 self.password)
        return request


class HTTPDigestAuth(AuthBase):
    """Digest auth over a single challenge round trip.

    Unlike requests this cannot hook the 401 response transparently, because the
    Core follows and completes the request itself.  Use
    ``handle_401(response, session)`` from a ``response`` hook, or pass the
    challenge in explicitly.
    """

    def __init__(self, username, password):
        self.username = username
        self.password = password
        self.chal = {}
        self.nonce_count = 0
        self.last_nonce = ""

    def parse_challenge(self, header):
        if not header or not header.lower().startswith("digest "):
            return {}
        pattern = re.compile(r'(\w+)=(?:"([^"]*)"|([^,]*))')
        self.chal = {name: (quoted if quoted else bare).strip()
                     for name, quoted, bare in pattern.findall(header[7:])}
        return self.chal

    def build_header(self, method, url):
        chal = self.chal
        if not chal:
            raise UnsupportedFeature(
                "HTTPDigestAuth needs the WWW-Authenticate challenge; call "
                "parse_challenge() with the 401 response header first")
        realm = chal.get("realm", "")
        nonce = chal.get("nonce", "")
        qop = chal.get("qop")
        algorithm = (chal.get("algorithm") or "MD5").upper()
        opaque = chal.get("opaque")
        digest_mod = {"MD5": hashlib.md5, "MD5-SESS": hashlib.md5,
                      "SHA": hashlib.sha1, "SHA-256": hashlib.sha256,
                      "SHA-256-SESS": hashlib.sha256,
                      "SHA-512": hashlib.sha512}.get(algorithm)
        if digest_mod is None:
            raise UnsupportedFeature("unsupported digest algorithm %r" % (algorithm,))

        def hash_utf8(value):
            return digest_mod(value.encode("utf-8")).hexdigest()

        path = urlparse(url).path or "/"
        query = urlparse(url).query
        if query:
            path = path + "?" + query
        a1 = "%s:%s:%s" % (self.username, realm, self.password)
        a2 = "%s:%s" % (method.upper(), path)
        ha1 = hash_utf8(a1)
        ha2 = hash_utf8(a2)
        if nonce == self.last_nonce:
            self.nonce_count += 1
        else:
            self.nonce_count = 1
        self.last_nonce = nonce
        ncvalue = "%08x" % self.nonce_count
        cnonce = hashlib.sha1(
            ("%s:%s:%s:%s" % (self.nonce_count, nonce, time.ctime(),
                              os.urandom(8))).encode("utf-8")).hexdigest()[:16]
        if algorithm.endswith("-SESS"):
            ha1 = hash_utf8("%s:%s:%s" % (ha1, nonce, cnonce))
        # ``qop`` is a comma-separated list of what the server accepts, and
        # only "auth" can be computed here: "auth-int" hashes the request body,
        # which this layer does not see. Testing ``qop is None`` treated every
        # non-empty value as "auth", so a server offering only ``qop="auth-int"``
        # received a header labelled qop="auth" and rejected it. Measured:
        # qop="auth-int" produced qop="auth" in the emitted header.
        offered = [item.strip() for item in (qop or "").split(",") if item.strip()]
        if not offered:
            response = hash_utf8("%s:%s:%s" % (ha1, nonce, ha2))
            use_qop = None
        elif "auth" in offered:
            response = hash_utf8("%s:%s:%s:%s:%s" % (ha1, nonce, ncvalue, cnonce, ha2))
            use_qop = "auth"
        else:
            raise UnsupportedFeature(
                "the server offers qop=%s only; this client can compute \"auth\" "
                "but not \"auth-int\" (it would need the request body, which the "
                "Core does not expose to this layer)" % (qop,))
        parts = ['username="%s"' % self.username, 'realm="%s"' % realm,
                 'nonce="%s"' % nonce, 'uri="%s"' % path, 'response="%s"' % response]
        if opaque:
            parts.append('opaque="%s"' % opaque)
        if chal.get("algorithm"):
            parts.append("algorithm=%s" % chal["algorithm"])
        if use_qop:
            parts.append('qop="%s", nc=%s, cnonce="%s"' % (use_qop, ncvalue, cnonce))
        return "Digest " + ", ".join(parts)

    def __call__(self, request):
        if self.chal:
            request.headers["Authorization"] = self.build_header(request.method,
                                                                 request.url)
        return request

    def handle_401(self, response, session, **kwargs):
        """Retries a 401 with the challenge applied; usable as a response hook."""
        if response.status_code != 401:
            return response
        self.parse_challenge(response.headers.get("www-authenticate", ""))
        if not self.chal:
            return response
        retry = response.request.copy()
        retry.headers["Authorization"] = self.build_header(retry.method, retry.url)
        retried = session.send(retry, **kwargs)
        retried.history = list(response.history) + [response]
        return retried


def build_auth(auth):
    """Normalises the ``auth=`` argument to a callable, as requests does."""
    if auth is None or callable(auth):
        return auth
    if isinstance(auth, (tuple, list)) and len(auth) == 2:
        return HTTPBasicAuth(auth[0], auth[1])
    raise TypeError("auth must be None, a 2-tuple, or a callable")
