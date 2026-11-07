#!/usr/bin/env bash
set -Eeuo pipefail

# Builds and RUNS the profile verification probes.
#
# profile_feature_probe.cc asserts per-release facts the table alone cannot
# prove: that chrome_92 has no HTTP/3 behaviour and chrome_93 does, that ALPS
# starts at 122 and kTryQuicByDefault at 123, and so on. Those assertions only
# exist when minicronet_profile_verification = true, and every build script
# gates that behind PROFILE_VERIFICATION=1 -- so on a default build the probe
# compiled to nothing and every one of its assertions had never run. A closure
# of "the table is right" that is never executed is not a closure.
#
# This gate turns the opt-in into a separate, explicit step with its own exit
# status. It uses its own out directory so it cannot perturb the artifacts the
# release builds produce.
#
#   tools/audit-profile-probes.sh
#   OUT_DIR=/path/to/out tools/audit-profile-probes.sh

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
source "$ROOT_DIR/tools/core-paths.sh"
require_rg
export CHROMIUM_SRC=$(resolve_chromium_src)
# A dedicated out directory, never the release one. minicronet_profile_verification
# changes a define on the minicronet target, so pointing this at a release out
# directory forces a full rebuild of that directory and then leaves its args.gn
# different from what the release artifacts were built with -- which is exactly
# the kind of silent divergence tools/audit-core-binaries.sh exists to catch.
# The cost is one full build of this directory the first time; it is incremental
# afterwards.
OUT_DIR=${OUT_DIR:-$CHROMIUM_SRC/out/MiniCronet-profile-probes}
GN=$CHROMIUM_SRC/buildtools/linux64/gn
NINJA=$CHROMIUM_SRC/third_party/ninja/ninja

die() { printf "audit-profile-probes: %s\n" "$*" >&2; exit 1; }

[[ -d "$CHROMIUM_SRC/net" ]] || die "no Chromium checkout at $CHROMIUM_SRC; set CHROMIUM_SRC"
[[ -x "$GN" ]] || die "missing $GN"
[[ -x "$NINJA" ]] || die "missing $NINJA"

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
target_cpu = "x64"
target_os = "linux"
use_remoteexec = false
use_reclient = false
use_siso = false
icu_use_data_file = false
is_cfi = false
minicronet_profile_verification = true
EOF

cd "$CHROMIUM_SRC"
"$GN" gen "$OUT_DIR" --fail-on-unused-args --root-target=//minicronet "--root-pattern=//minicronet:*"

targets=(
  minicronet:profile_feature_probe
  minicronet:profile_isolation_probe
  minicronet:profile_state_isolation_probe
)
"$NINJA" -C "$OUT_DIR" -j"${JOBS:-2}" "${targets[@]}"

# Run them. Each probe exits 0 only when its assertions hold, and each prints
# what it checked, so a failure names the boundary that moved.
for probe in profile_feature_probe profile_isolation_probe; do
  printf "== %s\n" "$probe"
  LD_LIBRARY_PATH="$OUT_DIR${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" "$OUT_DIR/$probe"
done

# profile_state_isolation_probe is the one probe that cannot run on its own: it
# drives /cache, /etag, /cookie-redirect, /connection/* and /tls-close/* against a
# purpose-built HTTP and HTTPS harness, asserting cache, cookie, connection and
# TLS-session isolation between two engines. That harness is not part of this
# repository, so the probe is a documented long-term skip rather than a silent
# one: pass HTTP_BASE and HTTPS_BASE pointing at the harness to run it.
if [[ -n ${HTTP_BASE:-} && -n ${HTTPS_BASE:-} ]]; then
  printf "== profile_state_isolation_probe\n"
  LD_LIBRARY_PATH="$OUT_DIR${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" \
    "$OUT_DIR/profile_state_isolation_probe" "$HTTP_BASE" "$HTTPS_BASE"
  state_note="state isolation"
else
  printf "SKIP profile_state_isolation_probe: needs HTTP_BASE and HTTPS_BASE\n"
  printf "     the cache/cookie/connection/TLS-session harness is not part of\n"
  printf "     this repository; see docs/NEXT_STEPS.md\n"
  state_note="state isolation SKIPPED (no HTTP_BASE/HTTPS_BASE)"
fi

printf "profile probes passed: feature boundaries, profile isolation; %s\n" "$state_note"
