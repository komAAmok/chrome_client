#include "minicronet.h"

#include <algorithm>
#include <atomic>
#include <cstring>
#include <new>
#include <string>
#include <utility>
#include <vector>

#include "base/compiler_specific.h"
#include "base/containers/span.h"
#include "base/i18n/streaming_utf8_validator.h"
#include "build/build_config.h"
#include "base/strings/string_split.h"
#include "base/strings/string_util.h"
#include "net/net_buildflags.h"
#include "net/base/host_mapping_rules.h"
#include "net/dns/public/dns_over_https_config.h"
#include "minicronet/engine.h"
#include "platform_identity.h"
#include "minicronet/minicronet_version_generated.h"
#include "minicronet/profile_context.h"
#include "minicronet/request.h"
#include "minicronet/websocket.h"
#include "net/http/http_util.h"
#include "net/proxy_resolution/proxy_config.h"
#include "url/gurl.h"
#include "url/origin.h"

struct mn_engine {
  std::atomic_uint32_t refs{1};
  scoped_refptr<minicronet::Engine> impl;
  // Backs the pointers mn_engine_cookies_get hands out. The ABI promises a
  // cookie's strings stay readable while the caller holds the result, and a
  // stack frame inside the Core cannot outlive the call that produced it. The
  // lifetime is therefore "until the next cookies_get on this engine, or until
  // release", which the header states; a caller that keeps a cookie copies the
  // bytes, the same rule the response header strings use.
  minicronet::CookieView cookie_storage;
};

struct mn_request {
  std::atomic_uint32_t refs{1};
  scoped_refptr<minicronet::Request> impl;
};

struct mn_websocket {
  std::atomic_uint32_t refs{1};
  scoped_refptr<minicronet::WebSocket> impl;
};

