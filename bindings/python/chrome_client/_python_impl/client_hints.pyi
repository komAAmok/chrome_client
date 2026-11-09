"""Type surface of :mod:`chrome_client._python_impl.client_hints`.

Generated from the h1 captures by ``tools/generate-client-hints.py``; do not
edit by hand. Each entry is the exact ordered header list that release sent,
minus ``Host`` and ``Connection`` (transport level, never extra headers).

Consumed by ``browser_headers``, which rewrites the platform-dependent values.
"""

from typing import Dict, Tuple

#: Ordered header list per pinned profile major, as captured.
CAPTURED_HEADERS: Dict[int, Tuple[Tuple[str, str], ...]]
