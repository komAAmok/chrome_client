"""Native extension discovery.

Python 3.6-3.8 loads ``chrome_client_native36`` (pyo3 0.15.1, abi3-py36,
cp36-abi3 wheel) and 3.9+ loads ``chrome_client_native`` (pyo3 0.30.1,
abi3-py39, cp39-abi3 wheel); one Python package ships against both, so the
choice is made here and nowhere else.

The split sits at 3.9 because pyo3 0.30.1 declares 3.9 as its minimum
supported version and offers no abi3-py37/abi3-py38 feature, so its cp39-abi3
wheel refuses to install on 3.7 and 3.8.  Those two versions stay on the
0.15.1 extension, whose cp36-abi3 wheel covers 3.6 through 3.8.
"""

import importlib
import importlib.util
import pathlib
import sys

LEGACY_MODULE_NAME = "chrome_client_native36"
MODERN_MODULE_NAME = "chrome_client_native"

#: The extension this interpreter asks for first.
MODULE_NAME = LEGACY_MODULE_NAME if sys.version_info < (3, 9) else MODERN_MODULE_NAME

#: The extension to try when the first choice is not installed.
#:
#: Both wheels ship the same Python package, and their interpreter ranges as
#: declared are identical (``>=3.6,<3.16``), so pip will happily install either
#: one on any supported interpreter: the difference is only the wheel tag, and
#: pip prefers the more specific tag it can satisfy. On 3.10 that means it
#: *could* pick the cp36-abi3 wheel, which carries ``chrome_client_native36`` --
#: and asking only for ``chrome_client_native`` then fails with "is not
#: installed" on an interpreter the project says it supports. Measured: a
#: cp36-abi3 wheel installed into a 3.12 venv imports fine once the legacy name
#: is accepted, which is exactly what this fallback does.
FALLBACK_MODULE_NAME = (MODERN_MODULE_NAME if MODULE_NAME == LEGACY_MODULE_NAME
                        else LEGACY_MODULE_NAME)


def _from_search_path(failures=None, module_name=None):
    """Finds an unpackaged extension in a source checkout.

    ``parents[4]`` is the repository root: this file sits at
    ``bindings/python/chrome_client/_python_impl/_native.py``.

    ``failures`` is an optional list; every candidate that was found but could
    not be loaded appends its path and reason, so the caller can report why
    instead of only that nothing matched.
    """
    name = module_name or MODULE_NAME
    root = pathlib.Path(__file__).resolve().parents[4]
    directories = list(sys.path) + [
        str(root / "target" / "release"),
        str(root / "target" / "debug"),
    ]
    # The python36 crate builds ``chrome_client_native36``. Its directory is
    # only a candidate when that is the module this interpreter wants: the
    # ``lib<name>*.so`` glob is a prefix match, so on 3.9+ the pattern for
    # ``chrome_client_native`` also matched ``libchrome_client_native36.so``
    # and loading it failed with "dynamic module does not define module export
    # function (PyInit_chrome_client_native)" -- an ABI mismatch reported as an
    # unexplained dlopen error. Measured by putting that .so on sys.path.
    if name == LEGACY_MODULE_NAME:
        directories += [
            str(root / "bindings" / "python36" / "target" / "release"),
            str(root / "bindings" / "python36" / "target" / "debug"),
        ]
    patterns = ["lib%s*.so" % name, "%s*.pyd" % name, "%s*.so" % name]
    if failures is None:
        failures = []
    for directory in directories:
        for pattern in patterns:
            try:
                candidates = sorted(pathlib.Path(directory).glob(pattern))
            except OSError:
                continue
            for path in candidates:
                # Skip the other Python generation's extension outright; the
                # glob cannot tell them apart and dlopen reports the mismatch
                # as a missing PyInit symbol.
                if LEGACY_MODULE_NAME in path.name and name != LEGACY_MODULE_NAME:
                    continue
                spec = importlib.util.spec_from_file_location(name, str(path))
                if spec is None or spec.loader is None:
                    continue
                try:
                    # ``module_from_spec`` already dlopen()s the library, so the
                    # "libminicronet.so: cannot open shared object file" failure
                    # happens here rather than in ``exec_module``.
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                except ImportError as error:
                    # Keep looking: one unloadable candidate must not hide a
                    # good one later in the list, and the reason is worth
                    # reporting if nothing else loads.
                    failures.append("%s: %s" % (path, error))
                    continue
                sys.modules.setdefault(name, module)
                return module
    return None


def _try_import(name):
    """Imports *name* as an installed extension, or returns ``None``."""
    for candidate in ("chrome_client." + name, name):
        try:
            return importlib.import_module(candidate)
        except ImportError:
            continue
    return None


def load():
    """Loads the extension this interpreter can actually use.

    Tries the name the interpreter prefers, then the other generation. The
    second attempt is not cosmetic: both wheels declare the same
    ``requires-python``, so pip may install the one whose module this
    interpreter does not ask for first, and refusing it would break an
    interpreter the project documents as supported.

    Returns:
        The loaded native module.

    Raises:
        ImportError: Neither extension could be imported. The message names
            both, and reports the Core loader-path failure when that was the
            cause.
    """
    failures = []
    for name in (MODULE_NAME, FALLBACK_MODULE_NAME):
        module = _try_import(name)
        if module is None:
            module = _from_search_path(failures, name)
        if module is not None:
            return module

    # The common failure is a Core library that is present but not on the
    # loader path, which surfaces as "libminicronet.so: cannot open shared
    # object file" from inside dlopen. Saying only "not installed" sends the
    # reader looking for a missing wheel when the fix is LD_LIBRARY_PATH.
    names = "%r or %r" % (MODULE_NAME, FALLBACK_MODULE_NAME)
    if failures:
        raise ImportError(
            "chrome_client native extension %s could not be loaded: %s. Set "
            "LD_LIBRARY_PATH (DYLD_LIBRARY_PATH on macOS, PATH on Windows) "
            "to the Core directory, e.g. core/binaries/linux-x86_64."
            % (names, "; ".join(failures)))
    raise ImportError(
        "chrome_client native extension %s is not installed and no build "
        "was found under target/release or target/debug. Build it with "
        "maturin, or set PYTHONPATH so the built module is importable."
        % (names,))

native = load()