namespace {

// The version is injected by the build, never written here. tools/version.py
// reads it from the root Cargo.toml and tools/sync-core.sh passes it in, so a
// release bump cannot leave a stale copy behind: the hand-written "0.4.0" this
// replaced was still being reported by chrome_client.core_version() long after
// the workspace had moved on.
//
// The literal carries the "minicronet/" prefix because
// tools/audit-core-binaries.sh reads exactly that shape out of the artifact with
// strings(1) to prove which build shipped. mn_version_string() returns the
// pointer past the prefix, so callers still see a bare version number.
//
// [[gnu::used]] because the marker is read out of the artifact by
// tools/audit-core-binaries.sh, not by any caller: -Oz with LTO drops a
// constant whose only reader is a compile-time expression, and then the gate
// fails on a perfectly good build.
//
// This literal used to be the default User-Agent. That changed when the default
// UA became a browser identity, so the marker moved here -- leaving it nowhere
// would have quietly disabled the version gate.
[[gnu::used]] constexpr char kVersionMarker[] =
    "minicronet/" MINICRONET_VERSION;
constexpr size_t kVersionPrefixLength = sizeof("minicronet/") - 1;// Caller-supplied counts and lengths are untrusted. Every one of them reaches a
// std::string/vector constructor or a base::span before Chromium sees it, and
// this build is compiled -fno-exceptions, so an over-long value does not throw
// -- it aborts the host process (measured: header_count = 2^60 aborted with
// "length_error ... in -fno-exceptions mode"; header_count = 4096 over a
// one-element array read past the end).
//
// These are safety valves against a corrupt count, not feature limits. The
// header cap sits far above real use -- the Core was measured sending 16000
// request headers -- and 16384 * sizeof(pair<string,string>) is a harmless
// 1 MiB reserve. The field cap keeps a single line sane; the two together bound
// the total, since either one alone leaves the product unbounded.
// kMaxUrlLength is Chromium's own cross-process ceiling (url/url_constants.h
// kMaxURLChars).
constexpr size_t kMaxUrlLength = 2u * 1024u * 1024u;
// ClientSocketPoolManager::socket_soft_cap_per_pool() is 256 for both pool
// types (net/socket/client_socket_pool_manager.cc), so a per-group limit above
// that can never be reached.
constexpr uint32_t kMaxSocketsPerGroupLimit = 256;

constexpr size_t kMaxHeaderCount = 16384u;
constexpr size_t kMaxHeaderFieldLength = 64u * 1024u;
constexpr size_t kMaxUploadBodyLength = 256u * 1024u * 1024u;
constexpr size_t kMaxWebSocketProtocolCount = 64u;

// The newest Chrome profile the generated table carries. This is the Chromium
// the Core is compiled from, so it is also the version the default User-Agent
// announces. Kept next to the platform token because the two are the only
// inputs to a browser identity.
constexpr int kCurrentChromiumMajor = 157;

// The identity table lives in platform_identity.h so the ABI getters, the
// default User-Agent and Engine::platform_hint all read one copy. AGENTS 1.1:
// a second implementation of one fact is the defect, even in the same language.
// The User-Agent an engine sends when no historical profile supplies one. It
// names a browser because that is what this library is: a build that reports
// "minicronet/<version>" identifies itself to every origin, which is a worse
// default than announcing the Chromium it actually is.
std::string DefaultUserAgent(mn_platform_t platform) {
  return std::string("Mozilla/5.0 (") + minicronet::PlatformToken(platform) +
         ") AppleWebKit/537.36 (KHTML, like Gecko) Chrome/" +
         std::to_string(kCurrentChromiumMajor) + ".0.0.0 Safari/537.36";
}

// The version portion of a Mozilla/5.0 User-Agent string. The profile conflict
// check uses it so that changing only the platform group is not mistaken for
// claiming a different browser version.
std::string_view UserAgentVersion(std::string_view user_agent) {
  constexpr std::string_view kMarker = "Chrome/";
  const size_t at = user_agent.find(kMarker);
  if (at == std::string_view::npos) {
    return std::string_view();
  }
  const size_t begin = at + kMarker.size();
  size_t end = begin;
  while (end < user_agent.size() &&
         (user_agent[end] == '.' ||
          (user_agent[end] >= '0' && user_agent[end] <= '9'))) {
    ++end;
  }
  return user_agent.substr(begin, end - begin);
}

// TransportSecurityState matches its bypass list against a hostname and only
// supports single-label entries: its constructor DCHECKs that
// (net/http/transport_security_state.cc:246-248) and a release build then
// ignores anything else. Rejecting here is the only way the caller learns the
// entry would not apply.
bool IsValidHstsBypassEntry(std::string_view entry) {
  return !entry.empty() && entry.find('.') == std::string_view::npos &&
         entry.find(' ') == std::string_view::npos;
}

std::string HistoricalUserAgent(const minicronet::ProfileContext& profile,
                                mn_platform_t platform) {
  // Chrome 86-104 and Chrome >=105 send structurally different User-Agent
  // strings, and the difference is the browser's own behaviour, not a capture
  // artefact:
  //
  //   86-104   full version, e.g. Chrome/99.0.4844.84
  //   >=105    User-Agent Reduction: Chrome/<major>.0.0.0
  //
  // Both halves are read from the reference captures for the matching major,
  // which is why the reduction boundary lives at 105 here rather than at some
  // rounder number. The previous table covered 99-104 only and returned an
  // empty string below 99, so chrome_86..chrome_98 sent `minicronet/<version>`.
  //
  // Indexed by major - kFirstProfileMajor for the unreduced range only.
  static constexpr const char* kFullVersions[] = {
      "86.0.4240.198",
      "87.0.4280.141",
      "88.0.4324.190",
      "89.0.4389.128",
      "90.0.4430.212",
      "91.0.4472.164",
      "92.0.4515.159",
      "93.0.4577.82",
      "94.0.4606.81",
      "95.0.4638.69",
      "96.0.4664.110",
      "97.0.4692.99",
      "98.0.4758.102",
      "99.0.4844.84",
      "100.0.4896.127",
      "101.0.4951.67",
      "102.0.5005.115",
      "103.0.5060.134",
      "104.0.5112.102",
  };
  // profile.id() is "chrome_<major>", already checked against the generated
  // table by ProfileContext::Create. This stays defence in depth so an
  // unparsable id cannot index kFullVersions out of bounds.
  const std::string id = profile.id();
  if (id.size() <= 7 || id.compare(0, 7, "chrome_") != 0) {
    return std::string();
  }
  int major = 0;
  for (char digit : id.substr(7)) {
    if (digit < '0' || digit > '9') {
      return std::string();
    }
    major = major * 10 + (digit - '0');
    if (major > 999) {
      return std::string();
    }
  }
  constexpr int kFirstProfileMajor = 86;
  constexpr int kLastProfileMajor = 158;
  // User-Agent Reduction: from this major Chrome sends <major>.0.0.0.
  constexpr int kReducedUserAgentFirstMajor = 105;
  static_assert(std::size(kFullVersions) ==
                    static_cast<size_t>(kReducedUserAgentFirstMajor -
                                        kFirstProfileMajor),
                "one full version string per profile before the reduction");
  if (major < kFirstProfileMajor || major > kLastProfileMajor) {
    return std::string();
  }
  const std::string version =
      major < kReducedUserAgentFirstMajor
          ? base::span(kFullVersions)[major - kFirstProfileMajor]
          : std::to_string(major) + ".0.0.0";
  // The wire evidence in profiles/chrome-<major>/captures.json was captured
  // from a Windows desktop client, but a profile cannot claim a platform the
  // socket is not on: the token follows the host this Core is running on, the
  // way a real Chrome build does. The version number is the part the profile
  // actually owns.
  return std::string("Mozilla/5.0 (") + minicronet::PlatformToken(platform) +
         ") AppleWebKit/537.36 (KHTML, like Gecko) Chrome/" + version +
         " Safari/537.36";
}

bool IsForbiddenWebSocketHeader(std::string_view name) {
  return base::EqualsCaseInsensitiveASCII(name, "Connection") ||
         base::EqualsCaseInsensitiveASCII(name, "Host") ||
         base::EqualsCaseInsensitiveASCII(name, "Origin") ||
         base::EqualsCaseInsensitiveASCII(name, "User-Agent") ||
         base::EqualsCaseInsensitiveASCII(name, "Upgrade") ||
         base::StartsWith(name, "Sec-WebSocket-",
                          base::CompareCase::INSENSITIVE_ASCII);
}

// Mirrors net::IsStrictlyValidCloseStatusCode
// (net/websockets/websocket_channel.cc): 1014 is unassigned and [1015, 3000) is
// reserved. 1014 used to be listed here, so mn_websocket_close accepted it and
// WebSocketChannel then silently substituted 1011 with an empty reason --
// measured: requested 1014, the peer received 1011. A validator must reject
// what it cannot honour rather than hand the caller a different close code.
bool IsValidWebSocketCloseCode(uint16_t code) {
  switch (code) {
    case 1000:
    case 1001:
    case 1002:
    case 1003:
    case 1007:
    case 1008:
    case 1009:
    case 1010:
    case 1011:
    case 1012:
    case 1013:
      return true;
    default:
      return code >= 3000 && code <= 4999;
  }
}

template <typename T> void Retain(T *object) {
  uint32_t refs = object->refs.load(std::memory_order_relaxed);
  while (refs != UINT32_MAX && !object->refs.compare_exchange_weak(
                                   refs, refs + 1, std::memory_order_relaxed,
                                   std::memory_order_relaxed)) {
  }
}

} // namespace

uint32_t MN_CALL mn_abi_version(void) { return MN_ABI_VERSION; }


mn_result_t MN_CALL mn_platform_user_agent_group(mn_platform_t platform,
                                               const char **out_group,
                                               uint32_t *out_length) {
  if (!out_group || !out_length) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  if (platform < MN_PLATFORM_AUTO || platform > MN_PLATFORM_LINUX) {
    return MN_ERROR_UNSUPPORTED_ABI;
  }
  const char *group = minicronet::PlatformToken(platform);
  *out_group = group;
  *out_length = static_cast<uint32_t>(std::strlen(group));
  return MN_OK;
}

mn_result_t MN_CALL mn_platform_hint(mn_platform_t platform,
                                    const char **out_hint,
                                    uint32_t *out_length) {
  if (!out_hint || !out_length) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  if (platform < MN_PLATFORM_AUTO || platform > MN_PLATFORM_LINUX) {
    return MN_ERROR_UNSUPPORTED_ABI;
  }
  const char *hint = minicronet::PlatformHint(platform);
  *out_hint = hint;
  *out_length = static_cast<uint32_t>(std::strlen(hint));
  return MN_OK;
}

