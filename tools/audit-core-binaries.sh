#!/usr/bin/env bash
set -euo pipefail

# Audit only the staged Core artifacts. This deliberately does not inspect a
# Chromium checkout or a GN/Ninja output directory.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BINARIES="$ROOT/core/binaries"
HEADER="$ROOT/core/abi/minicronet.h"
# The per-target triple and library-name catalog. It restates facts that also
# live in each manifest, so it needs its own gate or the two drift.
TARGETS_JSON="$BINARIES/targets.json"

targets=(
  linux-x86 linux-x86_64 linux-armv7 linux-arm64
  windows-x86 windows-x86_64 windows-arm64
  macos-x86_64 macos-arm64
)
symbols=(
  mn_abi_version mn_version_string
  mn_engine_create mn_engine_retain mn_engine_release
  mn_request_create mn_request_retain mn_request_release
  mn_request_start mn_request_cancel mn_request_upload_write
  mn_request_follow_redirect mn_request_resume_read mn_request_release_buffer
  mn_request_negotiated_protocol mn_request_ech_status
  mn_engine_cookies_get mn_engine_cookies_set mn_engine_cookies_delete
  mn_engine_cookies_clear mn_engine_set_client_certificate
  mn_engine_clear_client_certificate
  mn_websocket_create mn_websocket_retain mn_websocket_release
  mn_websocket_start mn_websocket_send mn_websocket_close
  mn_websocket_cancel
)

# The first and last profile the compiled table carries. Derived from the
# generated header rather than written here, so adding a profile cannot leave
# this audit asserting an old range.
profile_table="$ROOT/core/source/minicronet/profile_table_generated.h"
[[ -f "$profile_table" ]] \
  || die "missing $profile_table; the compiled profile table is the source of truth for the supported range"
