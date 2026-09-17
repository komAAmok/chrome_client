#!/usr/bin/env python3
"""Regenerates the derived parts of the Python type stubs.

Most of ``bindings/python/chrome_client/**/*.pyi`` is hand-written, but two parts
are derived and must not be edited by hand:

1. **Profile ``Literal`` blocks** in ``_python_impl/impersonate.pyi``, derived from
   the runtime constants ``OLDEST_CHROME`` / ``LATEST_CHROME`` / ``HTTP_VERSIONS``.
   Bumping a pinned Chrome major means 112 string literals have to change; typing
   them out is how a version silently disappears from IDE completion.
2. **Facade modules** -- ``chrome_client/<mod>.pyi``,
   ``chrome_client/requests/<mod>.pyi`` and ``chrome_client/requests/__init__.pyi``.
   At runtime those modules *are* the ``_python_impl`` ones (the package aliases
   them into ``sys.modules``), so their stubs re-export the same public names with
   the PEP 484 ``X as X`` convention. Generating them from the implementation
   stubs keeps the list complete when a public name is added.

Both rewrites are idempotent: a file that is already current is left byte-identical.

Usage::

    python3 tools/generate-python-stubs.py          # rewrite what is stale
    python3 tools/generate-python-stubs.py --check  # exit 1 if anything is stale
"""

from __future__ import annotations

import argparse
import ast
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
PY_PKG = REPO / "bindings" / "python" / "chrome_client"
IMPL = PY_PKG / "_python_impl"

#: Modules whose facade stub is a pure alias of the ``_python_impl`` module.
FACADE_MODULES = (
    "adapters",
    "api",
    "auth",
    "cookies",
    "engine",
    "exceptions",
    "impersonate",
    "models",
    "multipart",
    "sessions",
    "status_codes",
    "structures",
    "utils",
    "websockets",
)

#: One-line summary used at the top of each generated facade stub.
FACADE_SUMMARY = {
    "adapters": "Transport adapters: the mount point for custom transports.",
    "api": "Module-level ``get``/``post``/... over one process-wide session.",
    "auth": "Authentication handlers, matching ``requests.auth``.",
    "cookies": "``RequestsCookieJar`` and the Core cookie-store bridge.",
    "engine": "Chromium engine configuration and per-session caching.",
    "exceptions": "requests' exception hierarchy plus curl_cffi's extra leaves.",
    "impersonate": "Profile selection: ``Literal`` lists of every accepted value.",
    "models": "``Request``, ``PreparedRequest``, ``Response``, ``AsyncResponse``.",
    "multipart": "Form, JSON and multipart body encoding, plus ``CurlMime``.",
    "sessions": "``Session``, ``AsyncSession``, ``Client`` and ``AsyncClient``.",
    "status_codes": "``codes``: status name to number, including upper-case aliases.",
    "structures": "``CaseInsensitiveDict``, ``Headers`` and ``LookupDict``.",
    "utils": "Helpers mirroring ``requests.utils``.",
    "websockets": "``WebSocket`` and ``AsyncWebSocket``.",
}

#: Names the package root defines itself, so it must not re-export them.
ROOT_LOCAL_NAMES = {"core_version", "abi_version", "__all__"}


# ---------------------------------------------------------------------------
# Profile literals
# ---------------------------------------------------------------------------

def runtime_values():
    """Returns ``(oldest, latest, http_version_keys)`` from the runtime module."""
    tree = ast.parse((IMPL / "impersonate.py").read_text(encoding="utf-8"))
    constants: dict[str, object] = {}
    http_versions = None
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not isinstance(target, ast.Name):
                continue
            if target.id in ("OLDEST_CHROME", "LATEST_CHROME"):
                constants[target.id] = ast.literal_eval(node.value)
            if target.id == "HTTP_VERSIONS":
                http_versions = [ast.literal_eval(key) for key in node.value.keys]  # type: ignore[attr-defined]
    missing = {"OLDEST_CHROME", "LATEST_CHROME"} - set(constants)
    if missing or http_versions is None:
        raise SystemExit("could not read %s from impersonate.py" % (missing or "HTTP_VERSIONS"))
    return constants["OLDEST_CHROME"], constants["LATEST_CHROME"], http_versions