mn_result_t MN_CALL mn_engine_capabilities(uint32_t *out_flags) {
  if (!out_flags) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  uint32_t flags = 0;
#if BUILDFLAG(ENABLE_QUIC_PROXY_SUPPORT)
  flags |= MN_CAPABILITY_QUIC_PROXY;
#endif
  // Bit 1 is deliberately never set: see the reserved note in minicronet.h.
  // ENABLE_BRACKETED_PROXY_URIS is on, but the ABI has no field that can carry
  // a bracketed chain, so advertising it would be a claim this ABI cannot keep.
  *out_flags = flags;
  return MN_OK;
}

const char *MN_CALL mn_version_string(void) {
  // The suffix inside the marker, addressed with the escape hatch this codebase
  // uses wherever a static bound is provable: kVersionPrefixLength is the exact
  // prefix size, so the result points at the first version character.
  return UNSAFE_BUFFERS(kVersionMarker + kVersionPrefixLength);
}


mn_result_t MN_CALL mn_engine_create(const mn_engine_config_t *config,
                                     mn_engine_t **out_engine) {
  if (!config || !out_engine) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  *out_engine = nullptr;

  if (config->size != sizeof(mn_engine_config_t) ||
      config->version != MN_ABI_VERSION) {
    return config->version != MN_ABI_VERSION ? MN_ERROR_UNSUPPORTED_ABI
                                             : MN_ERROR_INVALID_ARGUMENT;
  }

  // Default to a browser identity this build can actually claim. The platform
  // group is overridable; the version names the Chromium underneath.
  std::string user_agent = DefaultUserAgent(config->platform);
  std::string accept_language;
  net::ProxyConfig proxy_config;
  if (config->user_agent_length && !config->user_agent) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  if (config->user_agent) {
    user_agent.assign(config->user_agent, config->user_agent_length);
    if (!net::HttpUtil::IsValidHeaderValue(user_agent)) {
      return MN_ERROR_INVALID_ARGUMENT;
    }
  }
  if (config->accept_language_length && !config->accept_language) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  if (config->accept_language) {
    accept_language.assign(config->accept_language,
                           config->accept_language_length);
    if (!net::HttpUtil::IsValidHeaderValue(accept_language)) {
      return MN_ERROR_INVALID_ARGUMENT;
    }
  }
  if (config->proxy_rules_length && !config->proxy_rules) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  if (config->proxy_rules) {
    std::string proxy_rules(config->proxy_rules, config->proxy_rules_length);
    if (proxy_rules.find('\0') != std::string::npos) {
      return MN_ERROR_INVALID_ARGUMENT;
    }
    // The third argument is what lets ParseFromString turn "quic://h:443" into
    // a SCHEME_QUIC server at all; its default is false, so the proxy_chain
    // field and this one would disagree about the same scheme without it.
    //
    // Both arguments are the buildflags, never literal `true`: proxy_config.cc:105
    // CHECKs that a caller does not ask for bracketed chains when the flag is
    // off, and proxy_config.cc:110 does the same for is_quic_allowed. Passing the
    // flags makes the call agree with the artifact it was compiled into; passing
    // true would turn a build without them into a CHECK crash at create time.
    // The scheme whitelist below covers proxy_chain only, so it cannot stand in
    // for either argument here.
    proxy_config.proxy_rules().ParseFromString(proxy_rules,
                                             /*allow_bracketed_proxy_chains=*/
                                             BUILDFLAG(ENABLE_BRACKETED_PROXY_URIS),
                                             /*is_quic_allowed=*/
                                             BUILDFLAG(ENABLE_QUIC_PROXY_SUPPORT));
    // ParseFromString is not a validator: ProxyList::AddProxyChain documents
    // "Silently discard malformed inputs" (proxy_list.cc:58), and the entries
    // are comma-split before that call, so "[https://a:1,https://b:2]" loses
    // both halves and "garbage://x" loses its only one. A rules value that
    // names a proxy and yields none is a DIRECT connection, which is the
    // accepted-and-ignored input section 8 forbids.
    //
    // ProxyRules::empty() cannot see it: it returns "type == Type::EMPTY", and
    // ParseFromString sets type = Type::PROXY_LIST before adding any entry
    // (proxy_config.cc:135), so the type stays PROXY_LIST while the list is
    // emptied underneath it. The list itself has to be asked, and single_proxies
    // is the list that case writes to.
    // Both shapes have to be asked, because ParseFromString can empty a list
    // under either of them:
    //   PROXY_LIST             -- one list for every URL, single_proxies.
    //   PROXY_LIST_PER_SCHEME  -- "http=...;https=...", one list per scheme.
    // The per-scheme shape is the one that was still slipping through: with
    // "http=garbage://x;https=garbage://y" every list ends up empty, the type
    // stays PROXY_LIST_PER_SCHEME, and Apply() then finds no proxy at all and
    // connects directly -- measured as a 200 from a local origin while the
    // proxy port recorded zero connections.
    const net::ProxyConfig::ProxyRules &parsed = proxy_config.proxy_rules();
    bool parsed_no_proxy;
    switch (parsed.type) {
      case net::ProxyConfig::ProxyRules::Type::PROXY_LIST:
        parsed_no_proxy = parsed.single_proxies.IsEmpty();
        break;
      case net::ProxyConfig::ProxyRules::Type::PROXY_LIST_PER_SCHEME:
        parsed_no_proxy = parsed.proxies_for_http.IsEmpty() &&
                          parsed.proxies_for_https.IsEmpty() &&
                          parsed.proxies_for_ftp.IsEmpty() &&
                          parsed.fallback_proxies.IsEmpty();
        break;
      case net::ProxyConfig::ProxyRules::Type::EMPTY:
        parsed_no_proxy = true;
        break;
    }
    if (!proxy_rules.empty() && parsed_no_proxy) {
      return MN_ERROR_INVALID_ARGUMENT;
    }
  }
  if ((config->proxy_username_length && !config->proxy_username) ||
      (config->proxy_password_length && !config->proxy_password)) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  std::string proxy_username;
  std::string proxy_password;
  if (config->proxy_username) {
    proxy_username.assign(config->proxy_username,
                          config->proxy_username_length);
  }
  if (config->proxy_password) {
    proxy_password.assign(config->proxy_password,
                          config->proxy_password_length);
  }
  if (proxy_username.find('\0') != std::string::npos ||
      proxy_password.find('\0') != std::string::npos ||
      !base::IsStringUTF8(proxy_username) ||
      !base::IsStringUTF8(proxy_password)) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  if (config->http_cache_mode != MN_HTTP_CACHE_ENABLED &&
      config->http_cache_mode != MN_HTTP_CACHE_DISABLED) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  if (config->protocol_mode < MN_PROTOCOL_NATIVE ||
      config->protocol_mode > MN_PROTOCOL_FORCE_H3) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  if (config->tls_verify_mode < MN_TLS_VERIFY_CHROMIUM_DEFAULT ||
      config->tls_verify_mode > MN_TLS_VERIFY_INSECURE) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  if (config->custom_ca_pem_length && !config->custom_ca_pem) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  constexpr size_t kMaxCustomCaPemLength = 16u * 1024u * 1024u;
  if (config->custom_ca_pem_length > kMaxCustomCaPemLength) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  if (config->tls_verify_mode == MN_TLS_VERIFY_CHROMIUM_DEFAULT &&
      config->custom_ca_pem_length != 0) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  if (config->tls_verify_mode == MN_TLS_VERIFY_INSECURE &&
      config->custom_ca_pem_length != 0) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  if (config->tls_verify_mode == MN_TLS_VERIFY_CUSTOM_CA &&
      config->custom_ca_pem_length == 0) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  std::string custom_ca_pem;
  if (config->custom_ca_pem_length) {
    custom_ca_pem.assign(
        reinterpret_cast<const char*>(config->custom_ca_pem),
        config->custom_ca_pem_length);
    if (custom_ca_pem.find('\0') != std::string::npos ||
        !minicronet::ValidateCustomCaPem(custom_ca_pem)) {
      return MN_ERROR_INVALID_ARGUMENT;
    }
  }

  if ((config->profile_id_length && !config->profile_id) ||
      (config->profile_namespace_length && !config->profile_namespace)) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  std::string profile_id;
  std::string profile_namespace;
  if (config->profile_id) {
    profile_id.assign(config->profile_id, config->profile_id_length);
  }
  if (config->profile_namespace) {
    profile_namespace.assign(config->profile_namespace,
                             config->profile_namespace_length);
  }
  if (profile_id.find('\0') != std::string::npos ||
      profile_namespace.find('\0') != std::string::npos) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  if (!profile_namespace.empty() &&
      profile_namespace != "minicronet/" +
                               (profile_id.empty() ? "chromium_current"
                                                   : profile_id)) {
    return MN_ERROR_PROFILE_CONFLICT;
  }
  if (config->platform < MN_PLATFORM_AUTO ||
      config->platform > MN_PLATFORM_LINUX) {
    return MN_ERROR_INVALID_ARGUMENT;
  }

  minicronet::ProfileContext profile;
  if (!minicronet::ProfileContext::Create(std::move(profile_id),
                                          std::move(profile_namespace),
                                          &profile)) {
    return MN_ERROR_PROFILE_UNSUPPORTED;
  }
  // A profile records what its release could actually do. chrome_86..chrome_92
  // have no HTTP/3 behaviour that can be reproduced from the reference
  // evidence, so asking for a forced H3 request is answered with an error
  // instead of silently sending a QUIC ClientHello those releases never sent.
  // The alternative -- falling back to H2 -- would report a fidelity this build
  // does not have; see docs/COMPATIBILITY_BOUNDARY.md.
  if (config->protocol_mode == MN_PROTOCOL_FORCE_H3 && profile.data() &&
      !profile.data()->h3_supported) {
    return MN_ERROR_PROFILE_UNSUPPORTED;
  }
  if (!profile.is_current()) {
    std::string profile_user_agent =
        HistoricalUserAgent(profile, config->platform);
    // A platform override changes the User-Agent group on purpose, so the
    // conflict check compares the version and not the whole string. Without
    // this, platform=windows on a chrome_124 engine would be rejected as a
    // profile conflict even though it is exactly what was asked for.
    if (config->user_agent && user_agent != profile_user_agent &&
        UserAgentVersion(user_agent) !=
            UserAgentVersion(profile_user_agent)) {
      return MN_ERROR_PROFILE_CONFLICT;
    }
    if (!profile_user_agent.empty()) {
      user_agent = std::move(profile_user_agent);
    }
  }

  // ---- v9 options. Every field below either takes effect or fails here; none
  // is accepted and then ignored (AGENTS §8).
  minicronet::EngineOptions options;
  if ((config->host_mapping_rules_length && !config->host_mapping_rules) ||
      (config->dns_nameservers_length && !config->dns_nameservers) ||
      (config->doh_template_length && !config->doh_template) ||
      (config->hsts_bypass_list_length && !config->hsts_bypass_list) ||
      (config->proxy_chain_length && !config->proxy_chain)) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  if (config->host_mapping_rules) {
    options.host_mapping_rules.assign(config->host_mapping_rules,
                                      config->host_mapping_rules_length);
    if (options.host_mapping_rules.find('\0') != std::string::npos) {
      return MN_ERROR_INVALID_ARGUMENT;
    }
    // Parsed by Chromium's HostMappingRules, never here: a second parser would
    // have to track its syntax and would silently disagree with it.
    // AddRuleFromString reports an unparsable rule
    // (net/base/host_mapping_rules.cc:107-132), so a typo cannot become an
    // engine whose mapping quietly matches nothing.
    net::HostMappingRules rules;
    for (const auto &rule : base::SplitStringPiece(
             options.host_mapping_rules, ",", base::TRIM_WHITESPACE,
             base::SPLIT_WANT_NONEMPTY)) {
      if (!rules.AddRuleFromString(std::string(rule))) {
        return MN_ERROR_INVALID_ARGUMENT;
      }
    }
  }
  if (config->dns_nameservers) {
    options.dns_nameservers.assign(config->dns_nameservers,
                                   config->dns_nameservers_length);
    if (options.dns_nameservers.find('\0') != std::string::npos) {
      return MN_ERROR_INVALID_ARGUMENT;
    }
  }
  if (config->doh_template) {
    options.doh_template.assign(config->doh_template,
                                config->doh_template_length);
    if (options.doh_template.find('\0') != std::string::npos) {
      return MN_ERROR_INVALID_ARGUMENT;
    }
    if (!net::DnsOverHttpsConfig::FromString(options.doh_template)
             .has_value()) {
      return MN_ERROR_INVALID_ARGUMENT;
    }
  }
  if (config->dns_secure_mode > 2) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  // Refused rather than accepted-and-ignored. Chromium's desktop build has no
  // DoT client, and setting dns_over_tls_active only makes
  // DnsClient::CanUseInsecureDnsTransactions() return false
  // (net/dns/dns_client.cc:168): the built-in resolver then declines every job
  // and TaskType::SYSTEM answers, so dns_nameservers and doh_template are
  // dropped while the request still succeeds. Measured -- 4 queries on the
  // configured server with this 0, 0 queries (UDP and TCP) with it 1, and a
  // 200 from the system resolver either way. See the field's comment in
  // core/abi/minicronet.h.
  if (config->dns_over_tls != 0) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  options.dns_secure_mode = config->dns_secure_mode;
  options.dns_over_tls = 0;
  options.dns_rotate = config->dns_rotate;
  // Refused rather than accepted-and-ignored, for the same reason as
  // dns_over_tls above: Chromium has no EDNS0 padding control, so the value
  // used to reach EngineOptions and stop there -- core/source/engine.cc never
  // read it, and a caller asking for padded queries silently got unpadded
  // ones. See the field's comment in core/abi/minicronet.h.
  if (config->dns_padding != 0) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  options.dns_padding = 0;
  // A DoH template with secure DNS off is a contradiction: one says DoH may be
  // used, the other says it must not.
  if (!options.doh_template.empty() && options.dns_secure_mode == 0) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  if (config->hsts_bypass_list) {
    std::string bypass(config->hsts_bypass_list,
                       config->hsts_bypass_list_length);
    if (bypass.find('\0') != std::string::npos) {
      return MN_ERROR_INVALID_ARGUMENT;
    }
    for (const auto &entry : base::SplitStringPiece(
             bypass, ",", base::TRIM_WHITESPACE, base::SPLIT_WANT_NONEMPTY)) {
      if (!IsValidHstsBypassEntry(entry)) {
        return MN_ERROR_INVALID_ARGUMENT;
      }
      options.hsts_bypass_list.emplace_back(entry);
    }
  }
  if (config->proxy_chain) {
    options.proxy_chain.assign(config->proxy_chain, config->proxy_chain_length);
    if (options.proxy_chain.find('\0') != std::string::npos) {
      return MN_ERROR_INVALID_ARGUMENT;
    }
    // A value that is non-empty but splits into no entry (",", "   ") would
    // leave Engine with an empty chain, which is a DIRECT connection: the same
    // accepted-and-ignored downgrade as an all-invalid proxy_rules value.
    // Counting the split entries rather than trimming by hand keeps this in
    // step with the loop below, which uses the same splitter.
    if (!options.proxy_chain.empty() &&
        base::SplitStringPiece(options.proxy_chain, ",", base::TRIM_WHITESPACE,
                               base::SPLIT_WANT_NONEMPTY)
            .empty()) {
      return MN_ERROR_INVALID_ARGUMENT;
    }
    // Every entry must be a scheme this artifact can actually dial. Engine
    // silently drops an entry it does not recognise, which turns a typo or an
    // unsupported scheme into a direct connection -- the accepted-and-ignored
    // input section 8 forbids. Checking here is also the only way to report
    // "this build has no QUIC proxy support" as a result code instead of as
    // a request that fails later with ERR_NO_SUPPORTED_PROXIES.
    for (const auto &entry : base::SplitStringPiece(
             options.proxy_chain, ",", base::TRIM_WHITESPACE,
             base::SPLIT_WANT_NONEMPTY)) {
      const size_t scheme_end = entry.find("://");
      if (scheme_end == std::string_view::npos) {
        return MN_ERROR_INVALID_ARGUMENT;
      }
      const std::string_view scheme = entry.substr(0, scheme_end);
      if (base::EqualsCaseInsensitiveASCII(scheme, "quic")) {
#if !BUILDFLAG(ENABLE_QUIC_PROXY_SUPPORT)
        return MN_ERROR_UNSUPPORTED_ABI;
#else
        continue;
#endif
      }
      // Must be exactly the set Engine accepts (core/source/engine.cc:
      // "socks"/"socks5" -> SCHEME_SOCKS5, "socks4", "https", "quic", "http").
      // A wider list is worse than no list: it turns a clear
      // INVALID_ARGUMENT into an INITIALIZATION_FAILED from a context Engine
      // never built, which is what "socks" did before engine.cc handled it.
      if (base::EqualsCaseInsensitiveASCII(scheme, "http") ||
          base::EqualsCaseInsensitiveASCII(scheme, "https") ||
          base::EqualsCaseInsensitiveASCII(scheme, "socks") ||
          base::EqualsCaseInsensitiveASCII(scheme, "socks4") ||
          base::EqualsCaseInsensitiveASCII(scheme, "socks5")) {
        continue;
      }
      return MN_ERROR_INVALID_ARGUMENT;
    }
    // proxy_rules and proxy_chain describe the same connection path twice;
    // picking one silently is the kind of accepted-and-ignored input this ABI
    // refuses.
    if (!proxy_config.proxy_rules().empty()) {
      return MN_ERROR_INVALID_ARGUMENT;
    }
  }
  options.platform = config->platform;
  // The Core now owns the captured request-header set, so the switch is
  // meaningful: 1 asks for it (the default behaviour for a historical
  // profile), 0 suppresses it and leaves only the caller's headers. There is
  // nothing to reject any more -- the data exists and is compiled in.
  if (config->inject_client_hints > 1) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  // ---- v11. A per-origin limit above the pool's soft cap can never be
  // reached, so it is rejected instead of clamped: a caller that asked for 4096
  // must not be told nothing and silently get 256.
  if (config->max_sockets_per_group > kMaxSocketsPerGroupLimit) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  options.max_sockets_per_group = config->max_sockets_per_group;
  options.inject_client_hints = config->inject_client_hints != 0;
  // 0 is Chrome's own default semantics, which the Core already has. 1..3
  // would be a policy engine of our own.
  if (config->cookie_samesite_override != 0) {
    return MN_ERROR_INVALID_ARGUMENT;
  }

  scoped_refptr<minicronet::Engine> impl = minicronet::Engine::Create(
      std::move(user_agent), std::move(accept_language),
      std::move(proxy_config),
      std::move(proxy_username), std::move(proxy_password),
      config->http_cache_mode == MN_HTTP_CACHE_ENABLED, config->protocol_mode,
      config->tls_verify_mode, std::move(custom_ca_pem),
      std::move(profile), std::move(options));
  if (!impl) {
    return MN_ERROR_INITIALIZATION_FAILED;
  }

  auto *engine = new (std::nothrow) mn_engine;
  if (!engine) {
    return MN_ERROR_OUT_OF_MEMORY;
  }
  engine->impl = std::move(impl);
  *out_engine = engine;
  return MN_OK;
}

