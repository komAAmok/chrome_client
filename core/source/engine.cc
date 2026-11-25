#include "minicronet/engine.h"
#include "minicronet/profile_ssl_config_service.h"
#include "platform_identity.h"

#include "build/build_config.h"

#include <cstdlib>
#include <string_view>
#include <utility>

#if BUILDFLAG(IS_POSIX)
#include <pthread.h>
#include <signal.h>
#endif  // BUILDFLAG(IS_POSIX)

#include "base/at_exit.h"
#include "base/command_line.h"
#include "base/feature_list.h"
#if defined(MINICRONET_PROFILE_VERIFICATION)
#include "base/files/file.h"
#include "base/files/file_path.h"
#endif
#include "base/functional/bind.h"
#include "base/i18n/icu_util.h"
#include "base/message_loop/message_pump_type.h"
#include "base/run_loop.h"
#include "base/no_destructor.h"
#include "base/synchronization/lock.h"
#include "base/synchronization/waitable_event.h"
#include "base/thread_annotations.h"
#include "base/time/time.h"
#include "base/task/thread_pool/thread_pool_instance.h"
#include "base/threading/thread.h"
#include "net/base/ip_address.h"
#include "net/base/ip_endpoint.h"
#include "net/base/network_change_notifier.h"
#include "net/cert/caching_cert_verifier.h"
#include "net/cert/coalescing_cert_verifier.h"
#include "net/cert/cert_verifier.h"
#include "net/cert/cert_verify_result.h"
#include "net/cert/x509_util.h"
#include "net/http/http_network_session.h"
#include "net/http/http_transaction_factory.h"
#include "net/net_buildflags.h"
#include "net/proxy_resolution/proxy_config_service_fixed.h"
#include "net/proxy_resolution/proxy_config_with_annotation.h"
#include "net/quic/quic_context.h"
#include "net/third_party/quiche/src/quiche/quic/core/crypto/crypto_protocol.h"
#if defined(MINICRONET_PROFILE_VERIFICATION)
#include "net/base/host_port_pair.h"
#include "net/base/proxy_chain.h"
#include "net/base/proxy_server.h"
#include "net/socket/client_socket_pool.h"
#include "net/socket/socket_pool_additional_capacity.h"
#include "net/socket/ssl_client_socket.h"
#include "net/ssl/ssl_key_logger_impl.h"
#endif
#if defined(MINICRONET_STATE_SEQUENCE_PROBE)
#include "net/cert/cert_verifier.h"
#include "net/cert/cert_verify_result.h"
#endif
#include "net/url_request/url_request_context.h"
#include "net/url_request/url_request_context_builder.h"
#include <deque>

#include "base/strings/string_split.h"
#include "net/base/host_mapping_rules.h"
#include "net/cert/x509_certificate.h"
#include "net/cookies/canonical_cookie.h"
#include "net/cookies/cookie_access_result.h"
#include "net/cookies/cookie_options.h"
#include "net/cookies/cookie_store.h"
#include "net/cookies/cookie_util.h"
#include "net/dns/public/dns_config_overrides.h"
#include "net/dns/public/dns_over_https_config.h"
#include "net/dns/public/insecure_dns_mode.h"
#include "net/dns/public/secure_dns_mode.h"
#include "net/ssl/openssl_private_key.h"
#include "third_party/boringssl/src/include/openssl/bio.h"
#include "third_party/boringssl/src/include/openssl/evp.h"
#include "third_party/boringssl/src/include/openssl/pem.h"
#include "third_party/boringssl/src/include/openssl/x509.h"
#include "third_party/boringssl/src/pki/pem.h"
#include "url/gurl.h"
#include "url/scheme_host_port.h"

namespace minicronet {
namespace {

// Applies the caller's protocol pin to the session params. Both the
// historical-profile path and the chromium_current path need exactly this
// mapping, and it used to be written out twice, once per path. The two copies
// had already started to disagree: the pin was applied only inside the profile
// branch, so http_version=v2/v3 was a silent no-op on the default profile while
// the same pin failed closed on a historical one. One function means the two
// paths cannot diverge again as modes are added.
void ApplyProtocolMode(mn_protocol_mode_t mode,
                       net::HttpNetworkSessionParams* params) {
  switch (mode) {
    case MN_PROTOCOL_FORCE_H1:
      params->protocol_mode = net::HttpProtocolMode::kHttp11;
      params->enable_http2 = false;
      params->enable_quic = false;
      break;
    case MN_PROTOCOL_FORCE_H2:
      params->protocol_mode = net::HttpProtocolMode::kHttp2;
      params->enable_http2 = true;
      params->enable_quic = false;
      break;
    case MN_PROTOCOL_FORCE_H3:
      params->protocol_mode = net::HttpProtocolMode::kHttp3;
      params->enable_http2 = false;
      params->enable_quic = true;
      break;
    case MN_PROTOCOL_NATIVE:
      break;
  }
}

class InsecureCertVerifier final : public net::CertVerifier {
 public:
  int Verify(const RequestParams& params, net::CertVerifyResult* result,
             net::CompletionOnceCallback callback,
             std::unique_ptr<Request>* out_req,
             const net::NetLogWithSource&) override {
    result->Reset();
    result->verified_cert = params.certificate();
    result->is_issued_by_known_root = true;
    return net::OK;
  }
  void Verify2QwacBinding(
      const std::string&, const std::string&,
      const scoped_refptr<net::X509Certificate>&,
      base::OnceCallback<void(const scoped_refptr<net::X509Certificate>&)> cb,
      const net::NetLogWithSource&) override {
    std::move(cb).Run(nullptr);
  }
  void SetConfig(const Config&) override {}
  void AddObserver(Observer*) override {}
  void RemoveObserver(Observer*) override {}
};

bool ParseCustomCaPem(std::string_view pem,
                      bssl::ParsedCertificateList* anchors_out) {
  constexpr std::string_view kBegin = "-----BEGIN CERTIFICATE-----";
  size_t declared = 0;
  for (size_t pos = 0; (pos = pem.find(kBegin, pos)) != std::string_view::npos;
       pos += kBegin.size()) {
    ++declared;
  }
  auto certs = net::X509Certificate::CreateCertificateListFromBytes(
      base::as_byte_span(pem), net::X509Certificate::FORMAT_PEM_CERT_SEQUENCE);
  auto anchors = net::x509_util::ParseAllValidCerts(certs);
  if (declared == 0 || declared != certs.size() ||
      anchors.size() != certs.size()) {
    return false;
  }
  *anchors_out = std::move(anchors);
  return true;
}

std::unique_ptr<net::CertVerifier> MakeCustomCaVerifier(
    std::string_view pem) {
  bssl::ParsedCertificateList anchors;
  if (!ParseCustomCaPem(pem, &anchors)) {
    return nullptr;
  }
  auto verifier = net::CertVerifier::CreateDefaultWithoutCaching(nullptr);
  net::CertVerifyProc::InstanceParams instance_params;
  instance_params.additional_trust_anchors = std::move(anchors);
  verifier->UpdateVerifyProcData(nullptr, {}, instance_params);
  return std::make_unique<net::CachingCertVerifier>(
      std::make_unique<net::CoalescingCertVerifier>(std::move(verifier)));
}

constexpr net::NetworkTrafficAnnotationTag kProxyTrafficAnnotation =
    net::DefineNetworkTrafficAnnotation("minicronet_proxy_config", R"(
      semantics {
        sender: "MiniCronet"
        description: "Proxy selected explicitly by the embedding app."
        trigger: "The embedding app creates a MiniCronet engine."
        data: "App-provided proxy host and destination network traffic."
        destination: OTHER
      }
      policy {
        cookies_allowed: NO
        setting: "Controlled by the embedding application."
        policy_exception_justification: "Not a Chrome feature."
      })");

#if defined(MINICRONET_PROFILE_VERIFICATION)
class LocalProbeCertVerifier final : public net::CertVerifier {
 public:
  int Verify(const RequestParams& params,
             net::CertVerifyResult* verify_result,
             net::CompletionOnceCallback callback,
             std::unique_ptr<Request>* out_req,
             const net::NetLogWithSource& net_log) override {
    verify_result->Reset();
    verify_result->verified_cert = params.certificate();
    verify_result->is_issued_by_known_root = true;
    return net::OK;
  }

