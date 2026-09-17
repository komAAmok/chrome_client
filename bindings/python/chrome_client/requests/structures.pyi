"""``CaseInsensitiveDict``, ``Headers`` and ``LookupDict``.

At runtime this module *is* ``chrome_client._python_impl.structures``: the package aliases it
into ``sys.modules`` on import. The names below are re-exported
explicitly, so a type checker follows the same path instead of
seeing ``Any`` (PEP 561).
"""

from .._python_impl.structures import (
    HeaderPairs as HeaderPairs,
    CaseInsensitiveDict as CaseInsensitiveDict,
    Headers as Headers,
    LookupDict as LookupDict,
)
