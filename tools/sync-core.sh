#!/usr/bin/env bash
set -Eeuo pipefail

# Applies the pinned Chromium patches and copies the chrome_client-owned Core
# sources into the Chromium checkout.
#
# Layout bridge: this repository keeps Core headers under
# `core/source/minicronet/`, the ABI header under `core/abi/` and the export
# tables under `core/exports/`. Chromium resolves `#include "minicronet/x.h"`
# from its own source root, so every file is flattened into
# `$CHROMIUM_SRC/minicronet/`.
#
# The profile-verification probes are owned here like every other Core source;
# only the profile evidence they read stays outside. tools/audit-core-inputs.sh
# asserts that every input this script installs or applies is tracked by Git.

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# The Chromium checkout defaults to a sibling of this repository, so this
# script works from any directory on any machine. Point CHROMIUM_SRC at your
# checkout if it lives elsewhere.
source "$(dirname "${BASH_SOURCE[0]}")/core-paths.sh"
CHROMIUM_SRC=$(resolve_chromium_src)
PATCH_DIR=$ROOT_DIR/core/patches

die() { printf 'sync-core: %s\n' "$*" >&2; exit 1; }

test -d "$CHROMIUM_SRC/net" \
  || die "no Chromium checkout at $CHROMIUM_SRC; set CHROMIUM_SRC to your Chromium checkout"

EXPECTED_REVISION=$(tr -d '[:space:]' <"$ROOT_DIR/CHROMIUM_REVISION")
ACTUAL_REVISION=$(git -C "$CHROMIUM_SRC" rev-parse HEAD)
[[ "$ACTUAL_REVISION" == "$EXPECTED_REVISION" ]] \
  || die "Chromium revision mismatch: expected $EXPECTED_REVISION, got $ACTUAL_REVISION"

# Nothing this script needs may live only in this machine's Chromium checkout:
# four profile-verification probes once did, so a sync here succeeded and the same
# sync died on a fresh clone. The check is cheap and runs before anything is
# written, so the failure names the missing input instead of a half-synced tree.
"$ROOT_DIR/tools/audit-core-inputs.sh"