  void Verify2QwacBinding(
      const std::string& binding,
      const std::string& hostname,
      const scoped_refptr<net::X509Certificate>& tls_cert,
      base::OnceCallback<void(const scoped_refptr<net::X509Certificate>&)>
          callback,
      const net::NetLogWithSource& net_log) override {
    std::move(callback).Run(nullptr);
  }

  void SetConfig(const Config& config) override {}
  void AddObserver(Observer* observer) override {}
  void RemoveObserver(Observer* observer) override {}
};
#endif

class Runtime {
public:
  Runtime() {
    if (!base::CommandLine::InitializedForCurrentProcess()) {
      base::CommandLine::Init(0, nullptr);
    }
    if (!base::FeatureList::GetInstance()) {
      base::FeatureList::InitInstance({}, {});
    }
    if (!base::ThreadPoolInstance::Get()) {
      base::ThreadPoolInstance::CreateAndStartWithDefaultParams("minicronet");
    }
    // The IDNA-only dataset is linked into this library, so this only wires up
    // the already-present data. Without it Chromium's URL canonicalizer aborts
    // the process on any internationalized hostname, so a failure here has to
    // fail Engine creation rather than be ignored.
    icu_ready_ = base::i18n::InitializeICU();
#if defined(MINICRONET_PROFILE_VERIFICATION)
    if (const char *value = std::getenv("MINICRONET_SSL_KEY_LOG_FILE")) {
#if BUILDFLAG(IS_WIN)
      base::FilePath path = base::FilePath::FromUTF8Unsafe(value);
#else
      base::FilePath path(value);
#endif
      base::File file(path,
                      base::File::FLAG_OPEN_ALWAYS | base::File::FLAG_APPEND);
      if (file.IsValid()) {
        net::SSLClientSocket::SetSSLKeyLogger(
            std::make_unique<net::SSLKeyLoggerImpl>(std::move(file)));
      }
    }
#endif

    base::Thread::Options options;
    options.message_pump_type = base::MessagePumpType::IO;
    CHECK(network_thread_.StartWithOptions(std::move(options)));

    base::WaitableEvent initialized;
    network_thread_.task_runner()->PostTask(
        FROM_HERE, base::BindOnce(
                       [](Runtime *runtime, base::WaitableEvent *event) {
                         runtime->network_change_notifier_ =
                             net::NetworkChangeNotifier::CreateIfNeeded();
                         event->Signal();
                       },
                       base::Unretained(this), base::Unretained(&initialized)));
    initialized.Wait();
  }

  scoped_refptr<base::SingleThreadTaskRunner> task_runner() const {
    return network_thread_.task_runner();
  }

