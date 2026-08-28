#include "minicronet/engine.h"
#include "minicronet/profile_ssl_config_service.h"

#include <cstdlib>
#include <string_view>
#include <utility>

#include "base/at_exit.h"
#include "base/command_line.h"
#include "base/feature_list.h"
#if defined(MINICRONET_PROFILE_VERIFICATION)
#include "base/files/file.h"
#include "base/files/file_path.h"
#include "build/build_config.h"
#endif
#include "base/functional/bind.h"
#include "base/message_loop/message_pump_type.h"
#include "base/no_destructor.h"
#include "base/synchronization/waitable_event.h"
#include "base/time/time.h"
#include "base/task/thread_pool.h"
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
    callback_runner_ = base::ThreadPool::CreateSequencedTaskRunner({});

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

  scoped_refptr<base::SequencedTaskRunner> callback_runner() const {
    return callback_runner_;
  }

private:
  base::AtExitManager at_exit_manager_;
  base::Thread network_thread_{"MiniCronetNet"};
  scoped_refptr<base::SequencedTaskRunner> callback_runner_;
  std::unique_ptr<net::NetworkChangeNotifier> network_change_notifier_;
};

Runtime &GetRuntime() {
  static base::NoDestructor<Runtime> runtime;
  return *runtime;
}

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
  GetRuntime();
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

scoped_refptr<base::SequencedTaskRunner> Engine::callback_runner() const {
  return GetRuntime().callback_runner();
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
      profile_(std::move(profile)) {}

Engine::~Engine() {
  if (!context_) {
    return;
  }
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
