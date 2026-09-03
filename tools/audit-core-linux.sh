#!/usr/bin/env bash
set -Eeuo pipefail

OUT_DIR=${1:?usage: audit-core-linux.sh OUT_DIR}
CHROMIUM_SRC=${CHROMIUM_SRC:-/home/sj/chromium/src}
# Runtime profile table is required for the single-library 99--151 selector.
# Keep the allowance narrow; v7's Engine-level CA verifier is intentional.
# Raised for ABI v8, which embeds the 191 KB IDNA-only ICU dataset so no external
# icudtl.dat travels with the library.
MAX_BYTES=${MAX_BYTES:-9400000}
LIB=$OUT_DIR/libminicronet.so
READELF=$CHROMIUM_SRC/third_party/llvm-build/Release+Asserts/bin/llvm-readelf
NM=$CHROMIUM_SRC/third_party/llvm-build/Release+Asserts/bin/llvm-nm
ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# Core sources, ABI header, export tables and Chromium patches all live under
# core/ in this repository, so one root covers every MiniCronet-owned file.
CORE_DIR=$ROOT_DIR/core
RG=${RG:-$(command -v rg || true)}
if [[ -z $RG ]]; then
  printf 'audit-core-linux: ripgrep (rg) is required; set RG to its path\n' >&2
  exit 1
fi
rg() { "$RG" "$@"; }

require_source() {
  local file=$1
  local marker=$2
  if ! grep -Fq "$marker" "$file"; then
    printf 'Required random source marker missing: %s: %s\n' \
      "$file" "$marker" >&2
    exit 1
  fi
}

test -f "$LIB"

if rg -q '^minicronet_profile_verification\s*=\s*true\s*$' \
  "$OUT_DIR/args.gn"; then
  printf 'Profile verification mode is enabled in release args\n' >&2
  exit 1
fi

"$ROOT_DIR/tools/audit-network-featurelist.sh" "$OUT_DIR"

if rg -n -i \
  '(^|[^[:alnum:]_])(srand|rand|RAND_seed|random_device|mt19937|seed_seq|MockRandom|SetRandom)([^[:alnum:]_]|$)|fixed[_ -]?seed|random[_ -]?seed' \
  "$CORE_DIR"; then
  printf 'MiniCronet-owned code contains a fixed/custom random source\n' >&2
  exit 1
fi

if rg -n -U \
  'quic_enable_http3_grease_randomness\s*(,|\))\s*(=\s*)?false|quic_disable_version_negotiation_grease_randomness\s*(,|\))\s*(=\s*)?true' \
  "$CORE_DIR"; then
  printf 'MiniCronet disables QUIC/H3 random GREASE\n' >&2
  exit 1
fi

if ! rg -q -U \
  'quic_disable_version_negotiation_grease_randomness,\s*false,' \
  "$CHROMIUM_SRC/net/third_party/quiche/src/quiche/common/quiche_protocol_flags_list.h" ||
   ! rg -q -U 'quic_enable_http3_grease_randomness,\s*true,' \
  "$CHROMIUM_SRC/net/third_party/quiche/src/quiche/common/quiche_protocol_flags_list.h"; then
  printf 'Pinned QUICHE random GREASE defaults changed\n' >&2
  exit 1
fi

require_source "$CHROMIUM_SRC/base/rand_util_posix.cc" \
  'GetRandomSyscall(output.data(), output.size())'
require_source "$CHROMIUM_SRC/crypto/random.cc" 'base::RandBytes(bytes);'
require_source "$CHROMIUM_SRC/net/websockets/websocket_basic_handshake_stream.cc" \
  'crypto::RandBytes(raw_challenge);'
require_source "$CHROMIUM_SRC/net/websockets/websocket_frame.cc" \
  'base::RandBytes(masking_key.key);'
require_source "$CHROMIUM_SRC/net/http/http_auth_handler_digest.cc" \
  'crypto::RandBytes(rand_bytes);'
require_source "$CHROMIUM_SRC/net/http/http_auth_ntlm_mechanism.cc" \
  'base::RandBytes(output);'
require_source "$CHROMIUM_SRC/net/dns/dns_client.cc" \
  'base::BindRepeating(&base::RandIntInclusive)'
require_source "$CHROMIUM_SRC/net/dns/dns_session.cc" \
  'return static_cast<uint16_t>(rand_callback_.Run());'