  bool icu_ready() const { return icu_ready_; }

private:
  base::AtExitManager at_exit_manager_;
  bool icu_ready_ = false;
  base::Thread network_thread_{"MiniCronetNet"};
  std::unique_ptr<net::NetworkChangeNotifier> network_change_notifier_;
};

// ---------------------------------------------------------------------------
// Runtime lifetime and fork safety
// ---------------------------------------------------------------------------
//
// The Runtime used to live in a function-local ``base::NoDestructor<Runtime>``.
// That is a C++ magic static: the compiler emits a guard variable, initialises
// the object once, and provides no way to reset it.  After fork() the child
// inherits the guard as "already initialised" and therefore keeps using the
// parent's Runtime -- whose ``network_thread_`` no longer exists, because
// fork() only carries the calling thread across.  Every ``PostTask`` + ``Wait``
// on that thread then blocks forever, and ``~Engine`` does exactly that, so the
// child hangs on its first request (or even on its first ``mn_engine_create``,
// which reaches ``Engine::Start``).
//
// Chromium's own process-wide singletons have the same problem and cannot be
// rebuilt from here:
//   * ``base::ThreadPoolInstance`` exposes no Destroy(); only ``Set()``, and
//     ``Set()`` deletes the previous instance, joining workers that are gone.
//   * ``base::i18n::InitializeICU()``, ``base::FeatureList``,
//     ``base::CommandLine`` and ``net::NetworkChangeNotifier`` are all
//     process-lifetime as well.
// A child that inherited an initialised Runtime therefore cannot be repaired.
// It can only be told to stop instead of deadlocking.  A child that forked
// *before* the Runtime was created is unaffected: it builds its own on first
// use.
//
// Hence the object is an explicit pointer guarded by a mutex we are able to
// release in the child: the pointer is dropped there, and the mutex is unlocked
// (POSIX-sanctioned, because the forking thread held it across the fork).

// Deliberately a raw pthread mutex rather than base::Lock on POSIX: the atfork
// child handler has to unlock it, and base::Lock performs thread-ownership
// DCHECKs under DCHECK_IS_ON(), which is not async-signal-safe.
#if BUILDFLAG(IS_POSIX)
pthread_mutex_t g_runtime_mutex = PTHREAD_MUTEX_INITIALIZER;
#else
// Windows has no fork(), so the atfork handlers below do not exist and the
// mutex never has to be released from a signal-adjacent context.  base::Lock is
// fine here, but it must not be a plain global: that would add an exit-time
// destructor, which Chromium builds with -Wexit-time-destructors.  Hiding it
// behind NoDestructor is the idiom base itself uses (see base/logging.cc).
base::NoDestructor<base::Lock> g_runtime_mutex;
#endif  // BUILDFLAG(IS_POSIX)

// Guarded by |g_runtime_mutex|.
Runtime *g_runtime = nullptr;

#if BUILDFLAG(IS_POSIX)
// Bumped by the atfork child handler, so objects created before a fork can be
// told apart from objects created after one.
volatile sig_atomic_t g_fork_generation = 0;
// Set by the atfork child handler when the parent had already created the
// Runtime (see above).  Such a child cannot serve requests; failing fast beats
// hanging forever.
volatile sig_atomic_t g_fork_child_unusable = 0;

void RuntimeMutexAcquire() {
  pthread_mutex_lock(&g_runtime_mutex);
}

void RuntimeMutexRelease() {
  pthread_mutex_unlock(&g_runtime_mutex);
}
#else
// base::Lock is LOCKABLE, so the analyzer needs to see that these two halves
// form an acquire/release pair even though they cross a function boundary.
// The annotations are on the definitions because these are local helpers.
EXCLUSIVE_LOCK_FUNCTION(g_runtime_mutex)
void RuntimeMutexAcquire() {
  g_runtime_mutex->Acquire();
}

UNLOCK_FUNCTION(g_runtime_mutex)
void RuntimeMutexRelease() {
  g_runtime_mutex->Release();
}
#endif  // BUILDFLAG(IS_POSIX)

Runtime &GetRuntime() {
  RuntimeMutexAcquire();
  if (!g_runtime) {
    g_runtime = new Runtime();
  }
  Runtime *runtime = g_runtime;
  RuntimeMutexRelease();
  return *runtime;
}

int CurrentForkGeneration() {
#if BUILDFLAG(IS_POSIX)
  return static_cast<int>(g_fork_generation);
#else
  return 0;
#endif  // BUILDFLAG(IS_POSIX)
}

bool IsRuntimeUnusableAfterFork() {
#if BUILDFLAG(IS_POSIX)
  return g_fork_child_unusable != 0;
#else
  return false;  // No fork() on Windows.
#endif  // BUILDFLAG(IS_POSIX)
}

#if BUILDFLAG(IS_POSIX)
// --- atfork handlers -------------------------------------------------------
//
// Only async-signal-safe work happens below: plain atomic-ish stores, pointer
// assignment and pthread_mutex_unlock().  No malloc/new, no logging, no STL.
//
// Lock order: this library owns exactly one lock (|g_runtime_mutex|) and the
// prepare handler takes only that one, so there is no ordering relationship
// that another library's atfork handler could violate.  Registration happens
// during static initialisation (see the registrar below), i.e. at library load
// time, which is deterministic and precedes every mn_engine_create() call.

void ForkPrepare() {
  // Keep other threads out of Runtime creation/replacement while the process
  // image is being copied; a thread frozen mid-construction would leave the
  // child with an inconsistent object.
  RuntimeMutexAcquire();
}

void ForkParent() {
  RuntimeMutexRelease();
}

void ForkChild() {
  // Written as an assignment rather than ``++``: incrementing a volatile is
  // deprecated in C++20 (and this translation unit builds with -Werror).
  g_fork_generation = g_fork_generation + 1;
  if (g_runtime) {
    // The Runtime -- and every Chromium singleton it initialised -- came from
    // the parent.  Its threads did not survive the fork and cannot be
    // restarted, so mark this child unusable and drop the reference.  The
    // object is intentionally leaked: the process has been told it cannot make
    // requests, so nothing will read it again.
    g_fork_child_unusable = 1;
    g_runtime = nullptr;
  }
  RuntimeMutexRelease();
}

class ForkHandlerRegistrar {
 public:
  ForkHandlerRegistrar() {
    pthread_atfork(&ForkPrepare, &ForkParent, &ForkChild);
  }
};

// Static initialisation: runs at library load, before any engine can exist.
const ForkHandlerRegistrar g_fork_handler_registrar;
#endif  // BUILDFLAG(IS_POSIX)

} // namespace

bool ValidateCustomCaPem(std::string_view pem) {
  bssl::ParsedCertificateList anchors;
  return ParseCustomCaPem(pem, &anchors);
}

scoped_refptr<Engine> Engine::Create(std::string user_agent,
                                     std::string accept_language,
                                     net::ProxyConfig proxy_config,
                                     std::string proxy_username,
                                     std::string proxy_password,
                                     bool http_cache_enabled,
                                     mn_protocol_mode_t protocol_mode,
                                     mn_tls_verify_mode_t tls_verify_mode,
                                     std::string custom_ca_pem,
                                     ProfileContext profile,
                                     EngineOptions options) {
  // A fork() child of a process that had already created the Runtime cannot be
  // served: Chromium's process-wide singletons died with the parent's threads
  // and have no rebuild path.  Fail here rather than block on a dead thread.
  if (IsRuntimeUnusableAfterFork()) {
    return nullptr;
  }
  if (!GetRuntime().icu_ready()) {
    return nullptr;
  }
  // Construct here so the private constructor remains part of the Core API
  // boundary while scoped_refptr still owns the object.
  scoped_refptr<Engine> engine(new Engine(
      std::move(user_agent), std::move(accept_language),
      std::move(proxy_config), std::move(proxy_username),
      std::move(proxy_password), http_cache_enabled, protocol_mode,
      tls_verify_mode, std::move(custom_ca_pem),
      std::move(profile), std::move(options)));
  return engine->Start() ? engine : nullptr;
}

scoped_refptr<base::SingleThreadTaskRunner> Engine::task_runner() const {
  return GetRuntime().task_runner();
}

Engine::Engine(std::string user_agent, std::string accept_language,
               net::ProxyConfig proxy_config, std::string proxy_username,
               std::string proxy_password, bool http_cache_enabled,
               mn_protocol_mode_t protocol_mode,
               mn_tls_verify_mode_t tls_verify_mode,
               std::string custom_ca_pem,
               ProfileContext profile, EngineOptions options)
    : user_agent_(std::move(user_agent)),
      accept_language_(std::move(accept_language)),
      proxy_config_(std::move(proxy_config)),
      proxy_username_(std::move(proxy_username)),
      proxy_password_(std::move(proxy_password)),
      http_cache_enabled_(http_cache_enabled), protocol_mode_(protocol_mode),
      tls_verify_mode_(tls_verify_mode), custom_ca_pem_(std::move(custom_ca_pem)),
      profile_(std::move(profile)), options_(std::move(options)),
      fork_generation_(CurrentForkGeneration()) {}

Engine::~Engine() {
  if (!context_) {
    return;
  }
#if BUILDFLAG(IS_POSIX)
  // This engine was created before a fork(): its network thread is gone, so
  // posting the shutdown task and waiting for it would block forever.  Abandon
  // the context instead.  Only a fork() child can observe a stale generation,
  // and such a child has already been marked unusable.
  if (fork_generation_ != CurrentForkGeneration()) {
    return;
  }
#endif  // BUILDFLAG(IS_POSIX)
  auto runner = GetRuntime().task_runner();
  if (runner->RunsTasksInCurrentSequence()) {
    ShutdownOnNetworkThread();
    return;
  }
  base::WaitableEvent stopped;
  runner->PostTask(FROM_HERE,
                   base::BindOnce(
                       [](Engine *engine, base::WaitableEvent *event) {
                         engine->ShutdownOnNetworkThread();
                         event->Signal();
                       },
                       base::Unretained(this), base::Unretained(&stopped)));
  stopped.Wait();
}

bool Engine::Start() {
  auto runner = GetRuntime().task_runner();
  if (runner->RunsTasksInCurrentSequence()) {
    InitializeOnNetworkThread();
    return context_ != nullptr;
  }
  base::WaitableEvent initialized;
  runner->PostTask(FROM_HERE,
                   base::BindOnce(
                       [](Engine *engine, base::WaitableEvent *event) {
                         engine->InitializeOnNetworkThread();
                         event->Signal();
                       },
                       base::Unretained(this), base::Unretained(&initialized)));
  initialized.Wait();
  return context_ != nullptr;
}

void Engine::InitializeOnNetworkThread() {
  // Every Engine owns one URLRequestContext. Chromium consequently gives this
  // immutable profile its own H2/H3 pools, TLS session cache, QUIC server
  // config cache, Alt-Svc HttpServerProperties and HttpCache partition.
  net::URLRequestContextBuilder builder;
  if (tls_verify_mode_ == MN_TLS_VERIFY_INSECURE) {
    builder.SetCertVerifier(std::make_unique<InsecureCertVerifier>());
  } else if (tls_verify_mode_ == MN_TLS_VERIFY_CUSTOM_CA) {
    auto verifier = MakeCustomCaVerifier(custom_ca_pem_);
    CHECK(verifier);
    builder.SetCertVerifier(std::move(verifier));
  }
#if defined(MINICRONET_STATE_SEQUENCE_PROBE)
  // The local HTTPS sequence probe uses Chromium's checked-in test
  // certificate. This verifier is never compiled into libminicronet.
  builder.SetCertVerifier(std::make_unique<LocalProbeCertVerifier>());
#elif defined(MINICRONET_PROFILE_VERIFICATION)
  // QUIC does not use HttpNetworkSession's TCP-only certificate-error bypass.
  // Require an explicit opt-in for local wire probes; release builds neither
  // compile this verifier nor accept this environment variable.
  if (std::getenv("MINICRONET_INSECURE_LOCAL_CERT")) {
    builder.SetCertVerifier(std::make_unique<LocalProbeCertVerifier>());
  }
#endif
  builder.set_user_agent(user_agent_);
  builder.set_accept_language(accept_language_);
  builder.set_enable_brotli(true);
  builder.set_enable_zstd(true);

  if (const RuntimeProfileData *profile = profile_.data()) {
    const NetworkFeatureFlags feature_flags = profile_.feature_flags();
    builder.set_ssl_config_service(
        std::make_unique<ProfileSSLConfigService>(*profile));
    auto quic_context = std::make_unique<net::QuicContext>();
    quic_context->params()->idle_connection_timeout = base::Seconds(30);
    quic_context->params()->migrate_sessions_on_network_change_v2 = false;
    quic_context->params()->use_new_alps_codepoint =
        profile->use_new_alps_codepoint;
    quic_context->params()->use_feature_quic_options = false;
    // Keep the historical attempt policy as explicit immutable profile data.
    // The feature snapshot remains a consistency guard, never a process-global
    // selector.
    CHECK_EQ(profile->try_quic_by_default,
             HasNetworkFeature(feature_flags,
                               NetworkFeature::kTryQuicByDefault));
    quic_context->params()->try_quic_by_default =
        profile->try_quic_by_default;
    if (protocol_mode_ == MN_PROTOCOL_FORCE_H3) {
      quic_context->params()->force_quic_everywhere = true;
    }
    // Upstream feature kMaintainConnectionsOnIpv6TempAddrChange only exists
    // from 142: it landed DISABLED and flipped to ENABLED at 148, where it
    // stays through the pinned revision. Verified per release against
    // net/base/features.cc at each tag. The profile table carries the
    // per-release value so a pinned older profile keeps the behaviour that
    // release actually had instead of inheriting today's default.
    quic_context->params()->maintain_ipv6_temp_addr =
        profile->maintain_ipv6_temp_addr;
    quic_context->params()->send_legacy_version_information =
        profile->send_quic_legacy_version_information;
    // HTTP/3 SETTINGS_H3_DATAGRAM (51). Captured boundary: absent on
    // chrome_93..chrome_119, present from chrome_120. The value is profile data,
    // never a FeatureList read, so a pinned older profile cannot inherit
    // today's default.
    quic_context->params()->advertise_h3_datagram =
        profile->advertise_h3_datagram;
    if (profile->send_quic_orig) {
      quic_context->params()->connection_options.push_back(quic::kORIG);
    }
#if defined(MINICRONET_PROFILE_VERIFICATION)
    if (const char *value = std::getenv("MINICRONET_FORCE_QUIC_ORIGIN")) {
      GURL origin(value);
      if (origin.is_valid() && origin.SchemeIs("https") &&
          origin.path() == "/" && !origin.has_query() && !origin.has_ref()) {
        quic_context->params()->origins_to_force_quic_on.insert(
            url::SchemeHostPort(origin));
      }
    }
#endif
    builder.set_quic_context(std::move(quic_context));
    net::HttpNetworkSessionParams session_params;
    session_params.enable_quic = profile->enable_quic;
    ApplyProtocolMode(protocol_mode_, &session_params);
    session_params.enable_tls13_early_data = false;
    session_params.enable_early_data = session_params.enable_tls13_early_data;
    session_params.enable_alps_for_http2 = HasNetworkFeature(
        feature_flags, NetworkFeature::kAlpsForHttp2);
    session_params.send_http2_enable_push_setting =
        profile->send_http2_enable_push_setting;
    session_params.randomize_socket_pool_limit =
        profile->randomize_socket_pool_limit;
    session_params.randomize_proxy_socket_pool_limit =
        profile->randomize_proxy_socket_pool_limit;
    // v11. Not profile data: every captured release leaves this 0, which keeps
    // Chromium's own 6-per-origin limit. A non-zero value is the caller asking
    // for a deviation from the imitated browser, documented on the ABI field.
    session_params.max_sockets_per_group_override =
        options_.max_sockets_per_group;
    session_params.send_priority_header = profile->send_priority_header;
    // HTTP/2 framing knobs. All three are false for every profile (see the
    // struct comment in the generated header): the 432 captured connections
    // carry no greased SETTINGS id, no reserved-type frame, and no DATA frame
    // on a bodyless request. They are written from profile data rather than
    // left at the C++ default because a browser build's default is not the
    // same claim as the evidence -- if a future Chromium flipped one, the
    // fingerprint would change silently.
    session_params.enable_http2_settings_grease =
        profile->enable_http2_settings_grease;
    session_params.http2_end_stream_with_data_frame =
        profile->http2_end_stream_with_data_frame;
    if (profile->greased_http2_frame) {
      // The frame contents are randomised by Chromium; only the presence is
      // profile data, and no captured profile has it. Enabling it without a
      // frame description would send nothing, so it stays a hard error to ask
      // for it until a capture shows what the release actually sent.
      return;
    }
    session_params.enable_websocket_over_http3 = false;
#if defined(MINICRONET_PROFILE_VERIFICATION)
    if (std::getenv("MINICRONET_FORCE_H3_WEBSOCKET")) {
      session_params.enable_websocket_over_http3 = true;
    }
#endif
    session_params.ignore_certificate_errors =
        tls_verify_mode_ == MN_TLS_VERIFY_INSECURE;
    const auto& h2 = GetH2RuntimeParams(profile->h2_params_index);
    session_params.spdy_session_max_recv_window_size =
        h2.session_recv_window_size;
    session_params.http2_settings[spdy::SETTINGS_HEADER_TABLE_SIZE] =
        h2.header_table_size;
    session_params.http2_settings[spdy::SETTINGS_INITIAL_WINDOW_SIZE] =
        h2.initial_window_size;
    session_params.http2_settings[spdy::SETTINGS_MAX_HEADER_LIST_SIZE] =
        h2.max_header_list_size;
    // MAX_FRAME_SIZE remains at the RFC default for all activated profiles;
    // Chromium omits default-valued settings from the initial frame.
    if (h2.send_max_frame_size) {
      session_params.http2_settings[spdy::SETTINGS_MAX_FRAME_SIZE] =
          h2.max_frame_size;
    }
    if (profile->send_http2_max_concurrent_streams) {
      session_params.http2_settings[spdy::SETTINGS_MAX_CONCURRENT_STREAMS] =
          1000;
    }
    builder.set_http_network_session_params(session_params);
  }
  if (!profile_.data()) {
    // The protocol pin is not profile data. chromium_current has no
    // RuntimeProfileData, so keeping the pin inside the profile block above made
    // http_version=v2/v3 a silent no-op on the default profile while the same
    // pin failed closed on a historical one. The ABI documents the pin as
    // unconditional (docs/COMPATIBILITY_BOUNDARY.md: "Forced H1/H2/H3 modes
    // fail closed and never fall back to another version"), and
    // HttpNetworkSessionParams is accepted by the builder whether or not a
    // profile is present, so there is nothing here the Core cannot honour.
    net::HttpNetworkSessionParams session_params;
    ApplyProtocolMode(protocol_mode_, &session_params);
    // Redundant with the SetCertVerifier call at the top of this function, but
    // keeps these session params identical to the historical-profile path.
    session_params.ignore_certificate_errors =
        tls_verify_mode_ == MN_TLS_VERIFY_INSECURE;
    // The pool override is the protocol pin's twin: it is not profile data,
    // so chromium_current must honour it exactly as a historical profile does.
    session_params.max_sockets_per_group_override =
        options_.max_sockets_per_group;
    // chromium_current keeps Chromium's own defaults for the three HTTP/2
    // framing knobs: that is what "current" means, and overriding them here
    // would make the default profile disagree with the Chromium it names.
    builder.set_http_network_session_params(session_params);

    // The historical-profile path also arms force_quic_everywhere for a forced
    // H3 pin. Without it the default profile would restrict ALPN to QUIC but
    // never actually try QUIC for a plain origin, so the same pin would behave
    // differently depending on which profile was selected. Only FORCE_H3 gets a
    // QuicContext here: MN_PROTOCOL_NATIVE must keep Chromium's own defaults,
    // which is what "current" means.
    if (protocol_mode_ == MN_PROTOCOL_FORCE_H3) {
      auto quic_context = std::make_unique<net::QuicContext>();
      quic_context->params()->force_quic_everywhere = true;
      quic_context->params()->idle_connection_timeout = base::Seconds(30);
      builder.set_quic_context(std::move(quic_context));
    }
  }

  // ---- v9 options. All of these are context-construction parameters, so they
  // must be applied before Build() reads them.

  // Host mapping rewrites only the resolution result: the TCP destination
  // changes while SNI, the Host header and certificate verification keep using
  // the URL host (net/dns/mapped_host_resolver.cc:70-78 against
  // net/socket/ssl_client_socket_impl.cc:720). That is what makes a CDN
  // address reachable without turning its certificate into an IP-SAN one.
  // Mapping rules always travel through the resolver created below, never
  // through set_host_mapping_rules(): the builder DCHECKs that the two are not
  // combined (url_request_context_builder.cc:359-362), and the resolver is now
  // always client-supplied. CreateStandaloneResolver wraps it in a
  // MappedHostResolver itself (net/dns/host_resolver.cc:456-462).
  if (!options_.hsts_bypass_list.empty()) {
    builder.set_hsts_policy_bypass_list(options_.hsts_bypass_list);
  }

  // Proxy chain: the rules have to be rewritten before the config service is
  // handed to the builder, because the builder snapshots proxy_config_ at
  // Build() time. Reversing the two would accept the field and change nothing.
  if (!options_.proxy_chain.empty()) {
    net::ProxyConfig::ProxyRules rules;
    rules.type = net::ProxyConfig::ProxyRules::Type::PROXY_LIST;
    for (const auto &entry : base::SplitStringPiece(
             options_.proxy_chain, ",", base::TRIM_WHITESPACE,
             base::SPLIT_WANT_NONEMPTY)) {
      const std::string text(entry);
      // "scheme://host:port". ProxyServer parses the three parts itself, so a
      // malformed entry is rejected here rather than becoming a chain that
      // resolves to nothing.
      const size_t scheme_end = text.find("://");
      if (scheme_end == std::string::npos) {
        return;
      }
      const std::string_view scheme = std::string_view(text).substr(0, scheme_end);
      const std::string_view rest =
          std::string_view(text).substr(scheme_end + 3);
      const size_t colon = rest.rfind(':');
      if (colon == std::string_view::npos) {
        return;
      }
      net::ProxyServer::Scheme parsed_scheme = net::ProxyServer::SCHEME_HTTP;
      if (base::EqualsCaseInsensitiveASCII(scheme, "socks5") ||
          // "socks" is Chromium's own spelling for SOCKS5
          // (proxy_string_util.cc:253). proxy= already accepts it through
          // ProxyRules::ParseFromString, so refusing it here made the same
          // proxy fail or work depending on which option named it.
          base::EqualsCaseInsensitiveASCII(scheme, "socks")) {
        parsed_scheme = net::ProxyServer::SCHEME_SOCKS5;
      } else if (base::EqualsCaseInsensitiveASCII(scheme, "socks4")) {
        parsed_scheme = net::ProxyServer::SCHEME_SOCKS4;
      } else if (base::EqualsCaseInsensitiveASCII(scheme, "https")) {
        parsed_scheme = net::ProxyServer::SCHEME_HTTPS;
      } else if (base::EqualsCaseInsensitiveASCII(scheme, "quic")) {
        // The flag is on for every artifact this repository ships (see the
        // args.gn block in tools/build-core-*.sh), and mn_engine_create already
        // rejects quic:// when it is not, so this branch only ever runs on a
        // build that can dial it. ProxyChain::IsValidInternal is the second gate:
        // without ShouldAllowQuicForAllChains() the chain would be rejected and
        // the request would fail with ERR_NO_SUPPORTED_PROXIES.
        parsed_scheme = net::ProxyServer::SCHEME_QUIC;
      } else if (!base::EqualsCaseInsensitiveASCII(scheme, "http")) {
        return;
      }
      const net::ProxyServer server = net::ProxyServer::FromSchemeHostAndPort(
          parsed_scheme, rest.substr(0, colon), rest.substr(colon + 1));
      if (!server.is_valid()) {
        return;
      }
      rules.single_proxies.AddProxyServer(server);
    }
    proxy_config_.proxy_rules() = rules;
  }

  builder.set_proxy_config_service(
      std::make_unique<net::ProxyConfigServiceFixed>(
          net::ProxyConfigWithAnnotation(proxy_config_,
                                         kProxyTrafficAnnotation)));

  // The engine always supplies a resolver. That is not an optimisation: the
  // builder default is HostResolver::CreateStandaloneResolver(net_log,
  // ManagerOptions(), ...) (url_request_context_builder.cc:381), and
  // ManagerOptions::insecure_dns_mode defaults to kDisabled
  // (net/dns/host_resolver.h:354), which makes HostResolverManager push no
  // DNS task at all (net/dns/host_resolver_manager.cc:1354-1355). Only the
  // SYSTEM task survives, and HostResolverSystemTask is getaddrinfo(): it has
  // no HTTPS-record path whatsoever, so no ECHConfigList could ever appear.
  //
  // Chrome enables the built-in client by default
  // (chrome/browser/net/stub_resolver_config_reader.cc:190 -> kAsyncDns,
  // enabled for desktop at net/base/features.cc:40-43) and picks
  // kEnabledBuiltIn on every non-Android platform because
  // features::IsDnsPlatformSupported() is false there (features.cc:997-1006,
  // stub_resolver_config_reader.cc:449-457). additional_types_via_insecure_
  // dns_enabled defaults to true (host_resolver.h:358), which is what makes
  // the resolver ask for the HTTPS record that carries ech=. This mirrors
  // that configuration; the dns_* options below only override fields of it.
  {
    net::DnsConfigOverrides overrides;
    if (!options_.dns_nameservers.empty()) {
      std::vector<net::IPEndPoint> endpoints;
      for (const auto &entry : base::SplitStringPiece(
               options_.dns_nameservers, ",", base::TRIM_WHITESPACE,
               base::SPLIT_WANT_NONEMPTY)) {
        // "8.8.8.8:53" or "1.1.1.1". IPEndPoint has no text parser, so the
        // address and the optional port are split and parsed separately; an
        // unparsable entry fails the whole engine rather than leaving the
        // resolver on the system configuration.
        const std::string text(entry);
        const size_t colon = text.rfind(':');
        const std::string_view host =
            colon == std::string::npos
                ? std::string_view(text)
                : std::string_view(text).substr(0, colon);
        std::optional<net::IPAddress> address =
            net::IPAddress::FromIPLiteral(host);
        if (!address.has_value()) {
          return;
        }
        uint16_t port = 53;
        if (colon != std::string::npos) {
          const std::string_view port_text =
              std::string_view(text).substr(colon + 1);
          unsigned value = 0;
          for (char digit : port_text) {
            if (digit < '0' || digit > '9') {
              return;
            }
            value = value * 10 + static_cast<unsigned>(digit - '0');
            if (value > 65535) {
              return;
            }
          }
          port = static_cast<uint16_t>(value);
        }
        endpoints.emplace_back(*address, port);
      }
      overrides.nameservers = std::move(endpoints);
    }
    // Each field is overridden only when the caller supplied it. Setting
    // secure_dns_mode unconditionally would pin kOff over whatever the system
    // configuration says, and setting nameservers unconditionally would
    // replace the system list with an empty one.
    if (options_.dns_secure_mode != 0) {
      overrides.secure_dns_mode =
          static_cast<net::SecureDnsMode>(options_.dns_secure_mode);
    }
    if (options_.dns_rotate != 0) {
      overrides.rotate = true;
    }
    if (!options_.doh_template.empty()) {
      std::optional<net::DnsOverHttpsConfig> doh =
          net::DnsOverHttpsConfig::FromString(options_.doh_template);
      if (!doh.has_value()) {
        return;
      }
      overrides.dns_over_https_config = std::move(*doh);
    }
    net::HostResolver::ManagerOptions resolver_options;
    // kEnabledBuiltIn, not the kDisabled default, for two reasons. It is what
    // makes the DNS task run at all: kDisabled pushes no TaskType::DNS
    // (net/dns/host_resolver_manager.cc:1355), which is also why the old code
    // could accept dns_nameservers and ignore it -- measured then, a request
    // with dns_nameservers="127.0.0.1:1" still resolved example.com in 0.5 s
    // and sent zero packets to that address. And it is what desktop Chrome
    // itself selects (stub_resolver_config_reader.cc:456). The mode still
    // declines every job when the effective config carries unhandled options
    // (net/dns/dns_client.cc:167), which is why a resolv.conf with a
    // bare-resolv option falls back to the system resolver rather than
    // guessing.
    resolver_options.insecure_dns_mode =
        net::InsecureDnsMode::kEnabledBuiltIn;
    // Named rather than inherited: the field defaults to true, but this is the
    // one that decides whether the DNS HTTPS record is fetched, and a future
    // Chromium default flip must fail a test here instead of silently taking
    // ECH away.
    resolver_options.additional_types_via_insecure_dns_enabled = true;
    resolver_options.dns_config_overrides = std::move(overrides);
    builder.set_host_resolver(net::HostResolver::CreateStandaloneResolver(
        net::NetLog::Get(), std::move(resolver_options),
        options_.host_mapping_rules, true /* enable_caching */,
        false /* enable_stale */));
  }
  if (http_cache_enabled_) {
    net::URLRequestContextBuilder::HttpCacheParams cache_params;
    cache_params.type =
        net::URLRequestContextBuilder::HttpCacheParams::IN_MEMORY;
    builder.EnableHttpCache(cache_params);
  } else {
    builder.DisableHttpCache();
  }
  context_ = builder.Build();
#if defined(MINICRONET_PROFILE_VERIFICATION)
  if (const RuntimeProfileData *profile = profile_.data()) {
    quic::QuicConfig quic_config =
        net::InitializeQuicConfig(*context_->quic_context()->params());
    constexpr auto kLegacyVersionInformation =
        static_cast<quic::TransportParameters::TransportParameterId>(0x4752);
    CHECK_EQ(quic_config.custom_transport_parameters_to_send().contains(
                 kLegacyVersionInformation),
             profile->send_quic_legacy_version_information);
    net::HttpNetworkSession *session =
        context_->http_transaction_factory()->GetSession();
    CHECK_EQ(session->IsQuicEnabled(), profile->enable_quic);
    const net::SocketPoolAdditionalCapacity empty =
        net::SocketPoolAdditionalCapacity::CreateEmpty();
    const net::ProxyChain proxy(net::ProxyServer::SCHEME_HTTP,
                                net::HostPortPair("127.0.0.1", 9));
    for (net::HttpNetworkSession::SocketPoolType pool_type :
         {net::HttpNetworkSession::SocketPoolType::kNormal,
          net::HttpNetworkSession::SocketPoolType::kWebSocket}) {
      CHECK_EQ(session->GetSocketPool(pool_type, net::ProxyChain::Direct())
                       ->AdditionalCapacityForTest() != empty,
               profile->randomize_socket_pool_limit);
      CHECK_EQ(session->GetSocketPool(pool_type, proxy)
                       ->AdditionalCapacityForTest() != empty,
               profile->randomize_proxy_socket_pool_limit);
    }
  }
#endif
}

namespace {

// Runs |task| on the network thread and waits for it. Returns false when the
// context is already gone, which is the only case where the result is not the
// task's own.
//
// Waiting here is safe: the only thing the network thread does for these calls
// is the CookieMonster closure, which this build runs inline (see the note in
// engine.h), and SetClientCertificate, which takes no lock this thread holds.
template <typename Task> void RunOnNetworkThread(Engine *engine, Task task) {
  auto runner = engine->task_runner();
  if (runner->RunsTasksInCurrentSequence()) {
    task();
    return;
  }
  base::WaitableEvent done;
  runner->PostTask(FROM_HERE, base::BindOnce(
                                  [](Task task, base::WaitableEvent *done) {
                                    task();
                                    done->Signal();
                                  },
                                  std::move(task), base::Unretained(&done)));
  done.Wait();
}

// Appends |value| to |storage| and points |out| at it.
//
// storage must be a container whose push_back cannot move existing elements:
// every mn_cookie_t field points into it, so a std::vector would leave those
// pointers dangling the moment it grows. A deque allocates a fresh block per
// element growth and never relocates what it already handed out, which is
// exactly the guarantee this needs. Measured with a vector: the name field read
// back empty because the fourth push_back had moved the first string.
void StoreString(const std::string &value, std::deque<std::string> *storage,
                 const char **out, size_t *out_length) {
  storage->push_back(value);
  const std::string &stored = storage->back();
  *out = stored.data();
  *out_length = stored.size();
}

} // namespace

mn_result_t Engine::GetCookies(const std::string *url, CookieView *out) {
  if (!out) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  mn_result_t result = MN_ERROR_INVALID_STATE;
  RunOnNetworkThread(this, [this, url, out, &result] {
    result = GetCookiesOnNetworkThread(url, out);
  });
  return result;
}

mn_result_t Engine::GetCookiesOnNetworkThread(const std::string *url,
                                              CookieView *out) {
  net::CookieStore *store = context_ ? context_->cookie_store() : nullptr;
  if (!store) {
    return MN_ERROR_INVALID_STATE;
  }
  // The view is reused across calls (it lives on the engine handle so the
  // returned pointers stay valid), so each call must start from empty.
  out->cookies.clear();
  out->storage.clear();
  net::CookieList list;
  base::WaitableEvent done;
  if (url) {
    GURL gurl(*url);
    if (!gurl.is_valid()) {
      return MN_ERROR_INVALID_ARGUMENT;
    }
    net::CookieOptions options;
    options.set_include_httponly();
    options.set_same_site_cookie_context(
        net::CookieOptions::SameSiteCookieContext::MakeInclusive());
    store->GetCookieListWithOptionsAsync(
        gurl, options, net::CookiePartitionKeyCollection::ContainsAll(),
        base::BindOnce(
            [](net::CookieList *out_list, base::WaitableEvent *done,
               const net::CookieAccessResultList &included,
               const net::CookieAccessResultList &) {
              for (const auto &entry : included) {
                out_list->push_back(entry.cookie);
              }
              done->Signal();
            },
            &list, &done));
  } else {
    store->GetAllCookiesAsync(base::BindOnce(
        [](net::CookieList *out_list, base::WaitableEvent *done,
           const net::CookieList &cookies) {
          *out_list = cookies;
          done->Signal();
        },
        &list, &done));
  }
  done.Wait();

  out->cookies.reserve(list.size());
  // No reserve() on storage: a deque has none, and reserving in a vector is
  // exactly the reallocation that would dangle the pointers it backs., and reserving in a vector is exactly the
  // reallocation that used to dangle the pointers this container backs.
  for (const net::CanonicalCookie &cookie : list) {
    mn_cookie_t entry{};
    entry.size = sizeof(mn_cookie_t);
    entry.version = MN_ABI_VERSION;
    StoreString(cookie.Name(), &out->storage, &entry.name, &entry.name_length);
    StoreString(cookie.Value(), &out->storage, &entry.value,
                &entry.value_length);
    StoreString(cookie.Domain(), &out->storage, &entry.domain,
                &entry.domain_length);
    StoreString(cookie.Path(), &out->storage, &entry.path, &entry.path_length);
    entry.has_expiry = cookie.IsPersistent() ? 1 : 0;
    entry.expiry_unix_seconds =
        cookie.IsPersistent() ? cookie.ExpiryDate().InSecondsFSinceUnixEpoch()
                              : 0;
    entry.secure = cookie.SecureAttribute() ? 1 : 0;
    entry.http_only = cookie.IsHttpOnly() ? 1 : 0;
    switch (cookie.SameSite()) {
    case net::CookieSameSite::NO_RESTRICTION:
      entry.same_site = MN_COOKIE_SAME_SITE_NO_RESTRICTION;
      break;
    case net::CookieSameSite::LAX_MODE:
      entry.same_site = MN_COOKIE_SAME_SITE_LAX;
      break;
    case net::CookieSameSite::STRICT_MODE:
      entry.same_site = MN_COOKIE_SAME_SITE_STRICT;
      break;
    case net::CookieSameSite::UNSPECIFIED:
      entry.same_site = MN_COOKIE_SAME_SITE_UNSPECIFIED;
      break;
    }
    out->cookies.push_back(entry);
  }
  return MN_OK;
}

mn_result_t Engine::SetCookie(const mn_cookie_t &cookie,
                              const std::string &source_url,
                              int64_t creation_unix_seconds) {
  mn_result_t result = MN_ERROR_INVALID_STATE;
  RunOnNetworkThread(this, [this, &cookie, &source_url, creation_unix_seconds,
                            &result] {
    result =
        SetCookieOnNetworkThread(cookie, source_url, creation_unix_seconds);
  });
  return result;
}

mn_result_t Engine::SetCookieOnNetworkThread(const mn_cookie_t &cookie,
                                             const std::string &source_url,
                                             int64_t creation_unix_seconds) {
  net::CookieStore *store = context_ ? context_->cookie_store() : nullptr;
  if (!store) {
    return MN_ERROR_INVALID_STATE;
  }
  GURL url(source_url);
  if (!url.is_valid() || !url.SchemeIsHTTPOrHTTPS()) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  if (!cookie.name || !cookie.value || !cookie.domain || !cookie.path) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  const std::string name(cookie.name, cookie.name_length);
  const std::string value(cookie.value, cookie.value_length);
  const std::string domain(cookie.domain, cookie.domain_length);
  const std::string path(cookie.path, cookie.path_length);
  net::CookieSameSite same_site = net::CookieSameSite::UNSPECIFIED;
  switch (cookie.same_site) {
  case MN_COOKIE_SAME_SITE_NO_RESTRICTION:
    same_site = net::CookieSameSite::NO_RESTRICTION;
    break;
  case MN_COOKIE_SAME_SITE_LAX:
    same_site = net::CookieSameSite::LAX_MODE;
    break;
  case MN_COOKIE_SAME_SITE_STRICT:
    same_site = net::CookieSameSite::STRICT_MODE;
    break;
  case MN_COOKIE_SAME_SITE_UNSPECIFIED:
    break;
  default:
    return MN_ERROR_INVALID_ARGUMENT;
  }
  base::Time creation = creation_unix_seconds > 0
                            ? base::Time::FromSecondsSinceUnixEpoch(
                                  static_cast<double>(creation_unix_seconds))
                            : base::Time::Now();
  base::Time expiry =
      cookie.has_expiry
          ? base::Time::FromSecondsSinceUnixEpoch(
                static_cast<double>(cookie.expiry_unix_seconds))
          : base::Time();
  net::CookieInclusionStatus status;
  std::unique_ptr<net::CanonicalCookie> canonical =
      net::CanonicalCookie::CreateSanitizedCookie(
          url, name, value, domain, path, creation, expiry, creation,
          cookie.secure != 0, cookie.http_only != 0, same_site,
          net::COOKIE_PRIORITY_DEFAULT, std::nullopt, &status);
  if (!canonical) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  net::CookieOptions options;
  if (cookie.http_only != 0) {
    options.set_include_httponly();
  }
  options.set_same_site_cookie_context(
      net::CookieOptions::SameSiteCookieContext::MakeInclusive());
  bool accepted = false;
  base::WaitableEvent done;
  store->SetCanonicalCookieAsync(
      std::move(canonical), url, options,
      base::BindOnce(
          [](bool *accepted, base::WaitableEvent *done,
             net::CookieAccessResult result) {
            *accepted = result.status.IsInclude();
            done->Signal();
          },
          &accepted, &done),
      std::nullopt);
  done.Wait();
  return accepted ? MN_OK : MN_ERROR_INVALID_ARGUMENT;
}

mn_result_t Engine::DeleteCookie(const std::string &domain,
                                 const std::string &path,
                                 const std::string &name,
                                 size_t *out_removed) {
  mn_result_t result = MN_ERROR_INVALID_STATE;
  RunOnNetworkThread(this, [this, &domain, &path, &name, out_removed,
                            &result] {
    result = DeleteCookieOnNetworkThread(domain, path, name, out_removed);
  });
  return result;
}

mn_result_t Engine::DeleteCookieOnNetworkThread(const std::string &domain,
                                                const std::string &path,
                                                const std::string &name,
                                                size_t *out_removed) {
  net::CookieStore *store = context_ ? context_->cookie_store() : nullptr;
  if (!store) {
    return MN_ERROR_INVALID_STATE;
  }
  // Counted by the predicate, not by the completion callback.
  //
  // DeleteMatchingCookies runs the predicate once per cookie and deletes exactly
  // the ones it returned true for
  // (net/cookies/cookie_monster.cc:951-993), then calls FlushStore. FlushStore
  // posts its callback when there is no backing store
  // (net/cookies/cookie_monster.cc:511-520), and that task runs on this same
  // runner, so the callback's count can never be observed here without
  // deadlocking. The predicate runs synchronously, so counting there gives the
  // same number without waiting for anything.
  //
  // CanonicalCookie::Domain() encodes the host-only flag as a leading dot
  // (net/cookies/cookie_base.h:84-86), while a caller names the host the way it
  // wrote it, so both spellings are accepted.
  size_t removed = 0;
  store->DeleteMatchingCookiesAsync(
      base::BindRepeating(
          [](const std::string &domain, const std::string &path,
             const std::string &name, size_t *removed,
             const net::CanonicalCookie &cookie) {
            const bool domain_matches =
                cookie.Domain() == domain || cookie.DomainWithoutDot() == domain;
            if (!domain_matches || cookie.Path() != path ||
                cookie.Name() != name) {
              return false;
            }
            ++*removed;
            return true;
          },
          domain, path, name, &removed),
      base::BindOnce([](uint32_t) {}));
  // The predicate has already run for every cookie by the time
  // DeleteMatchingCookiesAsync returns: DoCookieCallback dispatches inline when
  // there is no backing store to load keys from.
  if (out_removed) {
    *out_removed = removed;
  }
  return MN_OK;
}

mn_result_t Engine::ClearCookies() {
  mn_result_t result = MN_ERROR_INVALID_STATE;
  RunOnNetworkThread(this, [this, &result] {
    result = ClearCookiesOnNetworkThread();
  });
  return result;
}

mn_result_t Engine::ClearCookiesOnNetworkThread() {
  net::CookieStore *store = context_ ? context_->cookie_store() : nullptr;
  if (!store) {
    return MN_ERROR_INVALID_STATE;
  }
  // Same shape as DeleteCookie: DeleteAllAsync deletes inline and then posts
  // its completion through FlushStore, so the deletion is done when this
  // returns and only the callback is deferred.
  store->DeleteAllAsync(base::BindOnce([](uint32_t) {}));
  return MN_OK;
}
namespace {

// Reads every PEM block of one type out of |pem| and returns the DER encoding
// of each. Returns an empty vector when the input is not one or more PEM blocks
// of that type, which the caller turns into MN_ERROR_INVALID_ARGUMENT.
std::vector<std::string> PemBlocksToDer(const std::vector<uint8_t> &pem,
                                        bool private_key) {
  std::vector<std::string> out;
  if (pem.empty()) {
    return out;
  }
  bssl::UniquePtr<BIO> bio(
      BIO_new_mem_buf(pem.data(), static_cast<int>(pem.size())));
  if (!bio) {
    return out;
  }
  for (;;) {
    if (private_key) {
      EVP_PKEY *key = PEM_read_bio_PrivateKey(
          bio.get(), nullptr,
          // A passphrase-protected key reaches this callback with an empty
          // buffer. Returning 0 makes PEM_read_bio_PrivateKey fail instead of
          // hanging or guessing, which is the fail-closed behaviour the header
          // documents.
          [](char *, int, int, void *) { return 0; }, nullptr);
      if (!key) {
        break;
      }
      bssl::UniquePtr<EVP_PKEY> owned(key);
      uint8_t *der = nullptr;
      const int length = i2d_PrivateKey(owned.get(), &der);
      if (length <= 0) {
        OPENSSL_free(der);
        return std::vector<std::string>();
      }
      out.emplace_back(reinterpret_cast<const char *>(der),
                       static_cast<size_t>(length));
      OPENSSL_free(der);
      // A private key file holds exactly one key.
      break;
    }
    X509 *cert = PEM_read_bio_X509(bio.get(), nullptr, nullptr, nullptr);
    if (!cert) {
      break;
    }
    bssl::UniquePtr<X509> owned(cert);
    uint8_t *der = nullptr;
    const int length = i2d_X509(owned.get(), &der);
    if (length <= 0) {
      OPENSSL_free(der);
      return std::vector<std::string>();
    }
    out.emplace_back(reinterpret_cast<const char *>(der),
                     static_cast<size_t>(length));
    OPENSSL_free(der);
  }
  return out;
}

// http_network_session() on URLRequestContext is private, so the only public
// route to the SSLClientContext is through the transaction factory, which
// needs its own definition for the virtual call to resolve.
net::SSLClientContext *SSLContextOf(net::URLRequestContext *context) {
  if (!context || !context->http_transaction_factory()) {
    return nullptr;
  }
  net::HttpNetworkSession *session =
      context->http_transaction_factory()->GetSession();
  return session ? session->ssl_client_context() : nullptr;
}

} // namespace

mn_result_t Engine::SetClientCertificate(
    const std::string &host, uint16_t port,
    const std::vector<uint8_t> &cert_pem,
    const std::vector<uint8_t> &key_pem) {
  mn_result_t result = MN_ERROR_INVALID_STATE;
  RunOnNetworkThread(this, [this, &host, port, &cert_pem, &key_pem, &result] {
    result = SetClientCertificateOnNetworkThread(host, port, cert_pem, key_pem);
  });
  return result;
}

mn_result_t Engine::SetClientCertificateOnNetworkThread(
    const std::string &host, uint16_t port,
    const std::vector<uint8_t> &cert_pem,
    const std::vector<uint8_t> &key_pem) {
  net::SSLClientContext *ssl = SSLContextOf(context_.get());
  if (!ssl) {
    return MN_ERROR_INVALID_STATE;
  }
  std::vector<std::string> chain = PemBlocksToDer(cert_pem, false);
  if (chain.empty()) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  std::vector<std::string> key_der = PemBlocksToDer(key_pem, true);
  if (key_der.size() != 1) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  std::vector<std::string_view> chain_views;
  chain_views.reserve(chain.size());
  for (const std::string &der : chain) {
    chain_views.emplace_back(der);
  }
  scoped_refptr<net::X509Certificate> certificate =
      net::X509Certificate::CreateFromDERCertChain(chain_views);
  if (!certificate) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  const uint8_t *key_bytes =
      reinterpret_cast<const uint8_t *>(key_der[0].data());
  bssl::UniquePtr<EVP_PKEY> key(
      d2i_AutoPrivateKey(nullptr, &key_bytes, static_cast<long>(key_der[0].size())));
  if (!key) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  // SetClientCertificate synchronously calls OnSSLConfigForServersChanged(),
  // and HttpStreamPool answers it by refreshing that origin's group
  // (net/http/http_stream_pool.cc, Refresh(kSslConfigChanged)) -- the idle
  // connections for this server are closed. Measured consequence: without it a
  // second request would reuse the pooled connection established under the old
  // certificate and the server would keep seeing the previous CN.
  ssl->SetClientCertificate(net::HostPortPair(host, port), std::move(certificate),
                            net::WrapOpenSSLPrivateKey(std::move(key)));
  return MN_OK;
}

mn_result_t Engine::ClearClientCertificate(const std::string &host,
                                           uint16_t port) {
  mn_result_t result = MN_ERROR_INVALID_STATE;
  RunOnNetworkThread(this, [this, &host, port, &result] {
    result = ClearClientCertificateOnNetworkThread(host, port);
  });
  return result;
}

mn_result_t Engine::ClearClientCertificateOnNetworkThread(
    const std::string &host, uint16_t port) {
  net::SSLClientContext *ssl = SSLContextOf(context_.get());
  if (!ssl) {
    return MN_ERROR_INVALID_STATE;
  }
  ssl->ClearClientCertificate(net::HostPortPair(host, port));
  return MN_OK;
}

const char *Engine::platform_hint() const {
  // One table, shared with mn_platform_hint and DefaultUserAgent; see
  // platform_identity.h. A second switch here would be a second answer to the
  // same question, and the two would eventually disagree.
  return PlatformHint(options_.platform);
}

void Engine::ShutdownOnNetworkThread() { context_.reset(); }

} // namespace minicronet
