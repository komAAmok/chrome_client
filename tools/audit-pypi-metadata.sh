#!/usr/bin/env bash
set -Eeuo pipefail

# Gates the PyPI-facing metadata of both wheels before anything is uploaded.
#
# The two wheels publish to the same `chrome-client` release, and PyPI renders
# the project page's "Requires: Python" as the intersection of every file's
# Requires-Python. 0.2.1.1 shipped with the python36 wheel declaring
# `>=3.6,<3.7` and the main wheel `>=3.7`, so the page showed the confusing
# "<3.7, >=3.6" even though the cp36-abi3 wheel works on every CPython 3.6-3.13.
# This script fails the build instead of letting that regress.

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
WORKSPACE_VERSION=$(grep -m1 '^version = ' "$ROOT_DIR/Cargo.toml" | sed 's/.*= *//; s/"//g')

python3 - "$WORKSPACE_VERSION" <<'PY'
import pathlib
import re
import sys

workspace_version = sys.argv[1]
expected_python = ">=3.6,<3.14"

for name in ("bindings/python/pyproject.toml", "bindings/python36/pyproject.toml"):
    text = pathlib.Path(name).read_text()
    version = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
    requires = re.search(r'^requires-python\s*=\s*"([^"]+)"', text, re.M)
    if not version or not requires:
        raise SystemExit(f"{name}: missing version or requires-python")
    if version.group(1) != workspace_version:
        raise SystemExit(
            f"{name}: version {version.group(1)} != workspace {workspace_version}")
    if requires.group(1) != expected_python:
        raise SystemExit(
            f"{name}: requires-python {requires.group(1)} != {expected_python}; "
            "PyPI renders the intersection of all wheels' bounds on the project "
            "page, so every wheel must declare the same full supported range")
    print(f"ok {name}: {version.group(1)}, requires-python {requires.group(1)}")
PY