def expected_literals():
    oldest, latest, http_versions = runtime_values()
    return {
        "ChromeProfileName": ["chrome_%d" % n for n in range(oldest, latest + 1)],
        "ChromeProfileAlias": ["chrome%d" % n for n in range(oldest, latest + 1)],
        "ChromeFamilyAlias": ["chrome", "chromium"],
        "HttpVersion": http_versions,
    }


def render_literal(values, per_line):
    # Double quotes and a trailing comma on every entry, so a rewrite of an
    # already-current block is byte-identical and ``--check`` stays meaningful.
    rendered = ['"%s",' % value if isinstance(value, str) else "%r," % value for value in values]
    if per_line == 1:
        return ["    " + item for item in rendered]
    lines, row = [], []
    for item in rendered:
        row.append(item)
        if len(row) == per_line:
            lines.append("    " + " ".join(row))
            row = []
    if row:
        lines.append("    " + " ".join(row))
    return lines


def regenerate_literals():
    """Returns the current ``impersonate.pyi`` text with its Literal blocks updated."""
    path = IMPL / "impersonate.pyi"
    lines = path.read_text(encoding="utf-8").split("\n")
    for name, values in expected_literals().items():
        header = next(
            index for index, line in enumerate(lines) if line.startswith("%s: TypeAlias = Literal[" % name)
        )
        closing = lines.index("]", header)
        per_line = 4 if name in ("ChromeProfileName", "ChromeProfileAlias") else 1
        lines[header + 1 : closing] = render_literal(values, per_line)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Facade stubs
# ---------------------------------------------------------------------------

def public_names(path: pathlib.Path) -> list[str]:
    """Every name a stub defines or re-exports, in source order.

    The PEP 484 convention marks a re-export as ``from x import Name as Name``; a
    plain ``import Name`` is an implementation detail and is skipped, as are
    ``from . import submodule`` lines.
    """
    names: list[str] = []
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if _public(node.name):
                names.append(node.name)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if _public(node.target.id):
                names.append(node.target.id)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and _public(target.id):
                    names.append(target.id)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            for alias in node.names:
                if alias.asname == alias.name and _public(alias.name):
                    names.append(alias.name)
    seen: set[str] = set()
    return [name for name in names if not (name in seen or seen.add(name))]


def _public(name: str) -> bool:
    return not name.startswith("_") or (name.startswith("__") and name.endswith("__"))


def render_import(names, module_path):
    lines = ["from %s import (" % module_path]
    lines.extend("    %s as %s," % (name, name) for name in names)
    lines.append(")")
    return "\n".join(lines)


def facade_document(title, alias_of, imports):
    typing_import = ["from typing import List", ""] if "__all__" in imports else []
    return "\n".join(
        ['"""%s' % title, ""]
        + [
            "At runtime this module *is* ``%s``: the package aliases it" % alias_of,
            "into ``sys.modules`` on import. The names below are re-exported",
            "explicitly, so a type checker follows the same path instead of",
            "seeing ``Any`` (PEP 561).",
        ]
        + ['"""', ""]
        + typing_import
        + [imports, ""]
    )