void MN_CALL mn_engine_retain(mn_engine_t *engine) {
  if (engine) {
    Retain(engine);
  }
}

void MN_CALL mn_engine_release(mn_engine_t *engine) {
  if (!engine) {
    return;
  }
  if (engine->refs.fetch_sub(1, std::memory_order_acq_rel) == 1) {
    delete engine;
  }
}

mn_result_t MN_CALL mn_engine_cookies_get(mn_engine_t *engine, const char *url,
                                          size_t url_length,
                                          mn_cookie_t *out_cookies,
                                          size_t capacity, size_t *out_count) {
  if (!engine || (url_length && !url) || (capacity && !out_cookies)) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  std::string url_text;
  if (url) {
    url_text.assign(url, url_length);
    if (url_text.find('\0') != std::string::npos) {
      return MN_ERROR_INVALID_ARGUMENT;
    }
  }
  // Filled directly into storage owned by the handle, never into a local: the
  // ABI promises a cookie's bytes are readable while the caller holds the
  // result, and a value on this stack frame dies with the call.
  minicronet::CookieView &view = engine->cookie_storage;
  const mn_result_t result =
      engine->impl->GetCookies(url ? &url_text : nullptr, &view);
  if (result != MN_OK) {
    return result;
  }
  if (out_count) {
    *out_count = view.cookies.size();
  }
  // capacity == 0 is how a caller asks for the count alone, so it succeeds
  // with whatever the store holds. A capacity that is too small for a
  // non-zero request is a sizing error the caller has to fix, and it is only
  // reported after the count is written so one call is enough to find out how
  // many entries are needed.
  if (capacity == 0) {
    return MN_OK;
  }
  if (view.cookies.size() > capacity) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  // The entries already point into engine->cookie_storage, so copying the
  // structs copies valid pointers.
  auto destination = UNSAFE_BUFFERS(base::span(out_cookies, capacity));
  std::copy(view.cookies.begin(), view.cookies.end(), destination.begin());
  return MN_OK;
}

