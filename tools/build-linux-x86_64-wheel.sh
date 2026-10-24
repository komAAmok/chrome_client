#!/usr/bin/env bash
set -euo pipefail

# Build the current Core/PyO3 sources, then let auditwheel vendor the private
# Core and NSS/NSPR libraries into chrome_client.libs. Chromium is not rebuilt.
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CORE_DIR="$ROOT_DIR/core/binaries/linux-x86_64"
RAW_DIR="$(mktemp -d "${TMPDIR:-/tmp}/chrome-client-wheel.XXXXXX")"
OUT_DIR="${WHEEL_OUT_DIR:-$ROOT_DIR/dist/linux-x86_64-prototype}"
trap 'rm -rf "$RAW_DIR"' EXIT

command -v maturin >/dev/null || { echo "maturin is required" >&2; exit 1; }
command -v auditwheel >/dev/null || { echo "auditwheel is required" >&2; exit 1; }
command -v patchelf >/dev/null || { echo "patchelf is required" >&2; exit 1; }

test -f "$CORE_DIR/libminicronet.so" || {
  echo "missing Linux x86_64 Core: $CORE_DIR/libminicronet.so" >&2
  exit 1
}

mkdir -p "$OUT_DIR"

(
  cd "$ROOT_DIR/bindings/python"
  MINICRONET_REQUIRE_NATIVE=1 \
    maturin build --release --auditwheel skip --out "$RAW_DIR"
)

# The raw wheel must exist before the output directory is touched. Deleting the
# stale wheels first meant a failed maturin run (a compile error, a missing
# toolchain) destroyed the last good wheel and left nothing in its place: the
# build reported failure, and the previous artifact was gone too. maturin already
# writes into the private mktemp directory, so by here the new wheel is known to
# exist and only now is the old one replaced.
shopt -s nullglob
raw_wheels=("$RAW_DIR"/*.whl)
shopt -u nullglob
(( ${#raw_wheels[@]} == 1 )) \
  || { echo "maturin produced ${#raw_wheels[@]} wheels in $RAW_DIR, expected exactly 1" >&2; exit 1; }

find "$OUT_DIR" -maxdepth 1 -type f -name '*.whl' -delete

LD_LIBRARY_PATH="$CORE_DIR${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" \
  auditwheel repair --wheel-dir "$OUT_DIR" "${raw_wheels[0]}"

# auditwheel can succeed and still emit nothing (an unsupported platform, a
# skipped repair); the previous script printed the directory either way.
shopt -s nullglob
built=("$OUT_DIR"/*.whl)
shopt -u nullglob
(( ${#built[@]} > 0 )) \
  || { echo "auditwheel wrote no wheel into $OUT_DIR" >&2; exit 1; }

printf 'Wheel written to: %s\n' "${built[*]}"