# apply_patch <patch> <repo-subdir> [marker-file:marker-text ...]
# A patch that no longer applies must already be present; every marker is
# checked so a half-applied tree fails instead of building silently. Markers are
# passed as separate arguments because their text contains shell metacharacters.
apply_patch() {
  local name=$1 subdir=$2
  shift 2
  local patch_file=$PATCH_DIR/$name
  local repo=$CHROMIUM_SRC/$subdir
  test -f "$patch_file" || die "missing patch $name"
  if git -C "$repo" apply --check "$patch_file" 2>/dev/null; then
    git -C "$repo" apply "$patch_file"
    printf 'applied %s\n' "$name"
    return
  fi
  # The patch does not apply forward, so it is supposed to be applied already.
  # The marker list alone cannot prove that: it is a hand-maintained sample of the
  # patch's files and covered 6 of the 10 files in minicronet-core.patch and 3 of
  # 27 in profile-context-net.patch, so a hunk missing from any unlisted file left
  # every named marker present and this printed "verified". `git apply --reverse
  # --check` succeeds only when *every* hunk of the patch is present, so it is the
  # authority here; the markers stay as the diagnostic that names a wrong file.
  if ! git -C "$repo" apply --check --reverse "$patch_file" 2>/dev/null; then
    local marker file text
    for marker in "$@"; do
      file=${marker%%:*}
      text=${marker#*:}
      grep -Fq "$text" "$repo/$file" \
        || die "$name is partially applied: $file lacks '$text'"
    done
    # Every named marker is present, yet the patch neither applies forward nor
    # reverses: some hunk in a file the marker list does not name is missing (or
    # the tree was edited under the patch). Either way the tree cannot be trusted.
    # Single-quoted segments: backticks inside a double-quoted string would be
    # command-substituted by the shell and the paths would vanish from the message.
    die "$name neither applies forward nor is fully present: every marker below is in place, so the difference is in a file the marker list does not name. Rejected by 'git -C $repo apply --reverse --check $patch_file'; run 'git -C $repo apply --reverse $patch_file' to unwind what is there, then re-sync"
  fi
  printf 'verified %s\n' "$name"
}

apply_patch minicronet-core.patch . \
  'build/config/cronet/config.gni:is_minicronet_build = false' \
  'build_overrides/build.gni:is_cronet_build || is_minicronet_build' \
  'build/config/compiler/BUILD.gn:is_fuchsia || enable_cast_receiver || is_minicronet_build' \
  'build/config/compiler/BUILD.gn:/clang:-Oz' \
  'build/config/compiler/BUILD.gn:lto_opt_level = 1' \
  'net/features.gni:disable_file_support = is_cronet_build || is_minicronet_build' \
  'net/BUILD.gn:!is_cronet_build && !is_minicronet_build' \
  'base/BUILD.gn:nix/xdg_util_minicronet.cc'
# //net resends a request on a reused keep-alive connection whenever no response
# header has arrived (net/http/http_network_transaction.cc:ShouldResendRequest),
# without looking at the method. For a browser that is safe: the renderer owns
# the submit and the user can see it. A client library exposes URLRequest
# directly, so a POST whose reused connection died is silently sent twice, body
# included -- measured, and Session(retry=0) cannot prevent it because the
# resend is below the ABI. Idempotent methods keep the resend.
apply_patch no-resend-non-idempotent.patch . \
  'net/http/http_network_transaction.cc:MINICRONET: a non-idempotent request'
apply_patch profile-context-net.patch . \
  'net/http/http_network_session.h:send_http2_enable_push_setting' \
  'net/socket/ssl_client_socket_impl.cc:const bool has_profile =' \
  'net/socket/socket_pool_additional_capacity.cc:#if BUILDFLAG(MINICRONET_BUILD)' \
  'net/quic/quic_chromium_client_session.h:advertise_h3_datagram'
apply_patch profile-priority-header-net.patch . \
  'net/http/http_network_session.h:send_priority_header'
apply_patch profile-tls-net.patch . \
  'net/ssl/ssl_config_service.h:profile_grease_signature_algorithms' \
  'net/socket/ssl_client_socket_impl.cc:profile_client_hello_padding_length'
apply_patch protocol-mode-net.patch . \
  'net/http/http_network_session.h:enum class HttpProtocolMode'
# The Core only ever asks for net::MEMORY_CACHE and its ABI exposes no cache
# directory, so the blockfile and simple disk backends are unreachable. Cutting
# the one live reference to them lets the linker drop both (-221,184 bytes on
# linux-x86_64).
apply_patch minicronet-disk-cache-memory-only.patch . \
  'net/disk_cache/disk_cache.cc:#if BUILDFLAG(MINICRONET_BUILD)'
apply_patch minicronet-windows-exports.patch . \
  'base/win/scoped_handle_verifier.cc:#if defined(MINICRONET_BUILD)'
apply_patch xwin-clang-cross.patch . \
  "build/toolchain/win/setup_toolchain.py:optional=sys.platform not in ('win32', 'cygwin')"
apply_patch xwin-no-symbols.patch . \
  'build/config/compiler/BUILD.gn:vendor CRT PDBs'
apply_patch profile-boringssl.patch third_party/boringssl/src \
  'include/openssl/ssl.h:SSL_set_grease_sigalgs_enabled' \
  'include/openssl/ssl.h:SSL_set_client_hello_padding_length' \
  'include/openssl/ssl.h:SSL_GROUP_X25519_KYBER768' \
  'ssl/extensions.cc:body_len' \
  'ssl/ssl_key_share.cc:SSL_GROUP_X25519_KYBER768'
apply_patch icu-minicronet-data.patch third_party/icu \
  'BUILD.gn:data_dir = "minicronet"'
# The IDNA-only dataset never asks for a UCD property name, so PropNameData's
# four generated tables are unreachable and --gc-sections drops 47 KB of them
# (-32,768 bytes on linux-x86_64).
apply_patch minicronet-icu-propname-trim.patch third_party/icu \
  'BUILD.gn:defines += [ "MINICRONET_BUILD" ]' \
  'source/common/propname.cpp:#if defined(MINICRONET_BUILD)'
# //third_party/simdutf forces optimize_speed, which overrides optimize_for_size
# and compiles all four ISA variants at -O2. The Core only reaches it through
# net::Base64Decode, so it honours -Oz here instead: -102,400 bytes on
# linux-x86_64, -376,896 across the eight targets.
apply_patch minicronet-simdutf-size.patch third_party/simdutf \
  'BUILD.gn:is_minicronet_build'

# BoringSSL is built at optimize_max (/O2 and -O2) by upstream on every platform,
# which overrides the Core's optimize_for_size. It is the third instance of the
# same defect class after simdutf and the Windows optimize branch, and it was
# present on all eight targets at once: -442,368 bytes on linux-x86_64,
# ClientHello byte-identical.
apply_patch minicronet-boringssl-size.patch third_party/boringssl \
  'BUILD.gn:is_minicronet_build)'

# The IDNA-only ICU dataset is embedded into the library, so it has to be in the
# Chromium checkout before gn runs. Without it the build falls back to nothing
# and every internationalized hostname aborts the process.
install -d "$CHROMIUM_SRC/third_party/icu/minicronet"
install -m 644 "$ROOT_DIR/core/icu/icudtl.dat" \
  "$CHROMIUM_SRC/third_party/icu/minicronet/icudtl.dat"

test -f "$CHROMIUM_SRC/base/nix/xdg_util_minicronet.cc" \
  || die "minicronet-core.patch is partially applied: base/nix/xdg_util_minicronet.cc missing"

DEST=$CHROMIUM_SRC/minicronet
install -d "$DEST"

# Library sources plus the ABI-conformance smoke/probe sources. The smoke targets
# exercise the callback signatures, so they must travel with the ABI they test.
for source in "$ROOT_DIR"/core/source/*.cc "$ROOT_DIR"/core/source/*.c; do
  install -m 644 "$source" "$DEST/$(basename "$source")"
done
for header in "$ROOT_DIR"/core/source/minicronet/*.h; do
  install -m 644 "$header" "$DEST/$(basename "$header")"
done
# Top-level headers under core/source/ are included as "name.h" by the .cc files
# beside them, so they land at the same level in the checkout. platform_identity.h
# is the first one: it holds the single platform-identity table. A header that
# travels with its sources must not be forgotten here -- the failure mode is a
# build error on every target, which is at least loud, but only if someone looks.
for header in "$ROOT_DIR"/core/source/*.h; do
  install -m 644 "$header" "$DEST/$(basename "$header")"
done
install -m 644 "$ROOT_DIR/core/source/BUILD.gn" "$DEST/BUILD.gn"

# The Core's version string is generated, never hand-written. The previous
# hand-written copy in minicronet.cc drifted (it still said 0.4.0 while the
# workspace was on 0.2.5) and was user-visible through core_version(). This
# runs AFTER the source copy above, because that copy would otherwise overwrite
# the generated header with the committed placeholder.
MINICRONET_VERSION=$(python3 "$ROOT_DIR/tools/version.py") \
  || die "cannot read the workspace version (tools/version.py)"
[[ -n "$MINICRONET_VERSION" ]] || die "empty workspace version"
cat >"$DEST/minicronet_version_generated.h" <<EOF
// Generated by tools/sync-core.sh from the root Cargo.toml. Do not edit.
#ifndef MINICRONET_VERSION_GENERATED_H_
#define MINICRONET_VERSION_GENERATED_H_
#define MINICRONET_VERSION "$MINICRONET_VERSION"
#endif  // MINICRONET_VERSION_GENERATED_H_
EOF
printf 'Core version %s\n' "$MINICRONET_VERSION"
install -m 644 "$ROOT_DIR/core/abi/minicronet.h" "$DEST/minicronet.h"
for table in "$ROOT_DIR"/core/exports/minicronet.{def,exports,lds}; do
  install -m 644 "$table" "$DEST/$(basename "$table")"
done

# BUILD.gn is the authority on what the build reads, so every name it declares
# must exist in the destination. The loop above installs by glob and can only
# prove that whatever exists here was copied; this is the direction that catches
# a BUILD.gn reference with no file behind it, without a second hardcoded list to
# drift from the first.
while IFS= read -r name; do
  [[ -n $name ]] || continue
  # A GN import is the one dotted name here that is not a file in this
  # repository; every other name must have arrived.
  [[ $name == //* ]] && continue
  test -f "$DEST/$name" \
    || die "$DEST/$name missing; core/source/BUILD.gn declares it but nothing installed it"
done < <(grep -oE '"[A-Za-z0-9_./-]+\.[A-Za-z0-9]+"' \
           "$ROOT_DIR/core/source/BUILD.gn" | tr -d '"' | sort -u)

# Prints the relative globs this script installs, one per line.
#
# Read out of the installs themselves rather than restated: a hardcoded list here
# would be a second copy of the same knowledge, and deleting an install loop would
# leave it reporting success for a file nothing carries any more.
install_patterns() {
  # Globs from the for-loops: `for x in "$ROOT_DIR"/a "$ROOT_DIR"/b; do`
  awk '/^ *for [A-Za-z_]* in /{
    line=$0
    sub(/^ *for [A-Za-z_]* in /, "", line)
    sub(/; *do.*$/, "", line)
    n=split(line, parts, " ")
    for (i=1;i<=n;i++) {
      p=parts[i]
      gsub(/"/, "", p)
      sub(/^\$ROOT_DIR\//, "", p)
      if (p !~ /[{}]/ && p ~ /^core\//) print p
    }
  }' "$ROOT_DIR/tools/sync-core.sh"
  # Single-file installs: `install -m 644 "$ROOT_DIR"/<rel> "$DEST/..."`
  awk '/^ *install -m 644 "\$ROOT_DIR\//{
    p=$0
    sub(/^ *install -m 644 "\$ROOT_DIR\//, "", p)
    sub(/".*$/, "", p)
    if (p ~ /^core\// && p != "core/icu/icudtl.dat") print p
  }' "$ROOT_DIR/tools/sync-core.sh"
  # The export tables arrive through one brace-expanded loop; ask bash what it
  # expands to rather than re-parsing the braces.
  eval "for table in \"\$ROOT_DIR\"/core/exports/minicronet.{def,exports,lds}; do
    printf '%s\n' \"\${table#\"\$ROOT_DIR\"/}\"
  done"
}

# ---- the other direction: source tree -> install rules ----
#
# The loops above install by glob. A new file whose extension no glob covers is
# silently left behind, and the failure surfaces much later:
#
#   * core/source/platform_identity.h was added while only *.cc/*.c and
#     minicronet/*.h were carried, so all nine targets failed at compile time
#     with "file not found".
#   * core/exports/minicronet.lds was edited after a build had read the old copy.
#     The linker dropped the new symbols and the build still reported success --
#     an installed file that is stale looks exactly like one that is correct.
#
# Checking the destination *after* the install cannot catch either, because the
# install has already repaired it by then. So this compares the source tree
# against the set of patterns the installs above actually cover, and fails on any
# file that none of them would carry.
covered=0
uncovered=()
while IFS= read -r source; do
  name=$(basename "$source")
  relative=${source#"$ROOT_DIR"/}
  # The patterns come from the install loops above, read out of this script, so
  # the check cannot drift from what the script actually does. A hardcoded list
  # here would be a third copy of the same knowledge and would keep reporting
  # success after the loop it describes was deleted.
  matched=0
  while IFS= read -r pattern; do
    # shellcheck disable=SC2053 - a glob is the point: these are the patterns
    case $relative in
      $pattern) matched=1; break ;;
    esac
  done < <(install_patterns)
  if ((matched)); then
    covered=$((covered + 1))
  else
    uncovered+=("$relative")
  fi
done < <(find "$ROOT_DIR/core/source" "$ROOT_DIR/core/abi" "$ROOT_DIR/core/exports" \
              -maxdepth 2 -type f \( -name "*.cc" -o -name "*.c" -o -name "*.h" \
              -o -name "*.gn" -o -name "*.def" -o -name "*.exports" -o -name "*.lds" \
              -o -name "*.inc" -o -name "*.hpp" -o -name "*.cpp" \) | sort)

if ((${#uncovered[@]})); then
  printf 'sync-core: these files exist but no install rule carries them:\n' >&2
  printf '  %s\n' "${uncovered[@]}" >&2
  die "add an install rule above, one per extension, so the next file of that kind travels too"
fi

# Now that every file has a rule, prove the destination agrees. This catches a
# rule that exists but points somewhere else: byte comparison is the only check
# that cannot be satisfied by copying the wrong thing to the right name.
while IFS= read -r source; do
  name=$(basename "$source")
  [[ $name == minicronet_version_generated.h ]] && continue
  cmp -s "$source" "$DEST/$name" \
    || die "$DEST/$name differs from its source $source after install"
done < <(ls "$ROOT_DIR"/core/source/*.cc "$ROOT_DIR"/core/source/*.c \
              "$ROOT_DIR"/core/source/*.h "$ROOT_DIR"/core/source/minicronet/*.h \
              "$ROOT_DIR"/core/abi/minicronet.h \
              "$ROOT_DIR"/core/exports/minicronet.def \
              "$ROOT_DIR"/core/exports/minicronet.exports \
              "$ROOT_DIR"/core/exports/minicronet.lds \
              "$ROOT_DIR"/core/source/BUILD.gn)

test -f "$DEST/minicronet_version_generated.h" \
  || die "$DEST/minicronet_version_generated.h was not generated"
grep -q "MINICRONET_VERSION \"$MINICRONET_VERSION\"" \
  "$DEST/minicronet_version_generated.h" \
  || die "$DEST/minicronet_version_generated.h does not carry version $MINICRONET_VERSION"

printf 'Synced %d Core sources to %s (every source covered by a rule)\n' \
  "$covered" "$DEST"