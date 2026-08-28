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
find "$OUT_DIR" -maxdepth 1 -type f -name '*.whl' -delete

(
  cd "$ROOT_DIR/bindings/python"
  MINICRONET_REQUIRE_NATIVE=1 \
    maturin build --release --auditwheel skip --out "$RAW_DIR"
)

LD_LIBRARY_PATH="$CORE_DIR${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" \
  auditwheel repair --wheel-dir "$OUT_DIR" "$RAW_DIR"/*.whl

echo "Wheel written to: $OUT_DIR"