ROOT_TEMPLATE = '''"""Chromium Core HTTP/WebSocket client.

Two API shapes over one Chromium network stack:

* ``requests``-shaped:  ``Session``, ``Response``, ``session.cookies``,
  ``session.proxies``, the ``requests.exceptions`` hierarchy and ``codes``.
* ``curl_cffi``-shaped:  ``impersonate``, ``http_version`` for HTTP/1.1, HTTP/2
  and HTTP/3, ``AsyncSession``, ``CurlMime``, ``Headers``, ``Cookies`` and
  ``WebSocket``.

    import chrome_client
    with chrome_client.Session(impersonate="chrome_153") as session:
        session.get("https://example.com")

``chrome_client.requests`` mirrors the ``requests`` module namespace, including
``requests.Session``::

    from chrome_client import requests
    with requests.Session() as session:
        session.get("https://example.com")

The submodules below are the same objects as their ``_python_impl``
counterparts, so ``chrome_client.exceptions.Timeout`` and
``import chrome_client.utils`` resolve exactly as their requests equivalents do.
"""

from typing import List, Optional

%s

from . import adapters as adapters, api as api, auth as auth, cookies as cookies
from . import engine as engine, exceptions as exceptions, impersonate as impersonate
from . import models as models, multipart as multipart, requests as requests
from . import sessions as sessions, status_codes as status_codes, structures as structures
from . import utils as utils, websockets as websockets

#: This release's version, taken from the workspace ``Cargo.toml``.
__version__: str


def core_version() -> Optional[str]:
    """Version string reported by the loaded Core, or ``None``.

    Returns:
        The Core's build version, e.g. ``"153.0.8010.37"``. ``None`` when the
        Core reports none.

    Example:
        >>> import chrome_client
        >>> chrome_client.core_version()      # doctest: +SKIP
        '153.0.8010.37'
    """
    ...


def abi_version() -> int:
    """Core ABI version this build links against.

    Returns:
        The ABI number, ``8`` for this release. The wheel refuses to load a Core
        whose ABI differs, so this always matches the Core in use.

    Example:
        >>> import chrome_client
        >>> chrome_client.abi_version()
        8
    """
    ...


#: Every public name, plus ``"requests"`` and ``"__version__"``.
__all__: List[str]
'''



def regenerate_facades():
    """Yields ``(path, expected_text)`` for every generated facade stub."""
    for module in FACADE_MODULES:
        names = public_names(IMPL / (module + ".pyi"))
        if not names:
            raise SystemExit("no public names found in _python_impl/%s.pyi" % module)
        yield (
            PY_PKG / (module + ".pyi"),
            facade_document(
                FACADE_SUMMARY[module],
                "chrome_client._python_impl." + module,
                render_import(names, "._python_impl." + module),
            ),
        )
        yield (
            PY_PKG / "requests" / (module + ".pyi"),
            facade_document(
                FACADE_SUMMARY[module],
                "chrome_client._python_impl." + module,
                render_import(names, ".._python_impl." + module),
            ),
        )

    package_names = public_names(IMPL / "__init__.pyi")
    requests_names = [name for name in package_names if name != "__all__"]
    yield (
        PY_PKG / "requests" / "__init__.pyi",
        facade_document(
            "``requests``-shaped namespace over the Chromium Core.",
            "chrome_client._python_impl",
            render_import(requests_names, ".._python_impl")
            + "\n\n#: Same names the implementation package exports.\n__all__: List[str]",
        ),
    )

    root_names = [name for name in package_names if name not in ROOT_LOCAL_NAMES]
    yield (
        PY_PKG / "__init__.pyi",
        ROOT_TEMPLATE % render_import(root_names, "._python_impl"),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="write nothing; fail if a rewrite is due")
    args = parser.parse_args()

    stale: list[str] = []

    literals = IMPL / "impersonate.pyi"
    if regenerate_literals() != literals.read_text(encoding="utf-8"):
        stale.append("_python_impl/impersonate.pyi (profile Literals)")

    facades = sorted(regenerate_facades())
    for path, expected in facades:
        if path.read_text(encoding="utf-8") != expected:
            stale.append(str(path.relative_to(REPO)))

    if not stale:
        print("generated stubs are up to date (%d facade files + profile literals)" % len(facades))
        return 0

    if args.check:
        for item in stale:
            print("stale: %s" % item, file=sys.stderr)
        print("run tools/generate-python-stubs.py to rewrite them", file=sys.stderr)
        return 1

    literals.write_text(regenerate_literals(), encoding="utf-8")
    for path, expected in facades:
        path.write_text(expected, encoding="utf-8")
    for item in stale:
        print("rewrote %s" % item)
    return 0


if __name__ == "__main__":
    sys.exit(main())
