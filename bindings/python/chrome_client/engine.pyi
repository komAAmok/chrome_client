"""Chromium engine configuration and per-session caching.

At runtime this module *is* ``chrome_client._python_impl.engine``: the package aliases it
into ``sys.modules`` on import. The names below are re-exported
explicitly, so a type checker follows the same path instead of
seeing ``Any`` (PEP 561).
"""

from ._python_impl.engine import (
    DEFAULT_MAX_ENGINES as DEFAULT_MAX_ENGINES,
    EngineConfig as EngineConfig,
    EngineSlot as EngineSlot,
    EngineCache as EngineCache,
    core_version as core_version,
    abi_version as abi_version,
)
