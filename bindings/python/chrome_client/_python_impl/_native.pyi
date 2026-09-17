"""Type surface of :mod:`chrome_client._python_impl._native`.

Native extension discovery.  Python 3.6 loads ``chrome_client_native36`` and 3.7+
loads ``chrome_client_native``; one Python package ships against both, so the
choice is made here and nowhere else.

The native module itself is a compiled extension, so its members are typed as
``Any``: describing them precisely would mean maintaining a second copy of the
Rust signatures.  The public Python surface in the sibling modules is what
carries types.
"""

from typing import Any, Optional

#: The extension module name for the running interpreter.
MODULE_NAME: str

#: The loaded extension module. Import fails loudly when no build is present.
native: Any


def load() -> Any:
    """Imports and returns the native extension.

    Tries ``chrome_client.<MODULE_NAME>`` first (an installed wheel), then a bare
    ``<MODULE_NAME>`` on ``sys.path``, then unpacks a source checkout from
    ``target/release`` and ``bindings/python36/target/release``.

    Returns:
        The extension module.

    Raises:
        ImportError: No extension could be found or loaded. Set
            ``LD_LIBRARY_PATH`` to the sibling Core directory (or
            ``DYLD_LIBRARY_PATH``/``PATH`` on macOS/Windows) and build with
            ``MINICRONET_CORE_DIR`` pointing at ``core/binaries/<target>``.
    """
    ...


def _from_search_path() -> Optional[Any]:
    """Finds an unpackaged extension in a source checkout.

    Returns:
        The extension module, or ``None`` when nothing matched.
    """
    ...
