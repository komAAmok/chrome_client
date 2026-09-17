"""``codes``: status name to number, including upper-case aliases.

At runtime this module *is* ``chrome_client._python_impl.status_codes``: the package aliases it
into ``sys.modules`` on import. The names below are re-exported
explicitly, so a type checker follows the same path instead of
seeing ``Any`` (PEP 561).
"""

from ._python_impl.status_codes import (
    REASONS as REASONS,
    codes as codes,
)
