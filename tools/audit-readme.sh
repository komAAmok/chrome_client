#!/usr/bin/env bash
set -euo pipefail

# The Python 3.7-3.13 and Python 3.6 wheels publish to the same PyPI release, so
# both must carry the same project introduction. maturin refuses a `readme` path
# outside the metadata root, and bindings/python36 is its own Cargo workspace, so
# the root README is mirrored there instead of referenced.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE="$ROOT/README.md"
MIRROR="$ROOT/bindings/python36/README.md"
MAIN_PROJECT="$ROOT/bindings/python/pyproject.toml"
PY36_PROJECT="$ROOT/bindings/python36/pyproject.toml"

die() { printf 'README audit: %s\n' "$*" >&2; exit 1; }

[[ -f "$SOURCE" ]] || die "missing $SOURCE"
[[ -f "$MIRROR" ]] || die "missing $MIRROR; copy README.md into bindings/python36"

cmp -s "$SOURCE" "$MIRROR" \
  || die "bindings/python36/README.md differs from README.md; run cp README.md bindings/python36/README.md"

grep -qx 'readme = "\.\./\.\./README\.md"' "$MAIN_PROJECT" \
  || die "bindings/python: readme must be \"../../README.md\""
grep -qx 'readme = "README\.md"' "$PY36_PROJECT" \
  || die "bindings/python36: readme must be \"README.md\""

for project in "$MAIN_PROJECT" "$PY36_PROJECT"; do
  grep -q '^\[project\.urls\]$' "$project" \
    || die "$(basename "$(dirname "$project")"): missing [project.urls]"
done

# Relative Markdown links resolve on GitHub but 404 on PyPI, so the published
# introduction must link out absolutely.
for readme in "$SOURCE" "$ROOT/README.en.md"; do
  if grep -nE '\]\((\.\.?/|[A-Za-z0-9_.-]+\.md|docs/)' "$readme" >/dev/null; then
    grep -nE '\]\((\.\.?/|[A-Za-z0-9_.-]+\.md|docs/)' "$readme" >&2
    die "$(basename "$readme"): relative links do not resolve on PyPI; use absolute URLs"
  fi
done

printf 'README audit passed: PyPI description matches README.md for both wheels.\n'
