"""Type surface of :mod:`chrome_client._python_impl.browser_headers`.

Builds the ordered header list one pinned profile would send, from the captures
in :mod:`chrome_client._python_impl.client_hints`. ``User-Agent`` and
``sec-ch-ua-platform`` are stored as captured (a Windows desktop) and rewritten
to the host platform at call time, because a client may not claim a platform the
socket is not on.
"""

from typing import List, Optional, Sequence, Tuple

#: Headers the host platform rewrites, rather than the capture.
PLATFORM_HEADERS: Tuple[str, ...]

#: First Chrome major whose captures send ``zstd`` in ``Accept-Encoding``.
ZSTD_FIRST_MAJOR: int


def _platform_token() -> str:
    """The parenthesised platform group a host-native UA carries."""
    ...


def _sec_ch_ua_platform() -> str:
    """The quoted ``sec-ch-ua-platform`` value for this host."""
    ...


def profile_major(profile: Optional[str]) -> Optional[int]:
    """Parses the major version out of a canonical profile name.

    Args:
        profile: A ``chrome_<major>`` name, already normalised.

    Returns:
        The major as an int, or ``None`` when the name is not one of the pinned
        profiles.
    """
    ...


def browser_headers(profile: Optional[str],
                    accept_language: Optional[str] = None) -> List[Tuple[str, str]]:
    """Returns the ordered headers Chrome of *profile* would send.

    Args:
        profile: A ``chrome_<major>`` name, or ``None`` for no browser headers.
        accept_language: Overrides the captured ``Accept-Language``.

    Returns:
        A list of ``(name, value)`` pairs in capture order, or an empty list when
        *profile* is not a pinned profile.
    """
    ...


def header_order(profile: Optional[str]) -> Sequence[str]:
    """Returns the order the profile browser emits its headers in.

    Args:
        profile: A ``chrome_<major>`` name, or ``None``.

    Returns:
        The header names in capture order, or an empty sequence when *profile* is
        not a pinned profile.
    """
    ...
