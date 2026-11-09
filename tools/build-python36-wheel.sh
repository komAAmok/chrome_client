#!/usr/bin/env bash
# Builds the bindings/python36 wheel (cp36-abi3) and then runs the artifact gate.
#
# Why a script instead of a line in the README: the wheel is the only thing this
# directory produces, it is built by a different toolchain than the rest of the
# repo (maturin + a pyo3 0.15 crate, because abi3-py36 needs the older pyo3), and
# nothing verified the result. tools/audit-python36-artifact.py now checks the
# artifact; this is the reproducible way to produce one to check.
#
# Usage:
#   tools/build-python36-wheel.sh [core-target]      # default linux-x86_64
#
# The Core directory is passed to the build as MINICRONET_CORE_DIR and put on the
# loader path afterwards, matching how the facade's _native.py unpacks a source
# checkout.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="${1:-linux-x86_64}"
CORE="$ROOT/core/binaries/$TARGET"

[[ -d "$CORE" ]] || {
  printf 'build-python36-wheel: no Core at %s\n' "$CORE" >&2
  printf 'Pick one of: %s\n' "$(cd "$ROOT/core/binaries" && ls -d */ | tr -d / | tr '\n' ' ')" >&2
  exit 1
}

command -v cargo >/dev/null || {
  printf 'build-python36-wheel: cargo not on PATH; install a Rust toolchain\n' >&2
  exit 1
}

# maturin builds the wheel; run it as a module so a venv-less install still works.
MATURIN=(python3 -m maturin)
"$MATURIN" --version >/dev/null 2>&1 || {
  printf 'build-python36-wheel: maturin not installed (pip install maturin)\n' >&2
  exit 1
}

printf 'build-python36-wheel: Core %s, output bindings/python36/dist\n' "$TARGET"
cd "$ROOT/bindings/python36"
MINICRONET_CORE_DIR="$CORE" "${MATURIN[@]}" build --release --out dist

WHEEL="$(ls -1t "$ROOT/bindings/python36/dist"/*.whl | head -1)"
printf 'build-python36-wheel: produced %s\n' "$(basename "$WHEEL")"

# The tag is a claim, not a detail: a cp39 wheel would silently shadow the other
# one on 3.9+, and a py3-none-any wheel would claim to be pure Python.
case "$(basename "$WHEEL")" in
  *-cp36-abi3-*) printf 'build-python36-wheel: tag cp36-abi3 ok\n' ;;
  *) printf 'build-python36-wheel: unexpected wheel tag in %s\n' "$(basename "$WHEEL")" >&2; exit 1 ;;
esac

printf 'build-python36-wheel: running the artifact gate\n'
cd "$ROOT"
LD_LIBRARY_PATH="$CORE${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" \
  python3 tools/audit-python36-artifact.py
