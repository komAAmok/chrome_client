"""Transport adapters: the mount point for custom transports.

At runtime this module *is* ``chrome_client._python_impl.adapters``: the package aliases it
into ``sys.modules`` on import. The names below are re-exported
explicitly, so a type checker follows the same path instead of
seeing ``Any`` (PEP 561).
"""

from .._python_impl.adapters import (
    BaseAdapter as BaseAdapter,
    HTTPAdapter as HTTPAdapter,
)
