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
#   README badge row                        -- slug checked against [project] name,
#                                              Python list against requires-python
#   pyproject `classifiers`                 -- the same Python range as the badge, and
#                                              what shields.io's pyversions badge reads
#
# The READMEs and docs/RUST_API_FREEZE.md used to restate the release number in
# prose; those copies are gone, and this script fails if one comes back. The
# shields.io `pypi/v` badge already renders the live version, so a second
# hand-edited copy could only ever drift.
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

# The READMEs and the API-freeze doc must not carry a hand-maintained copy of
# the release number. The shields.io `pypi/v` badge is the live one; anything
# else is a second source of truth that drifts.
version_restatement = re.compile(r"`\d+\.\d+\.\d+(?:\.\d+)?`")
for name in ("README.md", "README.en.md", "bindings/python36/README.md",
             "docs/RUST_API_FREEZE.md"):
    text = (root / name).read_text()
    for stale in ("当前版本", "Current release", "Workspace API version"):
        if stale in text:
            raise SystemExit(
                f"{name}: remove the '{stale}' line -- the release number lives only in "
                "Cargo.toml, and the README badge already renders it from PyPI")
    found = version_restatement.search(text)
    if found:
        raise SystemExit(
            f"{name}: hard-coded version {found.group(0)} -- the release number lives "
            "only in Cargo.toml; use the shields.io pypi/v badge instead")
    print(f"ok {name}: no hard-coded release number")

# docs/PROJECT_STATUS.md is the gap that let the drift happen: the gate scanned
# the READMEs but not the status document, so it sat two releases behind while
# every check stayed green. It cannot use the generic rule above because it
# legitimately quotes Chromium versions (153.0.8010.12) and the historical
# release numbers (0.2.1, 0.2.2). The precise rule is that the *current*
# workspace version must not be restated anywhere in it.
status_path = root / "docs/PROJECT_STATUS.md"
status_text = status_path.read_text()
if version in status_text:
    raise SystemExit(
        f"docs/PROJECT_STATUS.md restates the current release {version} -- the "
        "release number lives only in Cargo.toml; point at it instead of "
        "copying it, because this file drifted two releases when it did not")
print(f"ok docs/PROJECT_STATUS.md: does not restate the current release")

# Same gap, different carrier: docs/BASELINE_LINUX_X86_64.md quoted a Chromium
# commit that had already been superseded, so the document named a tree this
# repository does not build against while every gate stayed green. The revision
# has one source of truth (CHROMIUM_REVISION); a document may quote it, but only
# correctly, and the comparison is against that file rather than a second copy.
revision = (root / "CHROMIUM_REVISION").read_text().strip()
if not re.fullmatch(r"[0-9a-f]{40}", revision):
    raise SystemExit("CHROMIUM_REVISION is not a 40-character commit hash")
hex40 = re.compile(r"\b[0-9a-f]{40}\b")
for name in ("docs/PROJECT_STATUS.md", "docs/BASELINE_LINUX_X86_64.md",
             "docs/NEXT_STEPS.md", "docs/COMPATIBILITY_BOUNDARY.md",
             "docs/CORE_BINARY_SIZE_PLAN.md", "docs/ARCHITECTURE.md",
             "docs/MIGRATION_FROM_NEW.md", "README.md", "README.en.md"):
    path = root / name
    if not path.exists():
        continue
    for line_number, line in enumerate(path.read_text().splitlines(), 1):
        for found in hex40.findall(line):
            if found != revision:
                raise SystemExit(
                    f"{name}:{line_number}: commit {found} is not the pinned "
                    f"CHROMIUM_REVISION ({revision}) -- a document that quotes a "
                    "superseded tree describes a build this repository cannot make")
print("ok docs: every quoted commit is the pinned CHROMIUM_REVISION")

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