require_source "$CHROMIUM_SRC/net/third_party/quiche/src/quiche/common/quiche_random.cc" \
  'RAND_bytes(reinterpret_cast<uint8_t*>(data), len);'
require_source "$CHROMIUM_SRC/third_party/boringssl/src/crypto/rand/rand.cc" \
  'BCM_rand_bytes(buf, len);'
require_source "$CHROMIUM_SRC/third_party/boringssl/src/crypto/rand/rand.cc" \
  'RAND_bytes(&unused, sizeof(unused));'
require_source "$CHROMIUM_SRC/third_party/boringssl/src/ssl/handshake_client.cc" \
  'RAND_bytes(ssl->s3->client_random, sizeof(ssl->s3->client_random))'
require_source "$CHROMIUM_SRC/third_party/boringssl/src/ssl/handshake_client.cc" \
  'RAND_bytes(hs->inner_client_random, sizeof(hs->inner_client_random))'
require_source "$CHROMIUM_SRC/third_party/boringssl/src/ssl/handshake_client.cc" \
  'RAND_bytes(hs->session_id.data(), hs->session_id.size())'
require_source "$CHROMIUM_SRC/third_party/boringssl/src/ssl/handshake.cc" \
  'RAND_bytes(grease_seed, sizeof(grease_seed));'
require_source "$CHROMIUM_SRC/third_party/boringssl/src/ssl/extensions.cc" \
  'RAND_bytes(reinterpret_cast<uint8_t *>(seeds), sizeof(seeds))'
require_source "$CHROMIUM_SRC/third_party/boringssl/src/crypto/hpke/hpke.cc" \
  'RAND_bytes(seed, kem->seed_len);'
require_source "$CHROMIUM_SRC/third_party/boringssl/src/ssl/encrypted_client_hello.cc" \
  'RAND_bytes(payload, payload_len)'
require_source "$CHROMIUM_SRC/net/third_party/quiche/src/quiche/quic/core/crypto/transport_parameters.cc" \
  'uint64_t grease_id64 = random->RandUint64()'
require_source "$CHROMIUM_SRC/net/third_party/quiche/src/quiche/quic/core/http/quic_send_control_stream.cc" \
  'QuicRandom::GetInstance()->RandBytes(&result, sizeof(result));'
require_source "$CHROMIUM_SRC/net/third_party/quiche/src/quiche/common/quiche_random.cc" \
  'RAND_bytes(reinterpret_cast<uint8_t*>(&result), sizeof(result));'
require_source "$CHROMIUM_SRC/net/third_party/quiche/src/quiche/quic/core/quic_utils.cc" \
  'random->RandBytes(connection_id.mutable_data(), connection_id.length());'
require_source "$CHROMIUM_SRC/net/quic/quic_session_pool.cc" \
  'quic::QuicUtils::CreateRandomConnectionId(random_generator_)'
require_source "$CHROMIUM_SRC/net/third_party/quiche/src/quiche/quic/core/quic_connection.cc" \
  'random_generator_->RandBytes(&transmitted_connectivity_probe_payload,'
require_source "$CHROMIUM_SRC/net/third_party/quiche/src/quiche/quic/core/quic_connection.cc" \
  'random_generator_->RandBytes(&flow_label, sizeof(flow_label));'
require_source "$CHROMIUM_SRC/net/third_party/quiche/src/quiche/quic/core/quic_path_validator.cc" \
  'random_->RandBytes(probing_data_.back().frame_buffer.data(),'
require_source "$CHROMIUM_SRC/net/third_party/quiche/src/quiche/quic/core/crypto/curve25519_key_exchange.cc" \
  'rand->RandBytes(private_key, sizeof(private_key));'
require_source "$CHROMIUM_SRC/net/third_party/quiche/src/quiche/quic/core/crypto/quic_crypto_client_config.cc" \
  'rand->RandBytes(proof_nonce, ABSL_ARRAYSIZE(proof_nonce));'
require_source "$CHROMIUM_SRC/net/third_party/quiche/src/quiche/quic/core/quic_versions.cc" \
  'QuicRandom::GetInstance()->RandBytes(&result, sizeof(result));'
require_source "$CHROMIUM_SRC/net/third_party/quiche/src/quiche/quic/core/quic_framer.cc" \
  'QuicRandom::GetInstance()->RandUint64() % (wire_versions.size() + 1)'