mapfile -t profile_ids < <(grep -oE '"chrome_[0-9]+"' "$profile_table" | tr -d '"')
[[ ${#profile_ids[@]} -gt 0 ]] \
  || die "no profile ids in $profile_table"
profile_span=("${profile_ids[0]}" "${profile_ids[-1]}")

die() { printf 'audit: %s\n' "$*" >&2; exit 1; }

tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT
tmp_expected_exports="$tmpdir/expected_exports"
tmp_actual_exports="$tmpdir/actual_exports"
command -v sha256sum >/dev/null || die "sha256sum is required"
command -v file >/dev/null || die "file is required"

# The one source of truth for the release version (root Cargo.toml).
workspace_version="$(python3 "$ROOT/tools/version.py")" \
  || die "cannot read the workspace version from tools/version.py"
[[ -n "$workspace_version" ]] || die "empty workspace version"

# The expected ABI is read from the header, never written here: a release that
# bumps MN_ABI_VERSION must not also have to edit this gate, and a gate holding
# its own copy is how the two would drift apart.
expected_abi="$(sed -n 's/^#define MN_ABI_VERSION \([0-9]\+\)u$/\1/p' "$HEADER")"
[[ -n "$expected_abi" ]] \
  || die "header has no '#define MN_ABI_VERSION <n>u'"
export EXPECTED_ABI="$expected_abi"

[[ -f "$TARGETS_JSON" ]] \
  || die "missing core/binaries/targets.json; the per-target catalog is gone"

# The Mach-O walker is the only thing that reads Apple's export trie, and it
# used to drop any node that was both an export and the prefix of a longer one:
# on libminicronet.dylib "release" prefixes "release_buffer", and the gate
# reported 28 exports where the image held 29. Its verdict is worthless until
# the walker itself is checked, so the self-test runs before any artifact is
# trusted.
python3 "$ROOT/tools/core-exports.py" --self-test \
  || die "core-exports self-test failed; the export walker cannot be trusted"

for target in "${targets[@]}"; do
  dir="$BINARIES/$target"
  manifest="$dir/manifest.json"
  [[ -f "$manifest" ]] || die "$target: missing manifest.json"

  # Parse JSON with the standard library, avoiding a jq dependency.
  # CHROMIUM_REVISION is the pinned tree every artifact must have been built
  # from, and the manifest is the only record of which tree that was. The field
  # was required to *exist* but never compared, so a manifest naming any other
  # 40-hex commit passed: the audit then certified an artifact against a tree this
  # repository does not build against, which is exactly the drift the size
  # ceilings and the version marker exist to catch.
  pinned_revision=$(tr -d '[:space:]' <"$ROOT/CHROMIUM_REVISION")
  [[ "$pinned_revision" =~ ^[0-9a-f]{40}$ ]] \
    || die "CHROMIUM_REVISION is not a 40-character commit hash"
  # core/binaries/targets.json states the same two facts per target that the
  # manifest does -- the triple and the library filename -- and it is the file
  # build.rs reads to pick an artifact. Nothing compared the two, so a manifest
  # could name i686 while sitting in linux-x86_64/ and this audit stayed green;
  # the artifact was then certified against a triple the repository does not
  # use for that directory.
  python3 - "$manifest" "$target" "$pinned_revision" "$TARGETS_JSON" <<'PY'
import json, pathlib, sys
path, expected_target, pinned_revision, targets_json = sys.argv[1:]
data = json.loads(pathlib.Path(path).read_text())
required = ("project", "core", "abi_version", "chromium_revision",
            "target", "triple", "library", "sha256",
            "size_bytes", "runtime_dependencies")
missing = [key for key in required if key not in data]
if missing:
    raise SystemExit(f"missing fields: {', '.join(missing)}")
if data["project"] != "chrome_client" or data["core"] != "minicronet":
    raise SystemExit("unexpected project/core")
import os
EXPECTED_ABI = int(os.environ["EXPECTED_ABI"])
if data["target"] != expected_target:
    raise SystemExit("target mismatch")
# A target whose build inputs are unavailable (the macOS osxcross toolchain is
# incomplete on this host) keeps shipping its previous artifact. Its manifest
# must then state the ABI that artifact really is, not the one this release is
# aiming at: a manifest that claims a version its bytes do not implement is
# worse than a mixed tree, because the mismatch is invisible at load time.
permitted = {EXPECTED_ABI, EXPECTED_ABI - 1}
if data["abi_version"] not in permitted:
    raise SystemExit(
        f"abi_version {data['abi_version']} is neither {EXPECTED_ABI} nor the "
        f"previous {EXPECTED_ABI - 1}")
if data["abi_version"] != EXPECTED_ABI:
    print(f"note: {expected_target} still ships ABI {data['abi_version']}"
          f" (not rebuilt for {EXPECTED_ABI})", file=sys.stderr)
if data["chromium_revision"] != pinned_revision:
    raise SystemExit(
        f"chromium_revision is {data['chromium_revision']} but CHROMIUM_REVISION "
        f"pins {pinned_revision}; this artifact was built from a different tree "
        "than the one this repository builds against")
if not isinstance(data["runtime_dependencies"], list):
    raise SystemExit("runtime_dependencies must be an array")
catalog = json.loads(pathlib.Path(targets_json).read_text())
EXPECTED_ABI = int(os.environ["EXPECTED_ABI"])
if catalog.get("abi_version") != EXPECTED_ABI:
    raise SystemExit(
        f"targets.json abi_version is {catalog.get('abi_version')}, not {EXPECTED_ABI}; it "
        "describes a different ABI than the one this audit certifies")
entry = catalog.get("targets", {}).get(expected_target)
if entry is None:
    raise SystemExit(f"targets.json has no {expected_target} entry")
if entry.get("triple") != data["triple"]:
    raise SystemExit(
        f"triple is {data['triple']} in the manifest but {entry.get('triple')} "
        f"in targets.json for {expected_target}")
if entry.get("library") != data["library"]:
    raise SystemExit(
        f"library is {data['library']} in the manifest but {entry.get('library')} "
        f"in targets.json for {expected_target}")
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
    # The class token is part of the claim: an AArch64 artifact also says
    # "ARM", so EABI5 plus ELF 32-bit is what keeps the two apart.
    linux-armv7) [[ "$description" == *"ELF 32-bit"* && "$description" == *"ARM"* && "$description" == *"EABI5"* ]] || die "$target: wrong architecture ($description)" ;;
    linux-arm64) [[ "$description" == *"ELF 64-bit"* && "$description" == *"ARM aarch64"* ]] || die "$target: wrong architecture ($description)" ;;
    windows-x86) [[ "$description" == *"PE32 executable"* && "$description" == *"Intel 80386"* ]] || die "$target: wrong architecture ($description)" ;;
    windows-x86_64) [[ "$description" == *"PE32+ executable"* && "$description" == *"x86-64"* ]] || die "$target: wrong architecture ($description)" ;;
    windows-arm64) [[ "$description" == *"PE32+ executable"* && "$description" == *"Aarch64"* ]] || die "$target: wrong architecture ($description)" ;;
    macos-x86_64) [[ "$description" == *"Mach-O 64-bit x86_64"* ]] || die "$target: wrong architecture ($description)" ;;
    macos-arm64) [[ "$description" == *"Mach-O 64-bit arm64"* ]] || die "$target: wrong architecture ($description)" ;;
  esac

  # readelf is required, not merely used when present: `&& command -v readelf`
  # made the SONAME check vanish on a machine without binutils, so an artifact
  # with a wrong or missing SONAME passed on that machine and failed in CI. The
  # command is already a hard requirement of the top of this script (sha256sum,
  # file); this one was the only conditional.
  if [[ "$target" == linux-* ]]; then
    command -v readelf >/dev/null \
      || die "readelf is required to check the SONAME of the Linux artifacts"
    readelf -d "$path" | grep -q 'SONAME.*libminicronet\.so' \
      || die "$target: missing libminicronet.so SONAME"
  fi

  # HTTP Basic and Digest decode a challenge's realm with
  # base::ConvertToUtf8AndNormalize(..., kCharsetLatin1, ...), which resolves
  # "ISO-8859-1" to the windows-1252-html converter table. When the ICU dataset

  # Every artifact must have been built from the profile table this repository
  # currently ships. Nothing checked that before, and the result was a release
  # where one platform was rebuilt with chrome_86..chrome_98 and the other seven
  # still carried the older table: the manifests matched, the SHA-256s matched,
  # every other line of this audit was green, and impersonate="chrome_86"
  # raised ImpersonateError on seven of eight platforms while the Python facade
  # advertised the 86..157 range to all of them.
  #
  # The profile ids are string literals in the artifact, so the two ends of the
  # range are a direct witness to which table was compiled in. Both ends are
  # checked, because widening backwards (86) and forwards (157) fail
  # independently, and a stale artifact can miss either one. Size ceilings and
  # SHA-256 cannot see this: a rebuild with the old table is a perfectly
  # consistent artifact.
  for profile_id in "${profile_span[@]}"; do
    grep -aq "$profile_id" "$path" \
      || die "$target: artifact does not contain profile $profile_id; it was built from an older profile table than core/source/minicronet/profile_table_generated.h, so the range this build advertises differs from what it serves"
  done
  # left conversion_mappings out, ucnv_open failed, no auth handler was created,
  # and every realm-carrying 407/401 was silently dropped -- the credentials
  # were never sent and nothing was logged. The resource name is the one string
  # that only the packaged table contributes (cnvalias.icu alone yields the
  # bare alias, not the .cnv entry), so it is a direct check that the table
  # actually travelled into the artifact.
  grep -aq 'windows-1252-html\.cnv' "$path" \
    || die "$target: ICU ISO-8859-1 converter table is missing; HTTP Basic/Digest realms cannot be decoded"

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

  # Read the artifact's real export table. The obvious alternatives are each
  # wrong for at least one shipped format, which is why this is a parser rather
  # than a call to nm / llvm-nm / strings:
  #
  #   * llvm-nm answers "no symbols" for a PE image and still exits 0, so with
  #     LLVM on PATH every Windows target looked like it exported nothing and
  #     the audit failed on a good artifact. A PE export table lives in the
  #     image data directory, not in a COFF symbol table.
  #   * strings finds the export names as plain literals and keeps finding them
  #     after the export table is gone, so it cannot tell an export from a
  #     string. Zeroing the x86_64 DLL's export directory left all twenty names
  #     in .rdata and strings still reported 20/20.
  #   * binutils objdump reads x86/x86_64 PE but not the ARM64 image, and no
  #     Mach-O at all.
  #
  # tools/core-exports.py parses all three containers with the standard library,
  # so the check is identical in CI and locally and cannot silently degrade to a
  # weaker tool. An empty dump is a hard failure, never a pass.
  symbol_dump="$(python3 "$ROOT/tools/core-exports.py" "$path")" \
    || die "$target: cannot read the export table of $library"
  [[ -n "$symbol_dump" ]] \
    || die "$target: $library exports no symbols at all"

  for symbol in "${symbols[@]}"; do
    grep -Eq "^_?${symbol}$" <<<"$symbol_dump" \
      || die "$target: missing public symbol $symbol"
  done

  # The loop above is a superset check: it proves each v8 symbol is exported but
  # not that nothing else is. An artifact carrying a 21st mn_* export would load
  # a symbol this ABI does not define, and every check would still pass. Compare
  # the exported mn_* names as a set instead.
  printf '%s\n' "${symbols[@]}" | sort -u >"$tmp_expected_exports"
  grep -E '^_?mn_' <<<"$symbol_dump" | sed 's/^_//' | sort -u >"$tmp_actual_exports"
  cmp -s "$tmp_expected_exports" "$tmp_actual_exports" \
    || die "$target: exported mn_* set differs from ABI v$expected_abi ($(tr '\n' ' ' <"$tmp_actual_exports"))"

  # The version string is injected at build time from the workspace version
  # (tools/version.py -> tools/sync-core.sh -> MINICRONET_VERSION), so an
  # artifact built before a bump keeps reporting the old number forever:
  # chrome_client.core_version() said 0.4.0 for three releases because the
  # string was hand-written, and nothing checked it afterwards either. Every
  # artifact must carry the version this repository currently claims.
  #
  # The marker is the default User-Agent ("minicronet/<version>"), a literal in
  # minicronet.cc rather than a run-time concatenation, so it is present
  # verbatim in the image. Only the artifact's own bytes are searched, never
  # the manifest, so a stale manifest cannot satisfy the check.
  # The trailing '|| true' is load-bearing and belongs to this pipeline only.
  # Under 'set -e' a grep with no match makes the whole pipeline non-zero, so
  # the assignment itself aborted the script and the diagnostic below was
  # unreachable: a markerless artifact died with an empty stderr and no
  # explanation. Swallowing the status here is safe because the next line turns
  # an empty result into a hard failure; it must not be copied to a check that
  # has no such successor.
  artifact_version="$(strings -a "$path" \
    | grep -oE 'minicronet/[0-9]+\.[0-9]+\.[0-9]+' \
    | head -1 | cut -d/ -f2 || true)"
  [[ -n "$artifact_version" ]] \
    || die "$target: no minicronet/<version> marker in the artifact"
  [[ "$artifact_version" == "$workspace_version" ]] \
    || die "$target: artifact reports $artifact_version but the workspace version is $workspace_version; rebuild the Core after a version bump"
  printf 'OK %-16s %s (%s bytes, version %s)\n' "$target" "$library" "$actual_size" "$artifact_version"
done

printf 'Core binary audit passed for %d targets.\n' "${#targets[@]}"