mn_result_t MN_CALL mn_engine_cookies_set(mn_engine_t *engine,
                                          const mn_cookie_t *cookie,
                                          const char *source_url,
                                          size_t source_url_length,
                                          int64_t creation_unix_seconds) {
  if (!engine || !cookie || !source_url ||
      cookie->size != sizeof(mn_cookie_t) ||
      cookie->version != MN_ABI_VERSION) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  std::string source(source_url, source_url_length);
  if (source.find('\0') != std::string::npos) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  return engine->impl->SetCookie(*cookie, source, creation_unix_seconds);
}

mn_result_t MN_CALL mn_engine_cookies_delete(mn_engine_t *engine,
                                             const char *domain,
                                             size_t domain_length,
                                             const char *path, size_t path_length,
                                             const char *name, size_t name_length,
                                             size_t *out_removed) {
  if (!engine || (domain_length && !domain) || (path_length && !path) ||
      (name_length && !name)) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  const std::string domain_text(domain ? domain : "", domain_length);
  const std::string path_text(path ? path : "", path_length);
  const std::string name_text(name ? name : "", name_length);
  if (domain_text.find('\0') != std::string::npos ||
      path_text.find('\0') != std::string::npos ||
      name_text.find('\0') != std::string::npos) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  size_t removed = 0;
  const mn_result_t result = engine->impl->DeleteCookie(
      domain_text, path_text, name_text, &removed);
  if (result == MN_OK && out_removed) {
    *out_removed = removed;
  }
  return result;
}