require_source "$CHROMIUM_SRC/net/third_party/quiche/src/quiche/quic/core/quic_packet_creator.cc" \
  'random_->InsecureRandUint64() % kFirstFrameLengthRandom'
require_source "$CHROMIUM_SRC/net/third_party/quiche/src/quiche/quic/core/http/http_encoder.cc" \
  'QuicRandom::GetInstance()->RandBytes(&result, sizeof(result));'

if rg -q -i \
  'quic/test_tools/.*random|mock_random\.(cc|h)|MINICRONET_PROFILE_VERIFICATION' \
  "$OUT_DIR" -g '*.ninja' -g '*.rsp'; then
  printf 'Test/deterministic random implementation leaked into release graph\n' >&2
  exit 1
fi

if file "$LIB" | grep -q 'not stripped'; then
  printf 'Release library is not stripped\n' >&2
  exit 1
fi

if strings -a "$LIB" | rg -q -i \
  'MockRandom|MINICRONET_(SSL_KEY_LOG_FILE|FORCE_QUIC_ORIGIN|FORCE_H3_WEBSOCKET|RANDOM|SEED)|fixed.?seed|random.?seed'; then
  printf 'Verification or fixed-random control leaked into release library\n' >&2
  exit 1
fi

header=$($READELF -h "$LIB")
case "$header" in
  *'Class:'*'ELF64'*'Machine:'*'Advanced Micro Devices X86-64'*)
    expected_loader=ld-linux-x86-64.so.2
    target_label=Linux-x86_64
    ;;
  *'Class:'*'ELF32'*'Machine:'*'Intel 80386'*)
    expected_loader=ld-linux.so.2
    target_label=Linux-x86
    ;;
  *'Class:'*'ELF64'*'Machine:'*'AArch64'*)
    expected_loader=ld-linux-aarch64.so.1
    target_label=Linux-arm64
    ;;
  *)
    printf 'Unsupported Linux ELF target for %s\n' "$LIB" >&2
    exit 1
    ;;
esac
sections=$($READELF -S "$LIB")
if grep -qE '\.comment|\.note\.gnu\.build-id' <<<"$sections"; then
  printf 'Release library contains removable compiler/linker metadata\n' >&2
  exit 1
fi

actual=$($NM -D --defined-only --format=posix "$LIB" |
  awk '{sub(/@@.*/, "", $1); print $1}' | sort)
expected=$(printf '%s\n' \
  mn_abi_version \
  mn_engine_create \
  mn_engine_release \
  mn_engine_retain \
  mn_request_cancel \
  mn_request_create \
  mn_request_follow_redirect \
  mn_request_resume_read \
  mn_request_release \
  mn_request_retain \
  mn_request_start \
  mn_request_upload_write \
  mn_websocket_cancel \
  mn_websocket_close \
  mn_websocket_create \
  mn_websocket_release \
  mn_websocket_retain \
  mn_websocket_send \
  mn_websocket_start \
  mn_version_string | sort)
diff -u <(printf '%s\n' "$expected") <(printf '%s\n' "$actual")

actual_deps=$($READELF -d "$LIB" |
  awk '/NEEDED/ {gsub(/[\[\]]/, "", $NF); print $NF}' | sort)
expected_deps=$(printf '%s\n' \
  "$expected_loader" \
  libc.so.6 \
  libdl.so.2 \
  libgcc_s.so.1 \
  libm.so.6 \
  libnspr4.so \
  libnss3.so \
  libnssutil3.so \
  libpthread.so.0 | sort)
diff -u <(printf '%s\n' "$expected_deps") \
  <(printf '%s\n' "$actual_deps")

if $READELF -d "$LIB" | grep -Eq 'RPATH|RUNPATH'; then
  printf 'Forbidden runtime library search path\n' >&2
  exit 1
fi

bytes=$(stat -c %s "$LIB")
if ((bytes > MAX_BYTES)); then
  printf 'Library size %s exceeds %s budget %s bytes\n' \
    "$bytes" "$target_label" "$MAX_BYTES" >&2
  exit 1
fi

printf 'Audited %s: %s bytes, twenty public ABI symbols (%s)\n' \
  "$LIB" "$bytes" "$target_label"
