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
# Not yet owned by this repository: the profile-verification probes
# (profile_isolation_probe.c, profile_feature_probe.cc,
# profile_state_isolation_probe.cc, websocket_extended_connect_probe.c). They
# only build with minicronet_profile_verification=true and depend on the profile
# evidence pipeline, so they stay in the Chromium checkout for now. Migrating
# them is tracked in docs/MIGRATION_FROM_NEW.md.

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
CHROMIUM_SRC=${CHROMIUM_SRC:-/home/sj/chromium/src}
PATCH_DIR=$ROOT_DIR/core/patches

die() { printf 'sync-core: %s\n' "$*" >&2; exit 1; }

test -d "$CHROMIUM_SRC/net" || die "no Chromium checkout at $CHROMIUM_SRC"

EXPECTED_REVISION=$(tr -d '[:space:]' <"$ROOT_DIR/CHROMIUM_REVISION")
ACTUAL_REVISION=$(git -C "$CHROMIUM_SRC" rev-parse HEAD)
[[ "$ACTUAL_REVISION" == "$EXPECTED_REVISION" ]] \
  || die "Chromium revision mismatch: expected $EXPECTED_REVISION, got $ACTUAL_REVISION"

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
  local marker file text
  for marker in "$@"; do
    file=${marker%%:*}
    text=${marker#*:}
    grep -Fq "$text" "$repo/$file" \
      || die "$name is partially applied: $file lacks '$text'"
  done
  printf 'verified %s\n' "$name"
}

apply_patch minicronet-core.patch . \
  'build/config/cronet/config.gni:is_minicronet_build = false' \
  'build_overrides/build.gni:is_cronet_build || is_minicronet_build' \
  'net/features.gni:disable_file_support = is_cronet_build || is_minicronet_build' \
  'net/BUILD.gn:!is_cronet_build && !is_minicronet_build' \
  'base/BUILD.gn:nix/xdg_util_minicronet.cc'
apply_patch profile-context-net.patch . \
  'net/http/http_network_session.h:send_http2_enable_push_setting'
apply_patch profile-priority-header-net.patch . \
  'net/http/http_network_session.h:send_priority_header'
apply_patch profile-tls-net.patch . \
  'net/ssl/ssl_config_service.h:profile_grease_signature_algorithms'
apply_patch profile-clienthello-padding-net.patch . \
  'net/socket/ssl_client_socket_impl.cc:profile_client_hello_padding_length'
apply_patch profile-feature-isolation-net.patch . \
  'net/socket/ssl_client_socket_impl.cc:const bool has_profile =' \
  'net/socket/socket_pool_additional_capacity.cc:#if BUILDFLAG(MINICRONET_BUILD)'
apply_patch protocol-mode-net.patch . \
  'net/http/http_network_session.h:enum class HttpProtocolMode'
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
  'ssl/ssl_key_share.cc:SSL_GROUP_X25519_KYBER768'

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
install -m 644 "$ROOT_DIR/core/source/BUILD.gn" "$DEST/BUILD.gn"
install -m 644 "$ROOT_DIR/core/abi/minicronet.h" "$DEST/minicronet.h"
for table in "$ROOT_DIR"/core/exports/minicronet.{def,exports,lds}; do
  install -m 644 "$table" "$DEST/$(basename "$table")"
done

# The verification-only probes are not in this repository; fail loudly rather
# than silently dropping targets BUILD.gn still declares.
for probe in profile_isolation_probe.c profile_feature_probe.cc \
             profile_state_isolation_probe.cc websocket_extended_connect_probe.c; do
  test -f "$DEST/$probe" || die "$DEST/$probe missing; it is not owned by this repository yet"
done

printf 'Synced %d Core sources to %s\n' \
  "$(ls "$ROOT_DIR"/core/source/*.cc "$ROOT_DIR"/core/source/*.c \
       "$ROOT_DIR"/core/source/minicronet/*.h | wc -l)" \
  "$DEST"