mn_result_t MN_CALL mn_engine_cookies_clear(mn_engine_t *engine) {
  return engine ? engine->impl->ClearCookies() : MN_ERROR_INVALID_ARGUMENT;
}

mn_result_t MN_CALL mn_engine_set_client_certificate(
    mn_engine_t *engine, const char *host, size_t host_length, uint16_t port,
    const uint8_t *cert_pem, size_t cert_pem_length, const uint8_t *key_pem,
    size_t key_pem_length) {
  if (!engine || !host || host_length == 0 ||
      (cert_pem_length && !cert_pem) || (key_pem_length && !key_pem)) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  std::string host_text(host, host_length);
  if (host_text.find('\0') != std::string::npos) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  std::vector<uint8_t> cert;
  if (cert_pem_length) {
    auto span = UNSAFE_BUFFERS(base::span(cert_pem, cert_pem_length));
    cert.assign(span.begin(), span.end());
  }
  std::vector<uint8_t> key;
  if (key_pem_length) {
    auto span = UNSAFE_BUFFERS(base::span(key_pem, key_pem_length));
    key.assign(span.begin(), span.end());
  }
  return engine->impl->SetClientCertificate(host_text, port, cert, key);
}

mn_result_t MN_CALL mn_engine_clear_client_certificate(mn_engine_t *engine,
                                                       const char *host,
                                                       size_t host_length,
                                                       uint16_t port) {
  if (!engine || !host || host_length == 0) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  std::string host_text(host, host_length);
  if (host_text.find('\0') != std::string::npos) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  return engine->impl->ClearClientCertificate(host_text, port);
}

