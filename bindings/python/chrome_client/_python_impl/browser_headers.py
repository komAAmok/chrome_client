"""The request header set Chrome sends, as a function of the profile.

``impersonate=`` used to change only the ``User-Agent`` version string, so a
``chrome_155`` session announced itself as Chrome 155 and then sent four headers
where real Chrome sends twelve.  Any detector comparing ``sec-ch-ua`` against
``User-Agent`` caught that immediately, and it was the largest remaining
fingerprint gap (``docs/NEXT_STEPS.md``).

This module closes it from the *evidence*: ``client_hints.CAPTURED_HEADERS`` is
generated from the six h1 captures each profile already carries, and this file
turns one entry into the ordered header list a request should carry.

Two deliberate deviations from the captures, both of which the evidence cannot
settle on its own:

* **The platform follows the host, not the capture.**  The captures came from a
  Windows desktop; a client must not claim a platform the socket is not on.  The
  Core already derives ``User-Agent`` from ``BUILDFLAG(IS_WIN/IS_MAC)`` and
  ``ARCH_CPU_*`` (``core/source/minicronet.cc``), so ``sec-ch-ua-platform`` is
  rewritten to match what the Core will actually send.  A ``"Windows"`` hint next
  to an ``X11; Linux`` User-Agent is a louder contradiction than a missing hint.
* **``Sec-Fetch-*`` describe a top-level navigation.**  Every capture is a
  navigation, because that is what a browser does when a person opens a URL, and
  ``session.get(url)`` is the same act.  A subresource fetch wants
  ``Sec-Fetch-Site: same-origin`` and no ``Sec-Fetch-User``; a caller doing that
  passes its own headers, which override these field by field.

Nothing here runs when ``impersonate`` is ``None``: a session with no profile is
not claiming to be a browser, and injecting browser headers into it would make
every non-impersonating caller look like Chrome.
"""

import platform as _platform
import sys as _sys

from .client_hints import CAPTURED_HEADERS

#: Headers the facade fills in from the host rather than from the capture.
PLATFORM_HEADERS = ("User-Agent", "sec-ch-ua-platform")

#: Chrome major whose ``Accept-Encoding`` gained ``zstd``.  Measured across the
#: 72 captured profiles: 86..122 send "gzip, deflate, br", 123..157 add zstd.
#: Recorded so a reader can see the boundary is evidence rather than a guess;
#: the value itself still comes from the table.
ZSTD_FIRST_MAJOR = 123


def _platform_token():
    """Returns the platform token the Core puts inside the User-Agent group.

    Must match ``core/source/minicronet.cc`` exactly: the two strings are the
    same claim, one on each side of the ABI, and a mismatch is exactly the
    contradiction this module exists to avoid.

    Returns:
        The parenthesised-group text without the parentheses, e.g.
        ``"X11; Linux x86_64"``.
    """
    if _sys.platform.startswith("win"):
        return "Windows NT 10.0; Win64; x64"
    if _sys.platform == "darwin":
        return "Macintosh; Intel Mac OS X 10_15_7"
    machine = _platform.machine().lower()
    if machine in ("aarch64", "arm64"):
        return "X11; Linux aarch64"
    if machine in ("i386", "i486", "i586", "i686", "x86"):
        return "X11; Linux i686"
    return "X11; Linux x86_64"


def _sec_ch_ua_platform():
    """Returns the ``sec-ch-ua-platform`` value for this host.

    Chrome sends a quoted, case-sensitive platform name from a fixed set.  Linux
    and Windows are what the Core can run on; macOS is mapped from ``darwin``.

    Returns:
        e.g. ``"Linux"`` with the quotes Chrome writes, as a string.
    """
    if _sys.platform.startswith("win"):
        return chr(34) + "Windows" + chr(34)
    if _sys.platform == "darwin":
        return chr(34) + "macOS" + chr(34)
    return chr(34) + "Linux" + chr(34)


def profile_major(profile):
    """Parses the major version out of a canonical profile name.

    Args:
        profile: A ``chrome_<major>`` name, already normalised.

    Returns:
        The major as an int, or ``None`` when the name is not one of the pinned
        profiles.
    """
    if not profile or not profile.startswith("chrome_"):
        return None
    digits = profile[len("chrome_"):]
    if not digits.isdigit():
        return None
    major = int(digits)
    return major if major in CAPTURED_HEADERS else None


def browser_headers(profile, accept_language=None):
    """Returns the ordered headers Chrome of *profile* would send.

    Args:
        profile: A ``chrome_<major>`` name, or ``None`` for no browser headers.
        accept_language: Overrides the captured ``Accept-Language``.  The Core
            sends its own value from the engine field, so passing the same string
            here keeps the two from disagreeing.

    Returns:
        A list of ``(name, value)`` pairs in capture order, or an empty list when
        *profile* is not a pinned profile.
    """
    major = profile_major(profile)
    if major is None:
        return []
    token = _platform_token()
    platform_hint = _sec_ch_ua_platform()
    pairs = []
    for name, value in CAPTURED_HEADERS[major]:
        if name == "User-Agent":
            # Replace only the parenthesised group: the version string and the
            # rest of the UA come from the profile and must not be rewritten.
            head, _, rest = value.partition("(")
            _group, _, tail = rest.partition(")")
            value = "%s(%s)%s" % (head, token, tail)
        elif name == "sec-ch-ua-platform":
            value = platform_hint
        elif name == "Accept-Language" and accept_language:
            value = accept_language
        pairs.append((name, value))
    return pairs


def header_order(profile):
    """Returns the order the profile browser emits its headers in.

    Args:
        profile: A ``chrome_<major>`` name, or ``None``.

    Returns:
        A list of header names, or an empty list for an unknown profile.
    """
    return [name for name, _value in browser_headers(profile)]
