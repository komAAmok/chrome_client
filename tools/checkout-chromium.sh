#!/usr/bin/env bash
set -Eeuo pipefail

# Checkout Chromium outside this repository. Chromium requires a path without
# spaces and a large amount of free disk for a Chromium checkout.
ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
CHROMIUM_ROOT=${CHROMIUM_ROOT:-"$ROOT_DIR/chromium"}
CHROMIUM_SRC=${CHROMIUM_SRC:-"$CHROMIUM_ROOT/src"}
CHROMIUM_COMMIT=${CHROMIUM_COMMIT:-$(<"$ROOT_DIR/CHROMIUM_REVISION")}
PATCH_FILE="$ROOT_DIR/patches/minicronet-core.patch"

if ! command -v fetch >/dev/null 2>&1 || ! command -v gclient >/dev/null 2>&1; then
  printf '%s\n' 'depot_tools is required (fetch and gclient must be on PATH).' >&2
  exit 1
fi

mkdir -p "$CHROMIUM_ROOT"

if [[ ! -d "$CHROMIUM_SRC/.git" ]]; then
  if [[ -e "$CHROMIUM_SRC" ]]; then
    printf 'Chromium source path exists but is not a Git checkout: %s\n' "$CHROMIUM_SRC" >&2
    exit 1
  fi
  (
    cd "$CHROMIUM_ROOT"
    fetch --nohooks --no-history chromium
  )
fi

# A previous run may have left this repository-owned core patch applied. Remove
# only that exact patch before moving the managed checkout to another commit.
if git -C "$CHROMIUM_SRC" apply --reverse --check "$PATCH_FILE" 2>/dev/null; then
  git -C "$CHROMIUM_SRC" apply --reverse "$PATCH_FILE"
fi

(
  cd "$CHROMIUM_SRC"
  git fetch origin "$CHROMIUM_COMMIT" --depth=1
  git checkout --detach FETCH_HEAD
)
PINNED_REVISION=$(git -C "$CHROMIUM_SRC" rev-parse HEAD)

(
  cd "$CHROMIUM_ROOT"
  gclient sync --no-history --revision "src@$PINNED_REVISION"
)

if [[ $(git -C "$CHROMIUM_SRC" rev-parse HEAD) != "$PINNED_REVISION" ]]; then
  printf 'gclient moved Chromium away from pinned revision %s.\n' \
    "$PINNED_REVISION" >&2
  exit 1
fi

if git -C "$CHROMIUM_SRC" apply --reverse --check "$PATCH_FILE" 2>/dev/null; then
  printf '%s\n' 'MiniCronet core patch already applied.'
elif git -C "$CHROMIUM_SRC" apply --check "$PATCH_FILE"; then
  git -C "$CHROMIUM_SRC" apply "$PATCH_FILE"
else
  printf 'MiniCronet core patch is incompatible with revision %s.\n' \
    "$(git -C "$CHROMIUM_SRC" rev-parse HEAD)" >&2
  exit 1
fi

printf 'Chromium checkout ready: %s\n' "$CHROMIUM_SRC"
printf 'Revision: %s\n' "$(git -C "$CHROMIUM_SRC" rev-parse HEAD)"
