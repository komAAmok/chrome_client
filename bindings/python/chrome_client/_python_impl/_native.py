"""Native extension discovery.

Python 3.6 loads ``chrome_client_native36`` and 3.7+ loads
``chrome_client_native``; one Python package ships against both, so the choice
is made here and nowhere else.
"""

import importlib
import importlib.util
import pathlib
import sys

MODULE_NAME = "chrome_client_native36" if sys.version_info < (3, 7) else "chrome_client_native"


def _from_search_path():
    """Finds an unpackaged extension in a source checkout.

    ``parents[4]`` is the repository root: this file sits at
    ``bindings/python/chrome_client/_python_impl/_native.py``.
    """
    root = pathlib.Path(__file__).resolve().parents[4]
    directories = list(sys.path) + [
        str(root / "target" / "release"),
        str(root / "target" / "debug"),
        str(root / "bindings" / "python36" / "target" / "release"),
        str(root / "bindings" / "python36" / "target" / "debug"),
    ]
    patterns = ["lib%s*.so" % MODULE_NAME, "%s*.pyd" % MODULE_NAME, "%s*.so" % MODULE_NAME]
    for directory in directories:
        for pattern in patterns:
            try:
                candidates = sorted(pathlib.Path(directory).glob(pattern))
            except OSError:
                continue
            for path in candidates:
                spec = importlib.util.spec_from_file_location(MODULE_NAME, str(path))
                if spec is None or spec.loader is None:
                    continue
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                sys.modules.setdefault(MODULE_NAME, module)
                return module
    return None


def load():
    for candidate in ("chrome_client." + MODULE_NAME, MODULE_NAME):
        try:
            return importlib.import_module(candidate)
        except ImportError:
            continue
    module = _from_search_path()
    if module is None:
        raise ImportError("chrome_client native extension is not installed")
    return module


native = load()
