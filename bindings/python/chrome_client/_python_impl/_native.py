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
MODULE_NAME = LEGACY_MODULE_NAME if sys.version_info < (3, 9) else "chrome_client_native"


def _from_search_path(failures=None):
    """Finds an unpackaged extension in a source checkout.

    ``parents[4]`` is the repository root: this file sits at
    ``bindings/python/chrome_client/_python_impl/_native.py``.

    ``failures`` is an optional list; every candidate that was found but could
    not be loaded appends its path and reason, so the caller can report why
    instead of only that nothing matched.
    """
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
    if MODULE_NAME == LEGACY_MODULE_NAME:
        directories += [
            str(root / "bindings" / "python36" / "target" / "release"),
            str(root / "bindings" / "python36" / "target" / "debug"),
        ]
    patterns = ["lib%s*.so" % MODULE_NAME, "%s*.pyd" % MODULE_NAME, "%s*.so" % MODULE_NAME]
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
                if LEGACY_MODULE_NAME in path.name and MODULE_NAME != LEGACY_MODULE_NAME:
                    continue
                spec = importlib.util.spec_from_file_location(MODULE_NAME, str(path))
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
                sys.modules.setdefault(MODULE_NAME, module)
                return module
    return None


def load():
    for candidate in ("chrome_client." + MODULE_NAME, MODULE_NAME):
        try:
            return importlib.import_module(candidate)
        except ImportError:
            continue
    failures = []
    module = _from_search_path(failures)
    if module is None:
        # The common failure is a Core library that is present but not on the
        # loader path, which surfaces as "libminicronet.so: cannot open shared
        # object file" from inside dlopen. Saying only "not installed" sends the
        # reader looking for a missing wheel when the fix is LD_LIBRARY_PATH.
        detail = failures
        if detail:
            raise ImportError(
                "chrome_client native extension %r could not be loaded: %s. Set "
                "LD_LIBRARY_PATH (DYLD_LIBRARY_PATH on macOS, PATH on Windows) "
                "to the Core directory, e.g. core/binaries/linux-x86_64."
                % (MODULE_NAME, "; ".join(detail)))
        raise ImportError(
            "chrome_client native extension %r is not installed and no build "
            "was found under target/release or target/debug. Build it with "
            "maturin, or set PYTHONPATH so the built module is importable."
            % (MODULE_NAME,))
    return module


native = load()
