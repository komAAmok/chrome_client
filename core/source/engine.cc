#include "minicronet/engine.h"
#include "minicronet/profile_ssl_config_service.h"

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
#include "base/no_destructor.h"
#include "base/synchronization/lock.h"
#include "base/synchronization/waitable_event.h"
#include "base/thread_annotations.h"
#include "base/time/time.h"
#include "base/task/thread_pool/thread_pool_instance.h"
#include "base/threading/thread.h"
#include "net/base/network_change_notifier.h"
#include "net/cert/caching_cert_verifier.h"
#include "net/cert/coalescing_cert_verifier.h"
#include "net/cert/cert_verifier.h"
#include "net/cert/cert_verify_result.h"
#include "net/cert/x509_util.h"
#include "net/http/http_network_session.h"
#include "net/proxy_resolution/proxy_config_service_fixed.h"
#include "net/proxy_resolution/proxy_config_with_annotation.h"
#include "net/quic/quic_context.h"
#include "net/third_party/quiche/src/quiche/quic/core/crypto/crypto_protocol.h"
#if defined(MINICRONET_PROFILE_VERIFICATION)
#include "net/base/host_port_pair.h"
#include "net/base/proxy_chain.h"
#include "net/base/proxy_server.h"
#include "net/http/http_transaction_factory.h"
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
#include "third_party/boringssl/src/pki/pem.h"
#include "url/gurl.h"
#include "url/scheme_host_port.h"

namespace minicronet {
namespace {

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
                                     ProfileContext profile) {
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
      std::move(profile)));
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
               ProfileContext profile)
    : user_agent_(std::move(user_agent)),
      accept_language_(std::move(accept_language)),
      proxy_config_(std::move(proxy_config)),
      proxy_username_(std::move(proxy_username)),
      proxy_password_(std::move(proxy_password)),
      http_cache_enabled_(http_cache_enabled), protocol_mode_(protocol_mode),
      tls_verify_mode_(tls_verify_mode), custom_ca_pem_(std::move(custom_ca_pem)),
      profile_(std::move(profile)), fork_generation_(CurrentForkGeneration()) {}

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
    quic_context->params()->maintain_ipv6_temp_addr = false;
    quic_context->params()->send_legacy_version_information =
        profile->send_quic_legacy_version_information;
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
    switch (protocol_mode_) {
      case MN_PROTOCOL_FORCE_H1:
        session_params.protocol_mode = net::HttpProtocolMode::kHttp11;
        session_params.enable_http2 = false;
        session_params.enable_quic = false;
        break;
      case MN_PROTOCOL_FORCE_H2:
        session_params.protocol_mode = net::HttpProtocolMode::kHttp2;
        session_params.enable_http2 = true;
        session_params.enable_quic = false;
        break;
      case MN_PROTOCOL_FORCE_H3:
        session_params.protocol_mode = net::HttpProtocolMode::kHttp3;
        session_params.enable_http2 = false;
        session_params.enable_quic = true;
        break;
      case MN_PROTOCOL_NATIVE:
        break;
    }
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
    session_params.send_priority_header = profile->send_priority_header;
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
  if (!profile_.data() && tls_verify_mode_ == MN_TLS_VERIFY_INSECURE) {
    net::HttpNetworkSessionParams session_params;
    session_params.ignore_certificate_errors = true;
    builder.set_http_network_session_params(session_params);
  }

  builder.set_proxy_config_service(
      std::make_unique<net::ProxyConfigServiceFixed>(
          net::ProxyConfigWithAnnotation(proxy_config_,
                                         kProxyTrafficAnnotation)));
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

void Engine::ShutdownOnNetworkThread() { context_.reset(); }

} // namespace minicronet
