"""Type surface of :mod:`chrome_client.auth`.

Matches ``requests.auth``.  The plain and proxy handlers are complete; digest is
partial by nature, because the Core completes the request itself and cannot hand
a 401 back to the facade mid-flight.
"""

from typing import Any, Optional, Tuple

from typing_extensions import TypeAlias

from ._types import AuthLike, StrOrBytes
from .models import PreparedRequest

#: ``(username, password)``, as ``build_auth`` accepts it.
AuthCredentials: TypeAlias = Tuple[StrOrBytes, StrOrBytes]


class AuthBase:
    """Base class for authentication handlers.

    Subclass it and implement ``__call__``: it receives the
    :class:`~chrome_client.PreparedRequest` and returns it after adding headers.

    Example:
        >>> class TokenAuth(AuthBase):
        ...     def __init__(self, token):
        ...         self.token = token
        ...     def __call__(self, request):
        ...         request.headers["Authorization"] = "Bearer " + self.token
        ...         return request
    """

    def __call__(self, request: PreparedRequest) -> PreparedRequest:
        """Applies this handler to a prepared request.

        Args:
            request: The request to modify.

        Returns:
            The same request, with credentials applied.

        Raises:
            NotImplementedError: Always, on the base class.
        """
        ...


class HTTPBasicAuth(AuthBase):
    """HTTP Basic authentication.

    Attributes:
        username: The user name.
        password: The password.

    Example:
        >>> session.get(url, auth=HTTPBasicAuth("user", "pass"))
        >>> session.get(url, auth=("user", "pass"))   # same thing
    """

    username: StrOrBytes
    password: StrOrBytes

    def __init__(self, username: StrOrBytes, password: StrOrBytes) -> None:
        """Stores the credentials.

        Args:
            username: User name; encoded as latin-1 when it is a ``str``.
            password: Password; encoded as latin-1 when it is a ``str``.
        """
        ...

    def __eq__(self, other: object) -> bool:
        """Compares the credentials with another handler's.

        Args:
            other: The other object; anything without ``username``/``password``
                compares unequal.

        Returns:
            ``True`` when both fields match."""
        ...
    def __ne__(self, other: object) -> bool:
        """Negation of :meth:`__eq__`.

        Args:
            other: The other object.

        Returns:
            ``True`` when either field differs."""
        ...

    def __call__(self, request: PreparedRequest) -> PreparedRequest:
        """Adds the ``Authorization: Basic ...`` header.

        Args:
            request: The request to modify.

        Returns:
            The same request.
        """
        ...


class HTTPProxyAuth(HTTPBasicAuth):
    """Proxy authentication, as ``requests`` spells it.

    Sets ``Proxy-Authorization`` instead of ``Authorization``.
    """

    def __call__(self, request: PreparedRequest) -> PreparedRequest:
        """Adds the ``Proxy-Authorization: Basic ...`` header.

        Args:
            request: The request to modify.

        Returns:
            The same request.
        """
        ...


class HTTPDigestAuth(AuthBase):
    """Digest auth over a single challenge round trip.

    Unlike requests this cannot hook the 401 transparently, because the Core
    follows and completes the request itself.  Drive it explicitly::

        auth = HTTPDigestAuth("user", "pass")
        response = session.get(url, auth=auth)
        if response.status_code == 401:
            auth.parse_challenge(response.headers["WWW-Authenticate"])
            response = session.send(auth.handle_401(response, session))

    Or register it as a response hook, which does the same thing automatically
    for the single round trip the Core allows.

    Attributes:
        username: The user name.
        password: The password.
        chal: The parsed challenge parameters, empty until
            :meth:`parse_challenge` runs.
        nonce_count: How many times the current nonce has been used.
        last_nonce: The nonce the last header was built with.
    """

    username: StrOrBytes
    password: StrOrBytes
    chal: Any
    nonce_count: int
    last_nonce: str

    def __init__(self, username: StrOrBytes, password: StrOrBytes) -> None:
        """Stores the credentials.

        Args:
            username: User name.
            password: Password.
        """
        ...

    def parse_challenge(self, header: str) -> Any:
        """Parses a ``WWW-Authenticate: Digest ...`` header.

        Args:
            header: The header value. Anything that does not start with
                ``"digest "`` is ignored.

        Returns:
            The parsed parameters, also stored on :attr:`chal`; ``{}`` when the
            header was absent or of another scheme.

        Example:
            >>> auth.parse_challenge('Digest realm="r", nonce="n", qop="auth"')
            {'realm': 'r', 'nonce': 'n', 'qop': 'auth'}
        """
        ...

    def build_header(self, method: str, url: str) -> str:
        """Builds the ``Authorization: Digest ...`` header value.

        Args:
            method: The HTTP method, which is part of the digest computation.
            url: The request URL; its path and query are digested.

        Returns:
            The complete header value.

        Raises:
            UnsupportedFeature: :meth:`parse_challenge` has not run, or the
                challenge names an algorithm that is not supported (MD5,
                MD5-SESS, SHA, SHA-256, SHA-256-SESS, SHA-512).

        Example:
            >>> auth.parse_challenge(header)
            >>> auth.build_header("GET", "https://example.com/private")[:7]
            'Digest '
        """
        ...

    def __call__(self, request: PreparedRequest) -> PreparedRequest:
        """Adds the ``Authorization`` header once a challenge is known.

        Args:
            request: The request to modify. Left untouched while no challenge
                has been parsed, so the first 401 can be observed.

        Returns:
            The same request.
        """
        ...

    def handle_401(self, response: Any, session: Any, **kwargs: Any) -> Any:
        """Retries a 401 with the challenge applied; usable as a response hook.

        Args:
            response: The 401 response.
            session: The session to resend through.
            **kwargs: Forwarded to ``session.send``.

        Returns:
            The retried response, with the original 401 appended to
            ``.history``; the original response when the status was not 401 or
            the challenge could not be parsed.

        Example:
            >>> session.get(url, auth=HTTPDigestAuth("u", "p"),
            ...             hooks={"response": auth.handle_401})
        """
        ...


def build_auth(auth: AuthLike) -> Optional[Any]:
    """Normalises the ``auth=`` argument to a callable, as requests does.

    Args:
        auth: ``None``, a callable, an :class:`AuthBase`, or a
            ``(username, password)`` tuple or 2-element list.

    Returns:
        A callable (the tuple becomes an :class:`HTTPBasicAuth`), or ``None``.

    Raises:
        TypeError: ``auth`` is neither callable nor a 2-element sequence.

    Example:
        >>> build_auth(("user", "pass")).username
        'user'
    """
    ...