mn_result_t MN_CALL mn_request_create(mn_engine_t *engine,
                                      const mn_request_config_t *config,
                                      mn_request_t **out_request) {
  if (!engine || !config || !out_request) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  *out_request = nullptr;
  // size first: it is the only field that proves the caller allocated a struct
  // large enough to hold callbacks, which starts at offset 64. Reading
  // callbacks.version before this check read past the end of a shorter, older
  // caller's allocation. Kept separate from the version check rather than
  // folded into a single disjunction, because a folded chain evaluates size and
  // callbacks.version in written order and is exactly how this got inverted.
  if (config->size != sizeof(mn_request_config_t)) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  if (config->version != MN_ABI_VERSION ||
      config->callbacks.version != MN_ABI_VERSION) {
    return MN_ERROR_UNSUPPORTED_ABI;
  }
  if (!config->url || config->url_length == 0 || !config->method ||
      config->method_length == 0 ||
      config->callbacks.size != sizeof(mn_request_callbacks_t) ||
      (config->header_count > 0 && !config->headers)) {
    return MN_ERROR_INVALID_ARGUMENT;
  }

  mn_request_callbacks_t callbacks = config->callbacks;
  if (!callbacks.on_complete) {
    return MN_ERROR_INVALID_ARGUMENT;
  }

  mn_upload_mode_t upload_mode = MN_UPLOAD_NONE;
  mn_cache_mode_t cache_mode = MN_CACHE_DEFAULT;
  mn_redirect_mode_t redirect_mode = MN_REDIRECT_FOLLOW;
  std::vector<uint8_t> body;
  upload_mode = config->upload_mode;
  cache_mode = config->cache_mode;
  redirect_mode = config->redirect_mode;
  if (upload_mode < MN_UPLOAD_NONE || upload_mode > MN_UPLOAD_CHUNKED ||
      cache_mode < MN_CACHE_DEFAULT || cache_mode > MN_CACHE_ONLY_IF_CACHED ||
      redirect_mode < MN_REDIRECT_FOLLOW || redirect_mode > MN_REDIRECT_ERROR ||
      config->priority < MN_REQUEST_PRIORITY_DEFAULT ||
      config->priority > MN_REQUEST_PRIORITY_IDLE ||
      (upload_mode != MN_UPLOAD_FIXED && config->body_length != 0) ||
      (config->body_length != 0 && !config->body) ||
      config->strip_authorization_on_cross_origin_redirect > 1 ||
      (redirect_mode == MN_REDIRECT_MANUAL && !callbacks.on_redirect)) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  if (upload_mode == MN_UPLOAD_FIXED && config->body_length != 0) {
    if (config->body_length > kMaxUploadBodyLength) {
      return MN_ERROR_INVALID_ARGUMENT;
    }
    auto input_body =
        UNSAFE_BUFFERS(base::span(config->body, config->body_length));
    body.assign(input_body.begin(), input_body.end());
  }

  if (config->url_length > kMaxUrlLength ||
      config->method_length > kMaxHeaderFieldLength) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  std::string url(config->url, config->url_length);
  std::string method(config->method, config->method_length);
  if (url.find('\0') != std::string::npos ||
      !net::HttpUtil::IsValidHeaderName(method)) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  // Chromium canonicalizes the URL here, which needs the embedded ICU dataset
  // for an internationalized hostname. Engine creation already failed if ICU is
  // unavailable, so this cannot reach the canonicalizer unprepared.
  if (!GURL(url).is_valid()) {
    return MN_ERROR_INVALID_ARGUMENT;
  }

  // Headers are validated before anything is allocated. They used to be parsed
  // after the mn_request wrapper was created, which forced two hand-written
  // "delete request" early returns: the only place in this file where the
  // out-param invariant was maintained by hand rather than by falling through
  // to a single exit.
  if (config->header_count > kMaxHeaderCount) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  std::vector<std::pair<std::string, std::string>> headers;
  headers.reserve(config->header_count);
  auto input_headers =
      UNSAFE_BUFFERS(base::span(config->headers, config->header_count));
  for (const mn_header_t &header : input_headers) {
    if (!header.name || !header.value || header.name_length == 0) {
      return MN_ERROR_INVALID_ARGUMENT;
    }
    if (header.name_length > kMaxHeaderFieldLength ||
        header.value_length > kMaxHeaderFieldLength) {
      return MN_ERROR_INVALID_ARGUMENT;
    }
    std::string name(header.name, header.name_length);
    std::string value(header.value, header.value_length);
    if (!net::HttpUtil::IsValidHeaderName(name) ||
        !net::HttpUtil::IsValidHeaderValue(value)) {
      return MN_ERROR_INVALID_ARGUMENT;
    }
    headers.emplace_back(std::move(name), std::move(value));
  }

  auto *request = new (std::nothrow) mn_request;
  if (!request) {
    return MN_ERROR_OUT_OF_MEMORY;
  }

  request->impl = base::MakeRefCounted<minicronet::Request>(
      engine->impl, std::move(url), std::move(method), std::move(headers),
      std::move(body), upload_mode, cache_mode, redirect_mode,
      config->priority, callbacks, config->timeout_ms, request);
  request->impl->set_strip_auth_on_cross_origin_redirect(
      config->strip_authorization_on_cross_origin_redirect != 0);
  *out_request = request;
  return MN_OK;
}

void MN_CALL mn_request_retain(mn_request_t *request) {
  if (request) {
    Retain(request);
  }
}

void MN_CALL mn_request_release(mn_request_t *request) {
  if (!request) {
    return;
  }
  if (request->refs.fetch_sub(1, std::memory_order_acq_rel) == 1) {
    delete request;
  }
}

mn_result_t MN_CALL mn_request_start(mn_request_t *request) {
  if (!request) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  mn_request_retain(request);
  mn_result_t result = request->impl->Start();
  if (result != MN_OK) {
    mn_request_release(request);
  }
  return result;
}

mn_result_t MN_CALL mn_request_cancel(mn_request_t *request) {
  return request ? request->impl->Cancel() : MN_ERROR_INVALID_ARGUMENT;
}

mn_result_t MN_CALL mn_request_upload_write(mn_request_t *request,
                                            const uint8_t *data,
                                            size_t data_length,
                                            int final_chunk) {
  if (!request || (data_length != 0 && !data) ||
      (final_chunk != 0 && final_chunk != 1)) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  std::vector<uint8_t> body;
  if (data_length != 0) {
    auto input = UNSAFE_BUFFERS(base::span(data, data_length));
    body.assign(input.begin(), input.end());
  }
  return request->impl->UploadWrite(std::move(body), final_chunk != 0);
}

mn_result_t MN_CALL mn_request_follow_redirect(mn_request_t *request) {
  return request ? request->impl->FollowRedirect() : MN_ERROR_INVALID_ARGUMENT;
}

mn_result_t MN_CALL mn_request_release_buffer(mn_request_t *request,
                                             const uint8_t *data) {
  if (!request) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  return request->impl->ReleaseBuffer(data);
}

mn_result_t MN_CALL mn_request_resume_read(mn_request_t *request) {
  return request ? request->impl->ResumeRead() : MN_ERROR_INVALID_ARGUMENT;
}

mn_result_t MN_CALL mn_request_negotiated_protocol(
    mn_request_t *request, mn_http_version_t *out_version) {
  if (!request || !out_version) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  return request->impl->NegotiatedProtocol(out_version);
}

mn_result_t MN_CALL mn_request_ech_status(mn_request_t *request,
                                          mn_ech_status_t *out_status) {
  if (!request || !out_status) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  return request->impl->EchStatus(out_status);
}

