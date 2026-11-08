#!/usr/bin/env bash
set -euo pipefail

# The Python 3.9-3.15 and Python 3.6-3.8 wheels publish to the same PyPI release, so
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
# Both are also the files whose links are checked below. `grep` on a missing file
# exits 2, which an `if grep ...` condition reads as "no relative links found",
# so a deleted README.en.md passed this gate silently.
[[ -f "$ROOT/README.en.md" ]] || die "missing $ROOT/README.en.md"
# The fourth README. It is not the PyPI long description and it is not the
# python36 mirror, so neither of the checks above reaches it -- the same shape as
# the version gate that scanned the READMEs but not docs/PROJECT_STATUS.md.
[[ -f "$ROOT/bindings/python/README.md" ]] \
  || die "missing $ROOT/bindings/python/README.md"

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
# introduction must link out absolutely. bindings/python/README.md is scanned too:
# it is the one README neither the mirror check nor the PyPI-description check
# above reads, so a relative link there could 404 for anyone arriving from PyPI.
#
# The rule is stated as "every target must be absolute" rather than as a list of
# the relative shapes seen so far. The previous pattern enumerated three of them
# ("./", "x.md", "docs/"), so a link into a sibling package
# ("bindings/python/README.md"), into a nested directory
# ("profiles/README.md") or to an extensionless file ("LICENSE") matched
# nothing and was reported as clean -- a gate whose failure mode is silence.
LINK_TARGET='\]\([^)]*\)'
ABSOLUTE_TARGET='^\]\((https?://|mailto:|#)'
for readme in "$SOURCE" "$ROOT/README.en.md" "$ROOT/bindings/python/README.md"; do
  # The repo-relative path, not basename: three of the scanned files are called
  # README.md, so a bare name would not say which one to fix.
  name=${readme#"$ROOT"/}
  targets_status=0
  targets=$(grep -oE "$LINK_TARGET" "$readme") || targets_status=$?
  case $targets_status in
    0) ;;
    1) continue ;;  # no link targets at all: nothing can be relative
    *)
      # grep exits 2 for an unreadable file or a bad pattern. Reading that as
      # "no relative links" is how a deleted README passed this gate.
      die "$name: grep exited $targets_status, so the link scan did not run"
      ;;
  esac
  relative_status=0
  relative=$(printf '%s\n' "$targets" | grep -vE "$ABSOLUTE_TARGET") || relative_status=$?
  case $relative_status in
    0)
      printf '%s\n' "$relative" >&2
      die "$name: the targets above are not absolute URLs; they resolve on GitHub but 404 on PyPI"
      ;;
    1) ;;  # every target is absolute: what this check wants
    *)
      die "$name: the absolute-target filter exited $relative_status, so the link scan did not run"
      ;;
  esac
done

# The two languages drift independently: README.en.md is not the PyPI long
# description, so neither the mirror check nor the pyproject check above reads it,
# and its capability tables went a release describing a different product when
# ARMv7 was added to one file and not the other. That comparison needs a table
# parser, so it lives next door in audit-readme-tables.py and is invoked here --
# two gates a caller has to remember are one gate that nobody runs.
python3 "$ROOT/tools/audit-readme-tables.py"

printf 'README audit passed: PyPI description matches README.md for both wheels.\n'
