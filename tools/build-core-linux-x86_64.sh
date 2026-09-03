#!/usr/bin/env bash
set -Eeuo pipefail

# Rebuilds the Linux x86_64 Core from this repository's Core sources and the
# pinned Chromium checkout, then strips and audits the result.
#
# JOBS defaults to 2 because the ThinLTO link step already uses
# `--thinlto-jobs=all`; raising it on a 10 GB machine drives the linker into
# swap. Override deliberately, not casually.

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
CHROMIUM_SRC=${CHROMIUM_SRC:-/home/sj/chromium/src}
OUT_DIR=${OUT_DIR:-$CHROMIUM_SRC/out/MiniCronet-linux-x86_64}
JOBS=${JOBS:-2}
NINJA=$CHROMIUM_SRC/third_party/ninja/ninja
GN=$CHROMIUM_SRC/buildtools/linux64/gn
OBJCOPY=$CHROMIUM_SRC/third_party/llvm-build/Release+Asserts/bin/llvm-objcopy

die() { printf 'build-core: %s\n' "$*" >&2; exit 1; }

# Optional local pkgconf, kept outside this repository.
if [[ -n ${MINICRONET_PKGCONF_DIR:-} && -x "$MINICRONET_PKGCONF_DIR/usr/bin/pkg-config" ]]; then
  export PATH="$MINICRONET_PKGCONF_DIR/usr/bin:$PATH"
  export LD_LIBRARY_PATH="$MINICRONET_PKGCONF_DIR/usr/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi

for tool in "$NINJA" "$GN" "$OBJCOPY"; do
  test -x "$tool" || die "missing build tool $tool"
done

# The profile table is a committed generated header. Regeneration needs the
# profile evidence, which this repository does not own yet, so it is opt-in.
if [[ ${REGENERATE_PROFILE_TABLE:-0} == 1 ]]; then
  test -n "${PROFILE_EVIDENCE_DIR:-}" \
    || die "REGENERATE_PROFILE_TABLE=1 needs PROFILE_EVIDENCE_DIR"
  python3 "$ROOT_DIR/tools/generate-profile-table.py"
fi

"$ROOT_DIR/tools/sync-core.sh"

mkdir -p "$OUT_DIR"
cat >"$OUT_DIR/args.gn" <<'EOF'
is_component_build = false
is_debug = false
is_official_build = true
is_minicronet_build = true
optimize_for_size = true
symbol_level = 0
chrome_pgo_phase = 0
target_cpu = "x64"
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
if [[ ${DISABLE_OPTIONAL_TRACE_EVENTS:-0} == 1 ]]; then
  printf '%s\n' 'optional_trace_events_enabled = false' >>"$OUT_DIR/args.gn"
fi
if [[ ${PROFILE_VERIFICATION:-0} == 1 ]]; then
  printf '%s\n' 'minicronet_profile_verification = true' >>"$OUT_DIR/args.gn"
fi

cd "$CHROMIUM_SRC"
"$GN" gen "$OUT_DIR" --fail-on-unused-args \
  --root-target=//minicronet '--root-pattern=//minicronet:*'

# The desktop Core must never pull in the Android/Java/JNI graph.
if grep -rlE '(components/cronet/android|obj/base/android/|\.java( |$)|\.jar( |$)|jni_headers|/jni/)' \
  "$OUT_DIR" --include='*.ninja' >/dev/null 2>&1; then
  die 'Android/Java/JNI dependency leaked into the desktop Core'
fi

targets=(minicronet:minicronet minicronet:minicronet_smoke
         minicronet:minicronet_websocket_smoke minicronet:profile_probe
         minicronet:minicronet_abi_cpp_smoke)
if [[ ${PROFILE_VERIFICATION:-0} == 1 ]]; then
  targets+=(minicronet:profile_isolation_probe minicronet:profile_feature_probe
            minicronet:profile_state_isolation_probe)
fi
"$NINJA" -C "$OUT_DIR" -j"$JOBS" "${targets[@]}"

# Strip after linking. Removing .note.gnu.build-id is what makes the artifact
# byte-reproducible: the build id hashes the unstripped image.
"$OBJCOPY" --strip-unneeded \
  --remove-section=.comment \
  --remove-section=.note.gnu.build-id \
  "$OUT_DIR/libminicronet.so"

if [[ ${SKIP_SMOKE:-0} != 1 ]]; then
  LD_LIBRARY_PATH="$OUT_DIR${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" \
    "$OUT_DIR/minicronet_smoke"
  LD_LIBRARY_PATH="$OUT_DIR${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" \
    python3 "$ROOT_DIR/tools/run-http-smoke.py" "$OUT_DIR/minicronet_smoke"
fi

"$ROOT_DIR/tools/audit-core-linux.sh" "$OUT_DIR"

printf 'Linux x86_64 build and audit passed\n'
printf '  %s\n' "$OUT_DIR/libminicronet.so"
printf '  size   %s bytes\n' "$(stat -c%s "$OUT_DIR/libminicronet.so")"
printf '  sha256 %s\n' "$(sha256sum "$OUT_DIR/libminicronet.so" | cut -d' ' -f1)"