mn_result_t MN_CALL mn_websocket_create(mn_engine_t *engine,
                                        const mn_websocket_config_t *config,
                                        mn_websocket_t **out_websocket) {
  if (!engine || !config || !out_websocket) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  *out_websocket = nullptr;
  // size first, for the same reason as mn_request_create: callbacks starts at
  // offset 72 here, so reading callbacks.version before proving the allocation
  // is long enough reads past the end of a shorter caller's struct.
  if (config->size != sizeof(mn_websocket_config_t)) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  if (config->version != MN_ABI_VERSION ||
      config->callbacks.version != MN_ABI_VERSION) {
    return MN_ERROR_UNSUPPORTED_ABI;
  }
  if (!config->url || config->url_length == 0 || !config->origin ||
      config->origin_length == 0 ||
      config->callbacks.size != sizeof(mn_websocket_callbacks_t) ||
      !config->callbacks.on_failure ||
      (config->protocol_count > 0 && !config->protocols) ||
      (config->header_count > 0 && !config->headers)) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  if (config->url_length > kMaxUrlLength ||
      config->origin_length > kMaxUrlLength) {
    return MN_ERROR_INVALID_ARGUMENT;
  }

  std::string url(config->url, config->url_length);
  std::string origin(config->origin, config->origin_length);
  if (url.find('\0') != std::string::npos ||
      origin.find('\0') != std::string::npos) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  GURL parsed_url(url);
  GURL parsed_origin(origin);
  if (!parsed_url.is_valid() || !parsed_url.SchemeIsWSOrWSS() ||
      parsed_url.has_ref() || !parsed_origin.is_valid() ||
      url::Origin::Create(parsed_origin).opaque()) {
    return MN_ERROR_INVALID_ARGUMENT;
  }

  if (config->protocol_count > kMaxWebSocketProtocolCount) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  std::vector<std::string> protocols;
  protocols.reserve(config->protocol_count);
  auto input_protocols =
      UNSAFE_BUFFERS(base::span(config->protocols, config->protocol_count));
  for (const mn_string_t &protocol : input_protocols) {
    if (!protocol.data || protocol.length == 0) {
      return MN_ERROR_INVALID_ARGUMENT;
    }
    std::string value(protocol.data, protocol.length);
    if (!net::HttpUtil::IsValidHeaderName(value)) {
      return MN_ERROR_INVALID_ARGUMENT;
    }
    if (std::ranges::find(protocols, value) != protocols.end()) {
      return MN_ERROR_INVALID_ARGUMENT;
    }
    protocols.push_back(std::move(value));
  }

  if (config->header_count > kMaxHeaderCount) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  std::vector<std::pair<std::string, std::string>> headers;
  headers.reserve(config->header_count);
  auto input_headers =
      UNSAFE_BUFFERS(base::span(config->headers, config->header_count));
  for (const mn_header_t &header : input_headers) {
    if (!header.name || !header.value || header.name_length == 0) {
      return MN_ERROR_INVALID_ARGUMENT;
    }
    if (header.name_length > kMaxHeaderFieldLength ||
        header.value_length > kMaxHeaderFieldLength) {
      return MN_ERROR_INVALID_ARGUMENT;
    }
    std::string name(header.name, header.name_length);
    std::string value(header.value, header.value_length);
    if (!net::HttpUtil::IsValidHeaderName(name) ||
        !net::HttpUtil::IsValidHeaderValue(value) ||
        IsForbiddenWebSocketHeader(name)) {
      return MN_ERROR_INVALID_ARGUMENT;
    }
    headers.emplace_back(std::move(name), std::move(value));
  }

  auto *websocket = new (std::nothrow) mn_websocket;
  if (!websocket) {
    return MN_ERROR_OUT_OF_MEMORY;
  }
  websocket->impl = base::MakeRefCounted<minicronet::WebSocket>(
      engine->impl, std::move(url), std::move(origin), std::move(protocols),
      std::move(headers), config->callbacks, config->timeout_ms, websocket);
  *out_websocket = websocket;
  return MN_OK;
}

void MN_CALL mn_websocket_retain(mn_websocket_t *websocket) {
  if (websocket) {
    Retain(websocket);
  }
}

void MN_CALL mn_websocket_release(mn_websocket_t *websocket) {
  if (!websocket) {
    return;
  }
  if (websocket->refs.fetch_sub(1, std::memory_order_acq_rel) == 1) {
    delete websocket;
  }
}

mn_result_t MN_CALL mn_websocket_start(mn_websocket_t *websocket) {
  if (!websocket) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  mn_websocket_retain(websocket);
  mn_result_t result = websocket->impl->Start();
  if (result != MN_OK) {
    mn_websocket_release(websocket);
  }
  return result;
}

mn_result_t MN_CALL mn_websocket_send(mn_websocket_t *websocket,
                                      mn_websocket_message_type_t type,
                                      const uint8_t *data, size_t data_length) {
  if (!websocket || (data_length > 0 && !data) || data_length > INT_MAX ||
      (type != MN_WEBSOCKET_MESSAGE_TEXT &&
       type != MN_WEBSOCKET_MESSAGE_BINARY)) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  std::vector<uint8_t> bytes;
  if (data_length > 0) {
    auto input = UNSAFE_BUFFERS(base::span(data, data_length));
    bytes.assign(input.begin(), input.end());
  }
  if (type == MN_WEBSOCKET_MESSAGE_TEXT &&
      !base::StreamingUtf8Validator::Validate(
          std::string(bytes.begin(), bytes.end()))) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  return websocket->impl->Send(type, std::move(bytes));
}

mn_result_t MN_CALL mn_websocket_close(mn_websocket_t *websocket, uint16_t code,
                                       const char *reason,
                                       size_t reason_length) {
  if (!websocket || (reason_length > 0 && !reason) || reason_length > 123 ||
      !IsValidWebSocketCloseCode(code)) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  std::string close_reason;
  if (reason_length > 0) {
    close_reason.assign(reason, reason_length);
  }
  if (!base::StreamingUtf8Validator::Validate(close_reason)) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  return websocket->impl->Close(code, std::move(close_reason));
}

mn_result_t MN_CALL mn_websocket_cancel(mn_websocket_t *websocket) {
  return websocket ? websocket->impl->Cancel() : MN_ERROR_INVALID_ARGUMENT;
}
