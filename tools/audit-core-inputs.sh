#!/usr/bin/env bash
set -Eeuo pipefail

# Every Core build input must be owned by this repository, not by whichever
# Chromium checkout happens to sit next to it. tools/sync-core.sh used to require
# four profile-verification probes that only ever existed in the developer's
# Chromium tree, so it passed on that machine and died on every fresh clone.
#
# Four invariants, because each direction is blind to something the others see:
#   1. every name core/source/BUILD.gn references resolves to exactly one file
#      here and is tracked by Git;
#   2. every file tools/sync-core.sh would install is tracked by Git;
#   3. every compilation unit under core/source/ is referenced by BUILD.gn, so a
#      source that no target names cannot rot in the synced tree unnoticed;
#   4. the patch set is sync-core.sh's own apply_patch list, not a glob over
#      core/patches/, so deleting a patch fails instead of shrinking the set.
# Invariants 3 and 4 are what make 1 and 2 self-checking: a reference that stops
# looking like a file name, or a patch that disappears, would otherwise just make
# the scan cover less while still reporting success.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_GN="$ROOT/core/source/BUILD.gn"
SYNC="$ROOT/tools/sync-core.sh"

die() { printf 'Core input audit: %s\n' "$*" >&2; exit 1; }

[[ -f $BUILD_GN ]] || die "missing $BUILD_GN"
[[ -f $SYNC ]] || die "missing $SYNC"
git -C "$ROOT" rev-parse --git-dir >/dev/null 2>&1 \
  || die "$ROOT is not a Git checkout, so tracking cannot be verified"

# --error-unmatch fails for an untracked file and for a missing one alike; the two
# are reported separately so the message says which one to fix.
require_tracked() {
  local relative=$1
  [[ -f "$ROOT/$relative" ]] || die "$relative is missing from this repository"
  git -C "$ROOT" ls-files --error-unmatch -- "$relative" >/dev/null 2>&1 \
    || die "$relative exists but is not tracked; a fresh clone cannot build it"
}

# The directories tools/sync-core.sh flattens into $CHROMIUM_SRC/minicronet/.
SEARCH_DIRS=(core/source core/source/minicronet core/exports)

# Source files may be referenced without a path but never outside these
# directories, and a GN import is the only dotted name here that is not a file in
# this repository.
SRC_RE='^[A-Za-z0-9_./-]+\.[A-Za-z0-9]+$'
SKIP_RE='^//'

# Every dotted name in BUILD.gn is a candidate, not just the extensions known
# today: a pattern that lists .c/.cc/.h silently stops checking the day someone
# adds a .cpp or a .inc. Names whose extension nothing here installs are a hard
# failure instead, so the file must be taught about a new extension rather than
# quietly skipping it.
candidates=$(grep -oE '"[A-Za-z0-9_./-]+\.[A-Za-z0-9]+"' "$BUILD_GN" | tr -d '"' | sort -u) \
  || die "no dotted names found in core/source/BUILD.gn; the extraction broke"

names=''
while IFS= read -r name; do
  [[ -n $name ]] || continue
  [[ $name =~ $SKIP_RE ]] && continue
  [[ $name =~ $SRC_RE ]] \
    || die "BUILD.gn names $name, which is neither an import nor a plain file name"
  case ${name##*.} in
    c|cc|h|def|exports|lds) ;;
    *) die "BUILD.gn names $name; nothing installs a .${name##*.} file. Teach this audit and tools/sync-core.sh about it, or drop the reference." ;;
  esac
  names+="$name"$'\n'
done <<<"$candidates"
[[ -n $names ]] || die "no source names found in core/source/BUILD.gn; the extraction broke"

resolved=0
while IFS= read -r name; do
  [[ -n $name ]] || continue
  hits=0
  for dir in "${SEARCH_DIRS[@]}"; do
    if [[ -f "$ROOT/$dir/$name" ]]; then
      require_tracked "$dir/$name"
      hits=$((hits + 1))
    fi
  done
  ((hits == 1)) \
    || die "BUILD.gn names $name, but it matches $hits files under ${SEARCH_DIRS[*]}"
  resolved=$((resolved + 1))
done <<<"$names"

installed=0
for glob in 'core/source/*.c' 'core/source/*.cc' 'core/source/minicronet/*.h'; do
  matched=0
  for file in "$ROOT"/$glob; do
    [[ -e $file ]] || continue
    matched=1
    require_tracked "${file#"$ROOT"/}"
    installed=$((installed + 1))
  done
  ((matched)) || die "$glob matches nothing"
done

# The reverse of the loop above: a file that no target references is shipped into
# the Chromium tree by tools/sync-core.sh and never compiled or included, so it
# rots unnoticed. This is also what makes the name extraction self-checking -- a
# BUILD.gn reference that stopped looking like a file name leaves its source
# unreferenced, which fails here instead of silently shrinking the checked set.
#
# Headers are included in this loop, not just compilation units. They were
# installed by the glob at the top and then never checked in this direction, so a
# new header that BUILD.gn does not name was copied into every synced tree with
# the audit still reporting success -- the same one-way coverage that let the
# version gate miss docs/PROJECT_STATUS.md. A header nothing includes is dead
# weight at best and a sign the target list is stale at worst.
#
# BUILD.gn names every file by bare basename -- the resolution loop above looks it
# up under each of SEARCH_DIRS -- so a header at core/source/minicronet/engine.h
# appears as "engine.h", not "minicronet/engine.h". Comparing the path relative to
# core/source/ would therefore fail on every header that is correctly listed.
for file in "$ROOT"/core/source/*.c "$ROOT"/core/source/*.cc \
            "$ROOT"/core/source/minicronet/*.h; do
  [[ -e $file ]] || continue
  base=$(basename "$file")
  grep -qx "$base" <<<"$names" \
    || die "core/source/${file#"$ROOT/core/source/"} exists but no target in core/source/BUILD.gn references it"
done

# Installed by name rather than by glob, so they need their own entries.
for file in core/source/BUILD.gn core/abi/minicronet.h core/icu/icudtl.dat \
            tools/version.py; do
  require_tracked "$file"
done

# The patch set is what sync-core.sh says it applies. Deriving it from the script
# instead of from the directory is what makes a deleted patch a failure: a glob
# over core/patches/ simply stops listing the file that is gone.
declared=$(grep -oE '^apply_patch [A-Za-z0-9_.-]+\.patch' "$SYNC" \
  | awk '{print $2}' | sort -u) \
  || die "no apply_patch lines found in tools/sync-core.sh"

patches=0
while IFS= read -r name; do
  [[ -n $name ]] || continue
  require_tracked "core/patches/$name"
  patches=$((patches + 1))
done <<<"$declared"

# The other direction: a patch no longer applied is dead weight in the tree.
for file in "$ROOT"/core/patches/*.patch; do
  [[ -e $file ]] || die "core/patches holds no patch"
  base=${file#"$ROOT"/}
  grep -qx "${base#core/patches/}" <<<"$declared" \
    || die "$base is not applied by tools/sync-core.sh"
done

printf 'Core input audit: %d BUILD.gn names, %d installed files, %d patches -- all tracked\n' \
  "$resolved" "$installed" "$patches"
