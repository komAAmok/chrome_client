#!/usr/bin/env bash
set -Eeuo pipefail

# Cross-build only. Runtime tests require a native or emulated i386 userland.
ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
CHROMIUM_SRC=${CHROMIUM_SRC:-/home/sj/chromium/src}
OUT_DIR=${OUT_DIR:-$CHROMIUM_SRC/out/MiniCronet-linux-x86}
JOBS=${JOBS:-2}
# Optional local pkgconf, kept outside this repository.
if [[ -n ${MINICRONET_PKGCONF_DIR:-} && -x "$MINICRONET_PKGCONF_DIR/usr/bin/pkg-config" ]]; then
  export PATH="$MINICRONET_PKGCONF_DIR/usr/bin:$PATH"
  export LD_LIBRARY_PATH="$MINICRONET_PKGCONF_DIR/usr/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi
GN=$CHROMIUM_SRC/buildtools/linux64/gn
NINJA=$CHROMIUM_SRC/third_party/ninja/ninja
OBJCOPY=$CHROMIUM_SRC/third_party/llvm-build/Release+Asserts/bin/llvm-objcopy
READELF=$CHROMIUM_SRC/third_party/llvm-build/Release+Asserts/bin/llvm-readelf

test -x "$GN"
test -x "$NINJA"
test -x "$OBJCOPY"
test -x "$READELF"

# The profile table is a committed generated header. Regeneration needs the
# profile evidence, which this repository does not own yet, so it is opt-in.
if [[ ${REGENERATE_PROFILE_TABLE:-0} == 1 ]]; then
  if [[ -z ${PROFILE_EVIDENCE_DIR:-} ]]; then
    printf 'REGENERATE_PROFILE_TABLE=1 needs PROFILE_EVIDENCE_DIR
' >&2
    exit 1
  fi
  python3 "$ROOT_DIR/tools/generate-profile-table.py"
fi
"$ROOT_DIR/tools/sync-core.sh"

mkdir -p "$OUT_DIR"
cat >"$OUT_DIR/args.gn" <<EOF
is_component_build = false
is_debug = false
is_official_build = true
is_minicronet_build = true
optimize_for_size = true
symbol_level = 0
chrome_pgo_phase = 0
target_cpu = "x86"
target_os = "linux"
use_remoteexec = false
use_reclient = false
use_siso = false
use_aura = false
use_gio = false
use_glib = false
use_ozone = false
use_udev = false
EOF

cd "$CHROMIUM_SRC"
"$GN" gen "$OUT_DIR" --fail-on-unused-args \
  --root-target=//minicronet '--root-pattern=//minicronet:*'
if rg -q '(components/cronet/android|obj/base/android/|\.java( |$)|\.jar( |$)|jni_headers|/jni/)' \
  "$OUT_DIR" -g '*.ninja'; then
  printf '%s\n' 'Android/Java/JNI dependency leaked into the desktop Core.' >&2
  exit 1
fi

"$NINJA" -C "$OUT_DIR" -j"$JOBS" \
  minicronet:minicronet \
  minicronet:minicronet_smoke \
  minicronet:minicronet_websocket_smoke \
  minicronet:minicronet_abi_cpp_smoke \
  minicronet:profile_probe

"$OBJCOPY" --strip-unneeded \
  --remove-section=.comment \
  --remove-section=.note.gnu.build-id \
  "$OUT_DIR/libminicronet.so"

file "$OUT_DIR/libminicronet.so"
"$READELF" -h "$OUT_DIR/libminicronet.so" | sed -n '1,14p'
"$ROOT_DIR/tools/audit-core-linux.sh" "$OUT_DIR"
printf 'Linux x86 cross build and audit passed: %s\n' "$OUT_DIR"
