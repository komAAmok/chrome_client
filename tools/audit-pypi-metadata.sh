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
# "<3.7, >=3.6"). One release therefore declares `>=3.6,<3.16` on both wheels,
# and the two wheels cover that range in halves: cp36-abi3 (pyo3 0.15.1 in
# bindings/python36) loads on 3.6-3.8, cp39-abi3 (pyo3 0.30.1 in bindings/python)
# loads on 3.9-3.15. Those halves are not a free choice -- 3.9 is the bottom of
# the modern extension's abi3 range and there is no abi3-py37/abi3-py38 feature
# in pyo3 0.30.1, so a cp37 or cp38 wheel cannot be built from that crate at all.
# The last block of this script asserts that the facade's runtime split and the
# modern crate's abi3 floor are the same number, because two independent numbers
# that must agree is exactly the shape that drifts silently.

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# Read through tools/version.py instead of grepping the file: `grep -m1 '^version = '`
# matched the first such key anywhere in Cargo.toml, so a `version = ` line in any
# later table ([profile.release], [workspace.dependencies], ...) could stand in for
# a deleted [workspace.package] version. version.py bounds the search to that one
# table, and it is already the declared single reader of this value.
WORKSPACE_VERSION=$(python3 "$ROOT_DIR/tools/version.py") \
  || { printf 'audit-pypi-metadata: cannot read the workspace version from tools/version.py\n' >&2; exit 1; }
[[ -n "$WORKSPACE_VERSION" ]] \
  || { printf 'audit-pypi-metadata: empty workspace version\n' >&2; exit 1; }

python3 - "$WORKSPACE_VERSION" "$ROOT_DIR" <<'PY'
import pathlib
import re
import subprocess
import sys

version = sys.argv[1]
root = pathlib.Path(sys.argv[2])
expected_python = ">=3.6,<3.16"
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
# bindings/python/README.md is the fourth README and was the one carrier no gate
# read: it is not the PyPI long description (that is the root README), and
# tools/audit-readme.sh only mirrors the python36 copy, so its PyO3/abi3 claims
# could drift with every check green.
for name in ("README.md", "README.en.md", "bindings/python36/README.md",
             "bindings/python/README.md", "docs/RUST_API_FREEZE.md"):
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
# The list used to be nine hand-picked documents. That is the same shape as the
# bug this check was written for: the gate scanned the READMEs and
# docs/PROJECT_STATUS.md while fourteen other documents went unchecked, so a
# stale commit could sit in any of them with every check green.
#
# The glob still stopped at the root and docs/, so bindings/*/README.md,
# core/*/README.md, crates/*/README.md and profiles/*.md were outside it -- 22 of
# the 48 tracked documents, and bindings/python/README.md in that set was already
# the carrier that drifted unnoticed. The list comes from `git ls-files` rather
# than a glob, so the scan is exactly the set a fresh clone gets: a recursive
# glob also picks up untracked scratch files, and this gate would then fail on a
# file that is nobody's to commit.
#
# profiles/ is excluded on purpose, not by omission: each
# profiles/chrome-NNN/README.md records the chromium_commit of that release tag
# (the tree the fingerprint evidence was captured from), which is a different
# fact from the revision this repository builds against and is legitimately not
# CHROMIUM_REVISION. Scoping the exclusion to that directory keeps the check
# honest for every other document.
tracked_markdown = subprocess.run(
    ["git", "-C", str(root), "ls-files", "--", "*.md"],
    capture_output=True, text=True, check=True).stdout.split()
candidates = sorted(
    root / name for name in tracked_markdown
    if not name.startswith("profiles/"))
tracked = subprocess.run(
    ["git", "-C", str(root), "ls-files", "--error-unmatch", "--"] +
    [str(path.relative_to(root)) for path in candidates],
    capture_output=True, text=True)
if tracked.returncode != 0:
    missing = [line for line in tracked.stderr.splitlines() if line.strip()]
    raise SystemExit(
        "a Markdown document is not tracked by Git, so a fresh clone cannot "
        "check it: " + "; ".join(missing[:5]))
