"""Type surface of :mod:`chrome_client.status_codes`.

``codes`` works both as an attribute lookup (``codes.not_found``) and as a
mapping (``codes["not_found"]``), and carries the upper-case spellings too, so
``codes.NOT_FOUND`` is the same ``404``.
"""

from typing import Mapping

from .structures import LookupDict

#: Reason phrases the Core does not report; ``Response.reason`` falls back here
#: because ABI v8 carries only the numeric status.
REASONS: Mapping[int, str]

#: Name or alias to numeric status, e.g. ``codes.ok``, ``codes["im_a_teapot"]``,
#: ``codes.NOT_FOUND``.
#:
#: Example:
#:     >>> from chrome_client import codes
#:     >>> codes.ok, codes.NOT_FOUND, codes.get("too_many_requests")
#:     (200, 404, 429)
codes: LookupDict
