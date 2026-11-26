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
  mn_request_release_buffer
  mn_request_negotiated_protocol mn_request_ech_status
  mn_engine_capabilities
  mn_platform_user_agent_group mn_platform_hint
  mn_header_disposition
  mn_engine_cookies_get mn_engine_cookies_set mn_engine_cookies_delete
  mn_engine_cookies_clear mn_engine_set_client_certificate
  mn_engine_clear_client_certificate
  mn_engine_set_wakeup_batch mn_request_set_wakeup_token
  mn_websocket_create mn_websocket_retain mn_websocket_release
  mn_websocket_start mn_websocket_send mn_websocket_close
  mn_websocket_cancel
)

die() { printf 'ABI audit: %s\n' "$*" >&2; exit 1; }

[[ -f "$HEADER" && -f "$SYS" && -f "$CORE" ]] || die "missing ABI/FFI/Core source"

# The version is read out of the header and never written twice. This script
# used to hardcode the expected macro *and* the number in its own failure and
# success lines, so bumping the ABI to 10 left it asserting "10u" while still
# printing "not 9" and "ABI v9 audit passed". A gate whose message disagrees
# with its own check is how the next reader concludes the message is right.
abi_version="$(sed -n 's/^#define MN_ABI_VERSION \([0-9]\+\)u$/\1/p' "$HEADER")"
[[ -n "$abi_version" ]] \
  || die "header has no '#define MN_ABI_VERSION <n>u'"
grep -qx "#define MN_ABI_VERSION ${abi_version}u" "$HEADER" \
  || die "header ABI macro is not in the form '#define MN_ABI_VERSION <n>u'"

# Every other carrier of the number is compared here, because the header alone
# cannot prove the tree agrees with itself. Both of these drifted once and
# nothing noticed: core/abi/ABI_VERSION still said 8 two releases after the
# header said 10, and the FFI constant is hand-written so a bump that misses it
# leaves the extension reporting the old ABI and refusing every call at create
# time. The version file is gated rather than deleted because core/abi/README.md
# publishes it as part of the ABI surface.
abi_file="$(tr -d '[:space:]' <"$ROOT/core/abi/ABI_VERSION" 2>/dev/null || true)"
[[ "$abi_file" == "$abi_version" ]] \
  || die "core/abi/ABI_VERSION says '${abi_file}' but the header declares ${abi_version}; update it with the header"

ffi_abi="$(sed -n 's/^pub const MN_ABI_VERSION: u32 = \([0-9]\+\);$/\1/p' "$SYS")"
[[ -n "$ffi_abi" ]] \
  || die "Rust FFI has no 'pub const MN_ABI_VERSION: u32 = <n>;'"
[[ "$ffi_abi" == "$abi_version" ]] \
  || die "crates/minicronet-sys declares ABI ${ffi_abi} but the header declares ${abi_version}; the extension would refuse every call at create time"

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
    || die "$format export list differs from ABI v${abi_version}"
done

# The loop above proves every listed symbol is present in all four places.
# It cannot see the opposite direction: an export added to the header, the
# FFI and the Core while the tables kept 20 entries passed every check and
# still printed "20 symbols". The exported set is fixed per ABI, so the header
# must declare exactly this set.
python3 "$ROOT/tools/abi-exports.py" "$HEADER" "$tmp/expected"

printf 'ABI v%s audit passed: %d symbols, header/FFI/Core/export tables agree.\n' \
  "$abi_version" "${#symbols[@]}"