scanned = 0
for path in candidates:
    name = str(path.relative_to(root))
    scanned += 1
    for line_number, line in enumerate(path.read_text().splitlines(), 1):
        for found in hex40.findall(line):
            if found != revision:
                raise SystemExit(
                    f"{name}:{line_number}: commit {found} is not the pinned "
                    f"CHROMIUM_REVISION ({revision}) -- a document that quotes a "
                    "superseded tree describes a build this repository cannot make")
print(f"ok docs: every quoted commit is the pinned CHROMIUM_REVISION ({scanned} documents)")

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

# Which extension an interpreter loads is one fact stored in two places: the
# facade picks a module name with `sys.version_info < (3, M)`, and the modern
# crate's `abi3-py3N` floor is the oldest interpreter its wheel can install on.
# Nothing links the two numbers. pyo3 only rejects an abi3 floor above the
# *building* interpreter (`cannot set a minimum Python version 3.9 higher than
# the interpreter version 3.7`); it never reads the facade. So M can drift away
# from N with every other check still green, and the damage lands on exactly the
# boundary version: with M below N, a 3.7/3.8 interpreter installs the cp36-abi3
# wheel (which ships only chrome_client_native36) but the facade asks it for
# chrome_client_native, and the import fails on a version the README marks ✓.
# Assert M == N here, at the source of both numbers.
def abi3_floor(manifest):
    text = (root / manifest).read_text()
    line = re.search(r'^pyo3\s*=.*$', text, re.M)
    if not line:
        raise SystemExit(f"{manifest}: no pyo3 dependency line to read the abi3 floor from")
    # 'abi3-py39' means "3.9", not "3.39": the trailing digits after the major
    # version are the minor, so abi3-py36 -> 6, abi3-py39 -> 9, abi3-py315 -> 15.
    features = re.findall(r'"abi3-py3(\d+)"', line.group(0))
    if len(features) != 1:
        raise SystemExit(
            f"{manifest}:{text[:line.start()].count(chr(10)) + 1}: pyo3 must enable exactly one "
            f"'abi3-py3<N>' feature, found {features or 'none'}")
    # A pinned version is the only way this crate's feature table is knowable; an
    # unpinned pyo3 could resolve to a release whose abi3 features differ.
    pinned = re.search(r'version\s*=\s*"=([\d.]+)"', line.group(0))
    if not pinned:
        raise SystemExit(
            f"{manifest}: pyo3 must be pinned with '=' so its abi3 feature table is "
            f"knowable: {line.group(0).strip()}")
    return int(features[0]), pinned.group(1), text[:line.start()].count(chr(10)) + 1

modern_floor, modern_pyo3, modern_line = abi3_floor("bindings/python/Cargo.toml")
legacy_floor, legacy_pyo3, legacy_line = abi3_floor("bindings/python36/Cargo.toml")

if legacy_floor != 6:
    raise SystemExit(
        f"bindings/python36/Cargo.toml:{legacy_line}: abi3 floor is 3.{legacy_floor}, must be "
        "abi3-py36 -- that crate is the only extension 3.6 can load")
if legacy_pyo3 != "0.15.1":
    raise SystemExit(
        f"bindings/python36/Cargo.toml:{legacy_line}: pyo3 is {legacy_pyo3}, must be =0.15.1; "
        "newer pyo3 dropped 3.6 (MINIMUM_SUPPORTED_VERSION) and would silently stop "
        "building a cp36-abi3 wheel")
# pyo3 0.30.1 ships abi3-py39..abi3-py315 only. abi3-py37/abi3-py38 are not
# features of that release, so naming one is a build error -- but it is also the
# obvious thing to "fix" the floor back to, so name the failure explicitly.
if modern_floor in (7, 8):
    raise SystemExit(
        f"bindings/python/Cargo.toml:{modern_line}: abi3-py3{modern_floor} does not exist in "
        f"pyo3 {modern_pyo3} (its abi3 features are abi3-py39..abi3-py315); the modern "
        "wheel's floor is 3.9 and cannot be lowered by editing this feature")

native_path = "bindings/python/chrome_client/_python_impl/_native.py"
native_text = (root / native_path).read_text()
switch = re.search(r'^MODULE_NAME\s*=.*sys\.version_info\s*<\s*\(\s*3\s*,\s*(\d+)\s*\)',
                   native_text, re.M)
