#!/usr/bin/env bash
set -Eeuo pipefail

OUT_DIR=${1:?usage: audit-core-macos.sh OUT_DIR ARCH}
EXPECTED_ARCH=${2:?usage: audit-core-macos.sh OUT_DIR ARCH}
CHROMIUM_SRC=${CHROMIUM_SRC:-/home/sj/chromium/src}
MAX_BYTES=${MAX_BYTES:-12000000}
ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
RG=${RG:-$(command -v rg || true)}
if [[ -z $RG ]]; then
  printf 'ripgrep (rg) is required; set RG to its path\n' >&2
  exit 1
fi
rg() { "$RG" "$@"; }
READOBJ=$CHROMIUM_SRC/third_party/llvm-build/Release+Asserts/bin/llvm-readobj
NM=$CHROMIUM_SRC/third_party/llvm-build/Release+Asserts/bin/llvm-nm
OTOOL=$CHROMIUM_SRC/third_party/llvm-build/Release+Asserts/bin/llvm-otool
LIB=$OUT_DIR/libminicronet.dylib

test -x "$READOBJ"
test -x "$NM"
test -x "$OTOOL"
test -f "$LIB"

headers=$($READOBJ --file-headers "$LIB")
case "$EXPECTED_ARCH:$headers" in
  x86_64:*'Format: Mach-O 64-bit x86-64'*'FileType: DynamicLibrary'*)
    target_label=macOS-x86_64
    ;;
  arm64:*'Format: Mach-O arm64'*'FileType: DynamicLibrary'*)
    target_label=macOS-ARM64
    ;;
  *)
    printf 'Expected macOS %s Mach-O dynamic library: %s\n' \
      "$EXPECTED_ARCH" "$LIB" >&2
    exit 1
    ;;
esac

if rg -q '(components/cronet/android|obj/base/android/|\.java( |$)|\.jar( |$)|jni_headers|/jni/)' \
  "$OUT_DIR" -g '*.ninja' -g '*.rsp'; then
  printf 'Android/Java/JNI dependency leaked into macOS Core graph\n' >&2
  exit 1
fi

if strings -a "$LIB" | rg -q -i \
  'MockRandom|MINICRONET_(SSL_KEY_LOG_FILE|FORCE_QUIC_ORIGIN|FORCE_H3_WEBSOCKET|RANDOM|SEED)|fixed.?seed|random.?seed'; then
  printf 'Verification or fixed-random control leaked into release dylib\n' >&2
  exit 1
fi

actual=$($NM -gU "$LIB" | awk '{print $NF}' | sort -u)
expected=$(sed '/^[[:space:]]*$/d' "$ROOT_DIR/core/exports/minicronet.exports" | sort -u)
diff -u <(printf '%s\n' "$expected") <(printf '%s\n' "$actual")

version=$($READOBJ --macho-version-min "$LIB")
if [[ $version != *'Platform: macos'* || $version != *'Version: 13.0'* ||
      $version != *'SDK: 26.5'* ]]; then
  printf 'Unexpected macOS SDK or deployment target\n%s\n' "$version" >&2
  exit 1
fi

deps=$($OTOOL -L "$LIB")
dylib_id=$($OTOOL -D "$LIB" | sed -n '2p')
runtime_deps=$(awk 'NR > 1 {print $1}' <<<"$deps" | grep -Fvx "$dylib_id" || true)
if grep -qE '@rpath|@loader_path|@executable_path' <<<"$runtime_deps"; then
  printf 'Forbidden runtime dependency search path in dylib\n' >&2
  exit 1
fi
unexpected_deps=$(grep -Ev '^(/usr/lib/|/System/Library/Frameworks/)' \
  <<<"$runtime_deps" || true)
if [[ -n $unexpected_deps ]]; then
  printf 'Non-system runtime dependency in dylib:\n%s\n' "$unexpected_deps" >&2
  exit 1
fi
if $OTOOL -l "$LIB" | grep -q 'LC_RPATH'; then
  printf 'Forbidden LC_RPATH in dylib\n' >&2
  exit 1
fi

bytes=$(stat -c %s "$LIB")
if ((bytes > MAX_BYTES)); then
  printf 'Library size %s exceeds %s budget %s bytes\n' \
    "$bytes" "$target_label" "$MAX_BYTES" >&2
  exit 1
fi

printf 'Audited %s: %s bytes, twenty public ABI symbols (%s)\n' \
  "$LIB" "$bytes" "$target_label"
