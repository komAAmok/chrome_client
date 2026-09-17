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
#   README.md / README.en.md / python36     -- release line, checked equal
#   README badge row                        -- slug checked against [project] name,
#                                              Python list against requires-python
#   pyproject `classifiers`                 -- the same Python range as the badge, and
#                                              what shields.io's pyversions badge reads
#   docs/RUST_API_FREEZE.md                 -- states the workspace version; nothing
#                                              checked it through 0.2.2
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

# The API-freeze document names the workspace version as well. It was bumped by
# hand at each release until 0.2.2 and then drifted, so it is a carrier now.
freeze = (root / "docs/RUST_API_FREEZE.md").read_text()
for needle in (f"Workspace API version: `{version}`",
               f"shipped as the Python release `{version}`"):
    if needle not in freeze:
        raise SystemExit(f"docs/RUST_API_FREEZE.md: must state {needle!r}")
print(f"ok docs/RUST_API_FREEZE.md: workspace version {version}")

# The badge row at the top of each README is the at-a-glance version and
# Python-support statement. Its slug has to be the published name and its
# version list has to track requires-python, so a rename or a range bump cannot
# leave the README advertising something the wheels do not do.
project_name = re.search(
    r'^name\s*=\s*"([^"]+)"', (root / "bindings/python/pyproject.toml").read_text(), re.M)
if not project_name:
    raise SystemExit("bindings/python/pyproject.toml: no [project] name for the badge slug")
slug = project_name.group(1)

range_match = re.fullmatch(r">=3\.(\d+),<3\.(\d+)", expected_python)
if not range_match:
    raise SystemExit(
        f"cannot derive the badge's Python list from requires-python {expected_python!r}; "
        "this audit assumes the 3.x-only range the wheels declare")
supported = [f"3.{minor}" for minor in range(int(range_match.group(1)), int(range_match.group(2)))]
expected_badge = "https://img.shields.io/badge/python-" + "%20%7C%20".join(supported) + "-blue"

for path in (root / "README.md", root / "README.en.md",
             root / "bindings/python36/README.md"):
    text = path.read_text()
    for needle in (f"img.shields.io/pypi/v/{slug}", f"img.shields.io/pypi/l/{slug}"):
        if needle not in text:
            raise SystemExit(
                f"{path.relative_to(root)}: badge row must reference {needle}")
    if expected_badge not in text:
        raise SystemExit(
            f"{path.relative_to(root)}: the Python badge must list {supported[0]}-{supported[-1]} "
            f"(from requires-python {expected_python}); update the badge URL")
    print(f"ok {path.relative_to(root)}: badges = {slug}, "
          f"python {supported[0]}-{supported[-1]}")

# The classifiers make the same claim in wheel metadata, and they are what
# shields.io's pypi/pyversions badge reads -- so the README could switch to the
# dynamic badge as soon as a release carries them.
expected_classifiers = ["3"] + [version.split("3.")[1] for version in supported]
for manifest in ("bindings/python/pyproject.toml", "bindings/python36/pyproject.toml"):
    declared = re.findall(
        r'"Programming Language :: Python :: (3(?:\.\d+)?)"',
        (root / manifest).read_text())
    if declared != ["3"] + [f"3.{minor}" for minor in expected_classifiers[1:]]:
        raise SystemExit(
            f"{manifest}: Python classifiers {declared} must match "
            f"['3'] + {supported} (the same range as requires-python and the badge)")
    print(f"ok {manifest}: {len(declared)} Python classifiers")
PY