if not switch:
    raise SystemExit(
        f"{native_path}: no 'MODULE_NAME = ... if sys.version_info < (3, M) ...' line; the "
        "facade must state which interpreter gets which extension")
switch_minor = int(switch.group(1))
switch_line = native_text[:switch.start()].count(chr(10)) + 1

if switch_minor != modern_floor:
    low, high = sorted((modern_floor, switch_minor))
    raise SystemExit(
        "the facade's extension switch must equal the modern crate's abi3 floor: "
        f"bindings/python/Cargo.toml:{modern_line} declares abi3-py3{modern_floor} (floor "
        f"3.{modern_floor}) but {native_path}:{switch_line} switches at (3, {switch_minor}). "
        f"Interpreters 3.{low}-3.{high - 1} get the wrong module: the facade asks for one "
        "the wheel they installed does not contain, and the import fails on a version the "
        "README marks supported. Set both to the same number")
print(f"ok extension split: abi3-py3{modern_floor} (3.{modern_floor}) == facade switch at "
      f"(3, {switch_minor}) ({native_path}:{switch_line})")

# bindings/python/README.md restates the same PyO3/abi3 facts in prose, and it was
# the one carrier with no gate over it. Prose cannot be derived, so the gate holds
# it to the two numbers that are derived: the pinned PyO3 version of each crate and
# the abi3 floor / wheel tags implied by that crate's feature list. A pyo3 bump or
# a floor move must then reach this file or the build fails, instead of leaving a
# README that advertises an interpreter range the wheels no longer cover.
# Whitespace is collapsed before matching: the prose wraps at 80 columns, so
# "(PyO3\n0.15.1" is the same claim as "(PyO3 0.15.1" and a raw substring search
# would report the README as missing a fact it states.
modern_readme = re.sub(r"\s+", " ", (root / "bindings/python/README.md").read_text())
# The wheel tag keeps the minor's zero padding ('abi3-py39' -> 'cp39-abi3'), while
# the feature name and the prose use the bare minor ('abi3-py39', '3.9'). Writing
# the tag as f"cp{modern_floor}-abi3" would look for 'cp9-abi3' and never match.
def wheel_tag(minor):
    return f"cp3{minor}-abi3"


expected_claims = (
    (f"PyO3 {modern_pyo3}", "the modern crate's pinned PyO3 version"),
    (f"abi3-py3{modern_floor}", "the modern crate's abi3 feature"),
    (wheel_tag(modern_floor), "the wheel tag that abi3 feature produces"),
    (f"PyO3 {legacy_pyo3}", "the legacy crate's pinned PyO3 version"),
    (f"abi3-py3{legacy_floor}", "the legacy crate's abi3 feature"),
    (wheel_tag(legacy_floor), "the wheel tag that abi3 feature produces"),
)
for claim, what in expected_claims:
    if claim not in modern_readme:
        raise SystemExit(
            f"bindings/python/README.md: no '{claim}' ({what}); the README states the "
            "PyO3/abi3 facts and nothing else checks them, so it must name the value "
            "the crates actually declare")
# The interpreter ranges the README advertises must be the ones the two wheels
# cover: 3.<modern_floor>-3.<last minor of requires-python>, and the legacy
# wheel 3.6 up to one minor below the modern floor.
modern_top = int(range_match.group(2)) - 1
for phrase in (f"3.{modern_floor}-3.{modern_top}", f"3.{modern_top}",
               f"3.6-3.{modern_floor - 1}"):
    if phrase not in modern_readme.replace("\u2013", "-"):
        raise SystemExit(
            f"bindings/python/README.md: does not state the interpreter range {phrase!r}; "
            f"the wheels cover 3.6-3.{modern_floor - 1} ({wheel_tag(legacy_floor)}) and "
            f"3.{modern_floor}-3.{modern_top} ({wheel_tag(modern_floor)})")
print(f"ok bindings/python/README.md: PyO3 {modern_pyo3}/{legacy_pyo3}, "
      f"abi3-py3{modern_floor}/abi3-py3{legacy_floor}, ranges 3.6-3.{modern_floor - 1} "
      f"and 3.{modern_floor}-3.{modern_top}")

