#!/usr/bin/env bash
set -euo pipefail

# Audit only the staged Core artifacts. This deliberately does not inspect a
# Chromium checkout or a GN/Ninja output directory.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BINARIES="$ROOT/core/binaries"
HEADER="$ROOT/core/abi/minicronet.h"

targets=(
  linux-x86 linux-x86_64 linux-arm64
  windows-x86 windows-x86_64 windows-arm64
  macos-x86_64 macos-arm64
)
symbols=(
  mn_abi_version mn_version_string
  mn_engine_create mn_engine_retain mn_engine_release
  mn_request_create mn_request_retain mn_request_release
  mn_request_start mn_request_cancel mn_request_upload_write
  mn_request_follow_redirect mn_request_resume_read
  mn_websocket_create mn_websocket_retain mn_websocket_release
  mn_websocket_start mn_websocket_send mn_websocket_close
  mn_websocket_cancel
)

die() { printf 'audit: %s\n' "$*" >&2; exit 1; }
command -v sha256sum >/dev/null || die "sha256sum is required"
command -v file >/dev/null || die "file is required"

grep -Eq '^#define MN_ABI_VERSION 8u$' "$HEADER" \
  || die "header ABI version is not 8"

for target in "${targets[@]}"; do
  dir="$BINARIES/$target"
  manifest="$dir/manifest.json"
  [[ -f "$manifest" ]] || die "$target: missing manifest.json"

  # Parse JSON with the standard library, avoiding a jq dependency.
  python3 - "$manifest" "$target" <<'PY'
import json, pathlib, sys
path, expected_target = sys.argv[1:]
data = json.loads(pathlib.Path(path).read_text())
required = ("project", "core", "abi_version", "chromium_revision",
            "target", "triple", "library", "sha256",
            "size_bytes", "runtime_dependencies")
missing = [key for key in required if key not in data]
if missing:
    raise SystemExit(f"missing fields: {', '.join(missing)}")
if data["project"] != "chrome_client" or data["core"] != "minicronet":
    raise SystemExit("unexpected project/core")
if data["abi_version"] != 8 or data["target"] != expected_target:
    raise SystemExit("ABI or target mismatch")
if not isinstance(data["runtime_dependencies"], list):
    raise SystemExit("runtime_dependencies must be an array")
PY

  library="$(python3 - "$manifest" <<'PY'
import json, sys
print(json.load(open(sys.argv[1]))["library"])
PY
)"
  path="$dir/$library"
  [[ -f "$path" ]] || die "$target: missing $library"
  expected_sha="$(python3 - "$manifest" <<'PY'
import json, sys
print(json.load(open(sys.argv[1]))["sha256"])
PY
)"
  actual_sha="$(sha256sum "$path" | awk '{print $1}')"
  [[ "$actual_sha" == "$expected_sha" ]] \
    || die "$target: SHA-256 mismatch for $library"
  expected_size="$(python3 - "$manifest" <<'PY'
import json, sys
print(json.load(open(sys.argv[1]))["size_bytes"])
PY
)"
  actual_size="$(wc -c < "$path" | tr -d ' ')"
  [[ "$actual_size" == "$expected_size" ]] \
    || die "$target: size mismatch for $library"

  description="$(file -b "$path")"
  case "$target" in
    linux-x86) [[ "$description" == *"ELF 32-bit"* && "$description" == *"Intel 80386"* ]] || die "$target: wrong architecture ($description)" ;;
    linux-x86_64) [[ "$description" == *"ELF 64-bit"* && "$description" == *"x86-64"* ]] || die "$target: wrong architecture ($description)" ;;
    linux-arm64) [[ "$description" == *"ELF 64-bit"* && "$description" == *"ARM aarch64"* ]] || die "$target: wrong architecture ($description)" ;;
    windows-x86) [[ "$description" == *"PE32 executable"* && "$description" == *"Intel 80386"* ]] || die "$target: wrong architecture ($description)" ;;
    windows-x86_64) [[ "$description" == *"PE32+ executable"* && "$description" == *"x86-64"* ]] || die "$target: wrong architecture ($description)" ;;
    windows-arm64) [[ "$description" == *"PE32+ executable"* && "$description" == *"Aarch64"* ]] || die "$target: wrong architecture ($description)" ;;
    macos-x86_64) [[ "$description" == *"Mach-O 64-bit x86_64"* ]] || die "$target: wrong architecture ($description)" ;;
    macos-arm64) [[ "$description" == *"Mach-O 64-bit arm64"* ]] || die "$target: wrong architecture ($description)" ;;
  esac

  if [[ "$target" == linux-* ]] && command -v readelf >/dev/null; then
    readelf -d "$path" | grep -q 'SONAME.*libminicronet\.so' \
      || die "$target: missing libminicronet.so SONAME"
  fi

  if [[ "$target" == windows-* ]]; then
    import_library="$dir/minicronet.lib"
    [[ -f "$import_library" ]] || die "$target: missing minicronet.lib"
    import_sha="$(sha256sum "$import_library" | awk '{print $1}')"
    expected_import_sha="$(python3 - "$manifest" <<'PY'
import json, sys
data = json.load(open(sys.argv[1]))
print(data.get("import_library_sha256", ""))
PY
)"
    [[ -n "$expected_import_sha" && "$import_sha" == "$expected_import_sha" ]] \
      || die "$target: import library SHA-256 mismatch"
    # ABI v8 links the IDNA-only ICU dataset into the library, so no external
    # icudtl.dat may travel with a Windows artifact any more.
    [[ ! -e "$ROOT/core/dependencies/$target/icudtl.dat" ]] \
      || die "$target: stale core/dependencies/$target/icudtl.dat; ICU data is embedded"
  fi

  # Linux nm is available on the build host. For foreign formats use llvm-nm
  # when installed; otherwise strings still catches an accidentally wrong ABI
  # export set without pretending to validate a foreign linker table.
  symbol_dump=""
  if [[ "$target" == linux-* ]] && command -v nm >/dev/null; then
    symbol_dump="$(nm -D --defined-only "$path" 2>/dev/null || true)"
  elif command -v llvm-nm >/dev/null; then
    symbol_dump="$(llvm-nm --defined-only "$path" 2>/dev/null || true)"
  else
    symbol_dump="$(strings "$path")"
  fi
  for symbol in "${symbols[@]}"; do
    grep -Eq "(^|[^[:alnum:]_])_?${symbol}(@|$|[^[:alnum:]_])" <<<"$symbol_dump" \
      || die "$target: missing public symbol $symbol"
  done
  printf 'OK %-16s %s (%s bytes)\n' "$target" "$library" "$actual_size"
done

printf 'Core binary audit passed for %d targets.\n' "${#targets[@]}"
