#!/usr/bin/env bash
set -Eeuo pipefail

# Cross-build only. Runtime tests require macOS on Intel.
ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
CHROMIUM_SRC=${CHROMIUM_SRC:-/home/sj/chromium/src}
OUT_DIR=${OUT_DIR:-$CHROMIUM_SRC/out/MiniCronet-macos-x86_64}
OSXCROSS_TARGET=${OSXCROSS_TARGET:-/home/sj/chromium/toolchains/osxcross-target}
JOBS=${JOBS:-4}
GN=$CHROMIUM_SRC/buildtools/linux64/gn
NINJA=$CHROMIUM_SRC/third_party/ninja/ninja

test -x "$GN"
test -x "$NINJA"
python3 "$ROOT_DIR/tools/prepare-osxcross-toolchain.py" "$OSXCROSS_TARGET" "$CHROMIUM_SRC"
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
enable_dsyms = false
chrome_pgo_phase = 0
target_cpu = "x64"
target_os = "mac"
use_system_xcode = false
mac_sdk_path = "$OSXCROSS_TARGET/SDK/MacOSX26.5.sdk"
mac_sdk_official_version = "26.5"
mac_sdk_official_build_version = "25F70"
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
  "$OUT_DIR" -g '*.ninja' -g '*.rsp'; then
  printf '%s\n' 'Android/Java/JNI dependency leaked into the macOS Core.' >&2
  exit 1
fi

"$NINJA" -C "$OUT_DIR" -j"$JOBS" \
  minicronet:minicronet \
  minicronet:minicronet_smoke \
  minicronet:minicronet_websocket_smoke \
  minicronet:minicronet_abi_cpp_smoke \
  minicronet:profile_probe

"$ROOT_DIR/tools/audit-core-macos.sh" "$OUT_DIR" x86_64
printf 'macOS x86_64 cross build and audit passed: %s\n' "$OUT_DIR"
