#!/usr/bin/env bash
set -Eeuo pipefail

OUT_DIR=${1:?usage: audit-core-windows.sh OUT_DIR}
CHROMIUM_SRC=${CHROMIUM_SRC:-/home/sj/chromium/src}
# Static MSVC/UCRT adds roughly 2 MB compared with the Linux shared libc
# build; keep a strict platform-specific ceiling instead of hiding it in the
# build output. Lowered after the memory-only disk cache patch: the largest
# Windows artifact is x86_64 at 11,291,648 bytes (x86 8,971,264,
# arm64 9,221,120).
MAX_BYTES=${MAX_BYTES:-11400000}
ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
RG=${RG:-$(command -v rg || true)}
if [[ -z $RG ]]; then
  printf 'ripgrep (rg) is required; set RG to its path\n' >&2
  exit 1
fi
rg() { "$RG" "$@"; }
READELF=$CHROMIUM_SRC/third_party/llvm-build/Release+Asserts/bin/llvm-readobj

test -x "$READELF"
LIB=${LIB:-}
if [[ -z "$LIB" ]]; then
  for candidate in "$OUT_DIR/minicronet.dll" "$OUT_DIR/libminicronet.dll"; do
    if [[ -f "$candidate" ]]; then
      LIB=$candidate
      break
    fi
  done
fi
test -n "$LIB"
test -f "$LIB"

headers=$($READELF --file-headers "$LIB")
case "$headers" in
  *'Format: COFF-x86-64'*'Magic: MZ'*)
    target_label=Windows-x86_64
    ;;
  *'Format: COFF-i386'*'Magic: MZ'*)
    target_label=Windows-x86
    ;;
  *'Format: COFF-ARM64'*'Magic: MZ'*)
    target_label=Windows-ARM64
    ;;
  *)
    printf 'Unsupported Windows PE target for %s\n' "$LIB" >&2
    exit 1
    ;;
esac

if rg -q '(components/cronet/android|obj/base/android/|\.java( |$)|\.jar( |$)|jni_headers|/jni/)' \
  "$OUT_DIR" -g '*.ninja' -g '*.rsp'; then
  printf 'Android/Java/JNI dependency leaked into Windows Core graph\n' >&2
  exit 1
fi

if strings -a "$LIB" | rg -q -i \
  'MockRandom|MINICRONET_(SSL_KEY_LOG_FILE|FORCE_QUIC_ORIGIN|FORCE_H3_WEBSOCKET|RANDOM|SEED)|fixed.?seed|random.?seed'; then
  printf 'Verification or fixed-random control leaked into release DLL\n' >&2
  exit 1
fi

exports=$($READELF --coff-exports "$LIB" 2>/dev/null |
  sed -n 's/.*Name: \([A-Za-z0-9_]*\).*/\1/p' | sort -u)
expected=$(printf '%s\n' \
  mn_abi_version \
  mn_engine_create \
  mn_engine_release \
  mn_engine_retain \
  mn_request_cancel \
  mn_request_create \
  mn_request_follow_redirect \
  mn_request_resume_read \
  mn_request_release \
  mn_request_retain \
  mn_request_start \
  mn_request_upload_write \
  mn_version_string \
  mn_websocket_cancel \
  mn_websocket_close \
  mn_websocket_create \
  mn_websocket_release \
  mn_websocket_retain \
  mn_websocket_send \
  mn_websocket_start | sort)
diff -u <(printf '%s\n' "$expected") <(printf '%s\n' "$exports")

bytes=$(stat -c %s "$LIB")
if ((bytes > MAX_BYTES)); then
  printf 'DLL size %s exceeds %s budget %s bytes\n' \
    "$bytes" "$target_label" "$MAX_BYTES" >&2
  exit 1
fi

printf 'Audited %s: %s bytes, twenty public ABI exports (%s)\n' \
  "$LIB" "$bytes" "$target_label"
