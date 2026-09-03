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

for symbol in "${symbols[@]}"; do
  grep -Eq "\\b${symbol}\\b" "$HEADER" || die "$symbol: missing from header"
  grep -Eq "pub fn ${symbol}\\(" "$SYS" || die "$symbol: missing from Rust FFI"
  grep -Eq "(^|[^[:alnum:]_])${symbol}\\(" "$CORE" \
    || die "$symbol: missing from Core implementation"
done

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

awk '/^[[:space:]]+mn_[a-z_]+;[[:space:]]*$/ { gsub(/[;[:space:]]/, ""); print }' \
  "$ROOT/core/exports/minicronet.lds" | sort -u >"$tmp/lds"
sed -n '/^EXPORTS$/,/^$/ s/^  //p' "$ROOT/core/exports/minicronet.def" | sort -u >"$tmp/def"
sed 's/^_//' "$ROOT/core/exports/minicronet.exports" | sort -u >"$tmp/exports"
printf '%s\n' "${symbols[@]}" | sort -u >"$tmp/expected"

for format in lds def exports; do
  cmp -s "$tmp/expected" "$tmp/$format" \
    || die "$format export list differs from ABI v8"
done

printf 'ABI v8 audit passed: %d symbols, header/FFI/Core/export tables agree.\n' \
  "${#symbols[@]}"