# ---------------------------------------------------------------------------
# The redirect ceiling: one number, five carriers, no gate over them.
#
# CHROMIUM_MAX_REDIRECTS in the facade is Chromium's net::URLRequest::kMaxRedirects.
# The Core installs no redirect limit of its own, so that value is the ceiling of
# the native follow path, and the facade must refuse to promise more. The number
# then has to appear in four prose carriers, because the behaviour is surprising
# (requests defaults max_redirects to 30 and this build cannot honour it) and the
# docs are where a caller learns that. Nothing derived those copies from the
# constant, so a change to one would leave the others claiming a limit the code
# no longer enforces.
#
# The matches are deliberately narrow. "20" appears in these files next to
# requests' 30, so a bare number search would pass on either value. Each pattern
# below anchors on the surrounding words that make the claim what it is.
redirect_src = "bindings/python/chrome_client/_python_impl/sessions.py"
redirect_text = (root / redirect_src).read_text()
redirect_match = re.search(r"^CHROMIUM_MAX_REDIRECTS\s*=\s*(\d+)\s*$", redirect_text, re.M)
if not redirect_match:
    raise SystemExit(
        f"{redirect_src}: no 'CHROMIUM_MAX_REDIRECTS = <int>' assignment; the facade's "
        "redirect ceiling must stay a literal this gate can read, because four prose "
        "carriers restate it and nothing else ties them together")
redirect_cap = int(redirect_match.group(1))
redirect_line = redirect_text[:redirect_match.start()].count(chr(10)) + 1

# (path, regex, human description of the sentence the regex anchors on)
redirect_carriers = (
    ("docs/COMPATIBILITY_BOUNDARY.md",
     r"max_redirects` tighter than Chromium's " + str(redirect_cap),
     "the 'tighter than Chromium's N' sentence"),
    ("docs/COMPATIBILITY_BOUNDARY.md",
     chr(34) + "adopt Chromium" + chr(39) + "s " + str(redirect_cap) + chr(34),
     "the default-means-N sentence"),
    ("docs/COMPATIBILITY_BOUNDARY.md",
     r"ceiling is \*\*" + str(redirect_cap) + r" hops\*\*",
     "the 'ceiling is N hops' sentence"),
    ("docs/COMPATIBILITY_BOUNDARY.md",
     r"at or above " + str(redirect_cap) + r" raises",
     "the fail-closed sentence"),
    ("README.md",
     r"必须小于 " + str(redirect_cap),
     "the Chinese 'must be below N' sentence"),
    ("README.en.md",
     r"must be below " + str(redirect_cap),
     "the English 'must be below N' sentence"),
    ("README.en.md",
     r"stops at " + str(redirect_cap) + r" hops",
     "the English 'stops at N hops' sentence"),
)
for carrier, pattern, what in redirect_carriers:
    text = (root / carrier).read_text()
    if not re.search(pattern, text):
        raise SystemExit(
            f"{carrier}: no {what} for max_redirects={redirect_cap}; "
            f"{redirect_src}:{redirect_line} sets the ceiling to {redirect_cap} and the "
            "prose carriers must state the same number. If the constant really moved, "
            "update the docs; if it did not, the docs drifted")
# README.md is mirrored into bindings/python36 for the PyPI page; audit-readme.sh
# owns that equality, so this gate does not repeat it -- it only has to fail if
# the mirror carries a *different* ceiling, which would mean the copy was edited
# instead of regenerated.
mirror_text = (root / "bindings/python36/README.md").read_text()
if f"必须小于 {redirect_cap}" not in mirror_text:
    raise SystemExit(
        "bindings/python36/README.md: the mirrored README does not state "
        f"'必须小于 {redirect_cap}'; copy README.md over it (audit-readme.sh enforces "
        "byte equality, so this only fires when the mirror was edited in place)")
print(f"ok redirect ceiling: CHROMIUM_MAX_REDIRECTS={redirect_cap} "
      f"({redirect_src}:{redirect_line}) == {len(redirect_carriers)} prose carriers")
PY
