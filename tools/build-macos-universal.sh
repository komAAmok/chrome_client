#!/usr/bin/env bash
set -Eeuo pipefail

# Builds a universal2 macOS wheel on a Linux host, without Xcode.
#
# Chromium is cross-built for macOS one architecture at a time, so
# core/binaries/ holds macos-x86_64/libminicronet.dylib and
# macos-arm64/libminicronet.dylib and no universal one. rustc has no
# universal2 target, so the extension module is built twice and fused by
# tools/lipo-dylib.py.
#
# Two things this script has to get right, both found by trying:
#
#   1. osxcross invokes `clang` from PATH, not its own bundled compiler, and
#      that wrapper needs a clang new enough to accept -mmacosx-version-min.
#      Chromium's own llvm-build is; the host cc is not. Without it the
#      wrapper dies with "your clang installation is outdated" and then
#      "invoking compiler failed", which reads like a missing SDK.
#   2. A cdylib leaves the CPython symbols undefined on purpose -- the
#      interpreter provides them at import time. ELF allows that in a shared
#      object; Mach-O needs -undefined dynamic_lookup, or the link fails on
#      PyExc_RuntimeError and twenty more symbols.
#
# delocate is deliberately absent: it shells out to codesign, which exists
# only on macOS. This script produces the fused wheel; the Core dylib is
# bundled by the macOS runner (or a Mac) afterwards.
#
#   tools/build-macos-universal.sh
#   BINDING=python36 tools/build-macos-universal.sh

ROOT_DIR=$(cd "$(dirname "$0")/.." && pwd)
source "$(dirname "$0")/core-paths.sh"
export CHROMIUM_SRC=$(resolve_chromium_src)
TOOLCHAIN=$(resolve_chromium_toolchain)
if [ -z "${OSXCROSS_TARGET:-}" ]; then
  OSXCROSS_TARGET=$TOOLCHAIN/toolchains/osxcross-target
fi
CLANG_DIR=$CHROMIUM_SRC/third_party/llvm-build/Release+Asserts/bin
if [ -z "${BINDING:-}" ]; then BINDING=python; fi
if [ -z "${OUT_DIR:-}" ]; then OUT_DIR=$ROOT_DIR/dist; fi

test -x "$CLANG_DIR/clang" || { printf "missing %s/clang\n" "$CLANG_DIR" >&2; exit 1; }
test -d "$OSXCROSS_TARGET/SDK" || { printf "missing osxcross SDK under %s\n" "$OSXCROSS_TARGET" >&2; exit 1; }

case $BINDING in
  python) MANIFEST=$ROOT_DIR/bindings/python/Cargo.toml ;;
  python36) MANIFEST=$ROOT_DIR/bindings/python36/Cargo.toml ;;
  *) printf "unknown BINDING %s\n" "$BINDING" >&2; exit 1 ;;
esac

# osxcross wraps whichever clang it finds first; the Chromium one is new
# enough and the host one is not.
export PATH="$CLANG_DIR:$OSXCROSS_TARGET/bin:$PATH"
# -undefined dynamic_lookup lets the cdylib leave the CPython symbols for
# the interpreter to provide at import time.
export RUSTFLAGS="${RUSTFLAGS:-} -C link-arg=-Wl,-undefined -C link-arg=-Wl,dynamic_lookup"

wheel_dir=$(mktemp -d)
trap 'rm -rf "$wheel_dir"' EXIT

for triple in x86_64-apple-darwin aarch64-apple-darwin; do
  case $triple in
    x86_64-*) core_target=macos-x86_64 ;;
    aarch64-*) core_target=macos-arm64 ;;
  esac
  # env var names cargo reads are the triple with dashes mapped to
  # underscores and upper-cased.
  flat=$(printf %s "$triple" | tr - _ | tr a-z A-Z)
  # osxcross names its wrappers after the SDK triple it was built with, so the
  # bare triple is not enough: -clang alone does not exist. Take the newest
  # wrapper for this architecture rather than pinning a version here.
  tool=$(compgen -c | grep -xE "$triple[0-9.]+-clang" | sort -V | tail -1)
  if [ -z "$tool" ]; then
    printf 'no osxcross clang wrapper for %s under %s\n' "$triple" "$OSXCROSS_TARGET/bin" >&2
    exit 1
  fi
  export "CC_$flat=$tool"
  export "AR_$flat=${tool%-clang}-ar"
  export "CARGO_TARGET_${flat}_LINKER=$tool"
  printf "== %s (%s)\n" "$triple" "$core_target"
  MINICRONET_CORE_DIR=$ROOT_DIR/core/binaries/$core_target \
    maturin build --release --locked --manifest-path "$MANIFEST" \
      --target "$triple" --out "$wheel_dir"
done

mkdir -p "$OUT_DIR"
# The Core dylibs travel into the repaired wheel, so their minimum macOS
# version bounds the tag too. Today they say 13.0 while the extension says
# 10.12/11.0, and a 11.0 tag would install on macOS 12 where the Core cannot be
# mapped.
python3 "$ROOT_DIR/tools/fuse-macos-wheels.py" --out "$OUT_DIR" \
  --bundled core/binaries/macos-x86_64/libminicronet.dylib \
            core/binaries/macos-arm64/libminicronet.dylib \
  -- "$wheel_dir"/*.whl
printf "\nnext, on macOS: DYLD_LIBRARY_PATH=<fused core dir> delocate-wheel -w repaired %s/*.whl\n" "$OUT_DIR"
printf "then: python3 tools/audit-macos-wheel.py %s/*.whl\n" "$OUT_DIR"
