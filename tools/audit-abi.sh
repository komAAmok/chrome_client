#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HEADER="$ROOT/core/abi/minicronet.h"
SYS="$ROOT/crates/minicronet-sys/src/lib.rs"
CORE="$ROOT/core/source/minicronet.cc"

symbols=(
  mn_abi_version mn_version_string
  mn_engine_create mn_engine_retain mn_engine_release
  mn_request_create mn_request_retain mn_request_release
  mn_request_start mn_request_cancel mn_request_upload_write
  mn_request_follow_redirect mn_request_resume_read
  mn_websocket_create mn_websocket_retain mn_websocket_release
  mn_websocket_start mn_websocket_send mn_websocket_close
  mn_websocket_cancel
)

die() { printf 'ABI audit: %s\n' "$*" >&2; exit 1; }

[[ -f "$HEADER" && -f "$SYS" && -f "$CORE" ]] || die "missing ABI/FFI/Core source"
grep -qx '#define MN_ABI_VERSION 8u' "$HEADER" \
  || die "header ABI version is not 8"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

# Every carrier is stripped of C and C++ comments before any symbol is looked
# for. Matching the raw text made the audit blind to the one edit that actually
# removes an export: commenting the declaration out. Deleting the MN_EXPORT line
# from the header, the "pub fn" from the FFI and the MN_CALL definition from the
# Core while leaving them in /* */ or // comments passed all 60 greps and still
# printed "20 symbols". A commented-out declaration is not a declaration.
python3 - "$HEADER" "$SYS" "$CORE" "$tmp" <<'PY'
import pathlib
import re
import sys

# /* ... */ (possibly multi-line) and // to end of line. The three carriers hold
# no string literal containing a comment marker, so a plain textual strip cannot
# eat a declaration that is really there.
COMMENTS = re.compile(r"/\*.*?\*/|//[^\n]*", re.S)
for source, name in zip(sys.argv[1:4], ("header", "sys", "core")):
    text = pathlib.Path(source).read_text()
    pathlib.Path(sys.argv[4], name).write_text(COMMENTS.sub("", text))
PY

for symbol in "${symbols[@]}"; do
  grep -Eq "\\b${symbol}\\(" "$tmp/header" \
    || die "$symbol: missing from header (a commented-out declaration is not a declaration)"
  grep -Eq "pub fn ${symbol}\\(" "$tmp/sys" \
    || die "$symbol: missing from Rust FFI (a commented-out declaration is not a declaration)"
  # Anchored on MN_CALL, the calling-convention macro every ABI definition uses.
  # Matching a bare "symbol(" accepted any *call* of the function: renaming only
  # the definition of mn_request_retain to mn_request_retain_REMOVED still passed,
  # because mn_request_start() calls it internally. The definition is what the
  # export table points at, so the definition is what must be there.
  grep -Eq "MN_CALL[[:space:]]+${symbol}\\(" "$tmp/core" \
    || die "$symbol: missing from Core implementation (no 'MN_CALL $symbol(' definition)"
done

awk '/^[[:space:]]+mn_[a-z_]+;[[:space:]]*$/ { gsub(/[;[:space:]]/, ""); print }' \
  "$ROOT/core/exports/minicronet.lds" | sort -u >"$tmp/lds"
sed -n '/^EXPORTS$/,/^$/ s/^  //p' "$ROOT/core/exports/minicronet.def" | sort -u >"$tmp/def"
sed 's/^_//' "$ROOT/core/exports/minicronet.exports" | sort -u >"$tmp/exports"
printf '%s\n' "${symbols[@]}" | sort -u >"$tmp/expected"

for format in lds def exports; do
  cmp -s "$tmp/expected" "$tmp/$format" \
    || die "$format export list differs from ABI v8"
done

# The loop above proves every listed symbol is present in all four places.
# It cannot see the opposite direction: an export added to the header, the
# FFI and the Core while the tables kept 20 entries passed every check and
# still printed "20 symbols". ABI v8 is frozen, so the header must declare
# exactly this set.
python3 "$ROOT/tools/abi-exports.py" "$HEADER" "$tmp/expected"

printf 'ABI v8 audit passed: %d symbols, header/FFI/Core/export tables agree.\n' \
  "${#symbols[@]}"
