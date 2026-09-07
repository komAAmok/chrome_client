#!/usr/bin/env bash
set -Eeuo pipefail

# Gates every version carrier against the workspace Cargo.toml, the single
# source of truth, and the PyPI-facing metadata derived from it.
#
# Where the version lives and why:
#   Cargo.toml (workspace.package.version)  -- the authority
#   bindings/python36/Cargo.toml            -- separate workspace, checked equal
#   Cargo.lock, python36/Cargo.lock         -- cargo regenerates these
#   bindings/*/pyproject.toml               -- no `version` line: maturin reads
#                                              it from the crate's Cargo.toml
#   chrome_client/__init__.py __version__   -- static string, checked equal
#   README.md / README.en.md / python36     -- static strings, checked equal
#
# Bumping the release is therefore one edit in Cargo.toml plus a cargo
# generate-lockfile; this script fails the build if any other carrier drifts.
#
# The same script keeps the Requires-Python consistent across the two wheels.
# PyPI renders the project page's "Requires: Python" as the intersection of
# every file's bound, so one wheel with a narrower bound mislabels the release
# (0.2.1.1 shipped `>=3.6,<3.7` next to `>=3.7` and the page showed
# "<3.7, >=3.6"). Both abi3 wheels cover 3.6-3.13.

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
WORKSPACE_VERSION=$(grep -m1 '^version = ' "$ROOT_DIR/Cargo.toml" | sed 's/.*= *//; s/"//g')

python3 - "$WORKSPACE_VERSION" "$ROOT_DIR" <<'PY'
import pathlib
import re
import sys

version = sys.argv[1]
root = pathlib.Path(sys.argv[2])
expected_python = ">=3.6,<3.14"
version_line = re.compile(r'^version\s*=\s*"([^"]+)"', re.M)

# bindings/python36 is its own Cargo workspace; its package version must equal
# the root workspace version.
for path in (root / "bindings/python36/Cargo.toml",):
    text = path.read_text()
    match = version_line.search(text)
    if not match or match.group(1) != version:
        raise SystemExit(f"{path}: package version != workspace {version}")
    print(f"ok {path.relative_to(root)}: {match.group(1)}")

# The pyprojects must not pin a different version: maturin reads the crate
# version from Cargo.toml, so a literal here is a second source of truth.
for name in ("bindings/python/pyproject.toml", "bindings/python36/pyproject.toml"):
    path = root / name
    text = path.read_text()
    requires = re.search(r'^requires-python\s*=\s*"([^"]+)"', text, re.M)
    if not requires or requires.group(1) != expected_python:
        raise SystemExit(
            f"{name}: requires-python must be {expected_python} (PyPI shows the "
            "intersection of all wheels' bounds; a narrower one mislabels the release)")
    pinned = version_line.search(text)
    if pinned and pinned.group(1) != version:
        raise SystemExit(
            f"{name}: version {pinned.group(1)} != workspace {version}; remove the "
            "line and let maturin read it from Cargo.toml")
    print(f"ok {name}: requires-python {requires.group(1)}"
          + (f", version line {pinned.group(1)}" if pinned else ", version from Cargo"))

# The Python runtime constant and the README badges are static strings; the
# gate is what keeps them honest.
init = (root / "bindings/python/chrome_client/__init__.py").read_text()
match = re.search(r'__version__\s*=\s*"([^"]+)"', init)
if not match or match.group(1) != version:
    raise SystemExit(
        f"__init__.py __version__ must be \"{version}\"; update it alongside Cargo.toml")
print(f"ok __init__.py: __version__ = {match.group(1)}")

for path in (root / "README.md", root / "README.en.md",
             root / "bindings/python36/README.md"):
    text = path.read_text()
    match = re.search(r"(?:当前版本|Current release)[:：]\s*`([^`]+)`", text)
    if not match or match.group(1) != version:
        raise SystemExit(
            f"{path.relative_to(root)}: version badge must read {version}")
    print(f"ok {path.relative_to(root)}: badge = {match.group(1)}")
PY
