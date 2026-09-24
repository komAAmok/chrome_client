#include "minicronet/engine.h"

#ifdef UNSAFE_BUFFERS_BUILD
#pragma allow_unsafe_buffers
#endif

#include <cstdio>
#include <memory>
#include <string>

#include "base/functional/bind.h"
#include "base/memory/ref_counted.h"
#include "base/synchronization/waitable_event.h"
#include "net/base/host_port_pair.h"
#include "net/base/io_buffer.h"
#include "net/base/isolation_info.h"
#include "net/base/load_flags.h"
#include "net/base/net_errors.h"
#include "net/base/network_anonymization_key.h"
#include "net/base/proxy_chain.h"
#include "net/base/proxy_server.h"
#include "net/http/http_server_properties.h"
#include "net/http/http_transaction_factory.h"
#include "net/http/http_network_session.h"
#include "net/quic/quic_context.h"
#include "net/socket/client_socket_pool.h"
#include "net/ssl/ssl_client_session_cache.h"
#include "net/ssl/ssl_info.h"
#include "net/traffic_annotation/network_traffic_annotation.h"
#include "net/url_request/url_request.h"
#include "net/url_request/url_request_context.h"
#include "url/gurl.h"
#include "url/origin.h"
#include "url/scheme_host_port.h"

namespace {

constexpr net::NetworkTrafficAnnotationTag kTrafficAnnotation =
    net::DefineNetworkTrafficAnnotation("minicronet_state_probe", R"(
      semantics {
        sender: "MiniCronet state isolation probe"
        description: "Local verification request."
        trigger: "A verification build is run."
        data: "No user data."
        destination: LOCAL
      }
      policy {
        cookies_allowed: YES
        cookies_store: "Ephemeral test CookieStore"
        setting: "Verification builds only."
        policy_exception_justification: "Local test traffic only."
      })");

bool MakeProfile(const char* id, minicronet::ProfileContext* out) {
  return minicronet::ProfileContext::Create(
      id, std::string("minicronet/") + id, out);
}

scoped_refptr<minicronet::Engine> MakeEngine(
    const minicronet::ProfileContext& profile) {
  net::ProxyConfig proxy;
  return minicronet::Engine::Create("minicronet/probe", "", proxy, "", "",
                                    true, MN_PROTOCOL_NATIVE,
                                    MN_TLS_VERIFY_CHROMIUM_DEFAULT, "", profile);
}

class Request final : public net::URLRequest::Delegate {
 public:
  bool Run(const scoped_refptr<minicronet::Engine>& engine,
           const std::string& url, int load_flags = net::LOAD_NORMAL) {
    done_.Reset();
    result_ = net::ERR_FAILED;
    body_.clear();
    handshake_type_ = net::SSLInfo::HANDSHAKE_UNKNOWN;
    engine->task_runner()->PostTask(
        FROM_HERE, base::BindOnce(&Request::Start, base::Unretained(this),
                                  engine, GURL(url), load_flags));
    done_.Wait();
    return result_ == net::OK;
  }

  const std::string& body() const { return body_; }
  net::SSLInfo::HandshakeType handshake_type() const {
    return handshake_type_;
  }

 private:
  void Start(scoped_refptr<minicronet::Engine> engine, GURL url,
             int load_flags) {
    engine_ = std::move(engine);
    request_ = engine_->context()->CreateRequest(
        url, net::DEFAULT_PRIORITY, this, kTrafficAnnotation,
        net::handles::kInvalidNetworkHandle);
    const url::Origin origin = url::Origin::Create(url);
    request_->set_isolation_info(
        net::IsolationInfo::CreateForInternalRequest(origin));
    request_->set_site_for_cookies(net::SiteForCookies::FromOrigin(origin));
    request_->SetLoadFlags(load_flags);
    request_->Start();
  }

  void OnResponseStarted(net::URLRequest* request, int net_error) override {
    if (net_error != net::OK) {
      Finish(net_error);
      return;
    }
    handshake_type_ = request->ssl_info().handshake_type;
    ReadMore();
  }

  void OnReadCompleted(net::URLRequest* request, int bytes_read) override {
    if (bytes_read <= 0) {
      Finish(bytes_read);
      return;
    }
    body_.append(buffer_->data(), static_cast<size_t>(bytes_read));
    ReadMore();
  }

  void ReadMore() {
    if (!buffer_) {
      buffer_ = base::MakeRefCounted<net::IOBufferWithSize>(4096);
    }
    int result = request_->Read(buffer_.get(), buffer_->size());
    if (result != net::ERR_IO_PENDING) {
      OnReadCompleted(request_.get(), result);
    }
  }

  void Finish(int result) {
    result_ = result;
    request_.reset();
    engine_.reset();
    done_.Signal();
  }

  base::WaitableEvent done_;
  scoped_refptr<minicronet::Engine> engine_;
  std::unique_ptr<net::URLRequest> request_;
  scoped_refptr<net::IOBufferWithSize> buffer_;
  std::string body_;
  int result_ = net::ERR_FAILED;
  net::SSLInfo::HandshakeType handshake_type_ =
      net::SSLInfo::HANDSHAKE_UNKNOWN;
};

std::string Join(const std::string& base, const char* path) {
  return base + path;
}

}  // namespace

int main(int argc, char** argv) {
  if (argc != 3) {
    std::fprintf(stderr, "usage: %s HTTP_BASE HTTPS_BASE\n", argv[0]);
    return 1;
  }

  minicronet::ProfileContext p122;
  minicronet::ProfileContext p123;
  if (!MakeProfile("chrome_122", &p122) ||
      !MakeProfile("chrome_123", &p123)) {
    return 2;
  }
  auto e122 = MakeEngine(p122);
  auto e123 = MakeEngine(p123);
  if (!e122 || !e123) {
    return 3;
  }

  net::URLRequestContext* c122 = e122->context();
  net::URLRequestContext* c123 = e123->context();
  auto* f122 = c122->http_transaction_factory();
  auto* f123 = c123->http_transaction_factory();
  auto* s122 = f122->GetSession();
  auto* s123 = f123->GetSession();
  if (!s122 || !s123 || c122 == c123 ||
      c122->cookie_store() == c123->cookie_store() ||
      f122->GetCache() == f123->GetCache() ||
      c122->http_server_properties() == c123->http_server_properties() ||
      s122 == s123 || s122->spdy_session_pool() == s123->spdy_session_pool() ||
      s122->quic_session_pool() == s123->quic_session_pool() ||
      s122->ssl_client_context()->ssl_client_session_cache() ==
          s123->ssl_client_context()->ssl_client_session_cache()) {
    return 4;
  }

  bool pools_are_isolated = false;
  base::WaitableEvent pools_checked;
  e122->task_runner()->PostTask(
      FROM_HERE,
      base::BindOnce(
          [](net::HttpNetworkSession* first, net::HttpNetworkSession* second,
             bool* result, base::WaitableEvent* done) {
            const net::ProxyChain proxy(net::ProxyServer::SCHEME_HTTP,
                                        net::HostPortPair("127.0.0.1", 9));
            *result =
                first->GetSocketPool(
                    net::HttpNetworkSession::SocketPoolType::kNormal,
                    net::ProxyChain::Direct()) !=
                    second->GetSocketPool(
                        net::HttpNetworkSession::SocketPoolType::kNormal,
                        net::ProxyChain::Direct()) &&
                first->GetSocketPool(
                    net::HttpNetworkSession::SocketPoolType::kNormal, proxy) !=
                    second->GetSocketPool(
                        net::HttpNetworkSession::SocketPoolType::kNormal,
                        proxy);
            done->Signal();
          },
          s122, s123, &pools_are_isolated, &pools_checked));
  pools_checked.Wait();
  if (!pools_are_isolated) {
    std::fprintf(stderr, "pool isolation failed\n");
    return 5;
  }

  Request request;
  const std::string http(argv[1]);
  if (!request.Run(e122, Join(http, "/cache")) || request.body() != "cache-1" ||
      !request.Run(e122, Join(http, "/cache")) || request.body() != "cache-1" ||
      !request.Run(e123, Join(http, "/cache")) || request.body() != "cache-2") {
    return 6;
  }
  if (!request.Run(e122, Join(http, "/etag")) ||
      !request.Run(e122, Join(http, "/etag"), net::LOAD_VALIDATE_CACHE) ||
      !request.Run(e123, Join(http, "/etag"))) {
    return 7;
  }
  if (!request.Run(e122, Join(http, "/cookie-redirect")) ||
      request.body().find("isolated=122") == std::string::npos ||
      !request.Run(e123, Join(http, "/cookie-read")) || !request.body().empty()) {
    return 8;
  }
  if (!request.Run(e122, Join(http, "/connection/a1"),
                   net::LOAD_DISABLE_CACHE) ||
      !request.Run(e122, Join(http, "/connection/a2"),
                   net::LOAD_DISABLE_CACHE) ||
      !request.Run(e123, Join(http, "/connection/b1"),
                   net::LOAD_DISABLE_CACHE)) {
    return 9;
  }

  const std::string https(argv[2]);
  if (!request.Run(e122, Join(https, "/tls-close/1"),
                   net::LOAD_DISABLE_CACHE) ||
      request.handshake_type() != net::SSLInfo::HANDSHAKE_FULL) {
    return 10;
  }
  bool tls_cache_isolated = false;
  base::WaitableEvent tls_cache_checked;
  e122->task_runner()->PostTask(
      FROM_HERE,
      base::BindOnce(
          [](net::HttpNetworkSession* first, net::HttpNetworkSession* second,
             bool* result, base::WaitableEvent* done) {
            *result = first->ssl_client_context()
                              ->ssl_client_session_cache()
                              ->size() > 0 &&
                      second->ssl_client_context()
                              ->ssl_client_session_cache()
                              ->size() == 0;
            done->Signal();
          },
          s122, s123, &tls_cache_isolated, &tls_cache_checked));
  tls_cache_checked.Wait();
  if (!tls_cache_isolated ||
      !request.Run(e122, Join(https, "/tls-close/2"),
                   net::LOAD_DISABLE_CACHE) ||
      request.handshake_type() != net::SSLInfo::HANDSHAKE_RESUME ||
      !request.Run(e123, Join(https, "/tls-close/3"),
                   net::LOAD_DISABLE_CACHE) ||
      request.handshake_type() != net::SSLInfo::HANDSHAKE_FULL ||
      !request.Run(e122, Join(https, "/alt-svc"),
                   net::LOAD_DISABLE_CACHE)) {
    std::fprintf(stderr, "tls or resume failed: isolated=%d handshake=%d\n",
                 tls_cache_isolated, static_cast<int>(request.handshake_type()));
    return 11;
  }

  bool alt_svc_isolated = false;
  base::WaitableEvent alt_svc_checked;
  const GURL origin_url(https);
  const net::NetworkAnonymizationKey key;
  e122->task_runner()->PostTask(
      FROM_HERE,
      base::BindOnce(
          [](net::URLRequestContext* first, net::URLRequestContext* second,
             url::SchemeHostPort origin, net::NetworkAnonymizationKey key,
             bool* result, base::WaitableEvent* done) {
            const size_t first_count =
                first->http_server_properties()
                    ->GetAlternativeServiceInfos(origin, key)
                    .size();
            const size_t second_count =
                second->http_server_properties()
                    ->GetAlternativeServiceInfos(origin, key)
                    .size();
            *result = first_count != 0 && second_count == 0;
            done->Signal();
          },
          c122, c123, url::SchemeHostPort(origin_url), key, &alt_svc_isolated,
          &alt_svc_checked));
  alt_svc_checked.Wait();
  if (!alt_svc_isolated ||
      s122->context().quic_context->params()->try_quic_by_default ||
      !s123->context().quic_context->params()->try_quic_by_default) {
    std::fprintf(stderr, "feature/alt-svc failed: alt=%d p122=%d p123=%d\n",
                 alt_svc_isolated,
                 s122->context().quic_context->params()->try_quic_by_default,
                 s123->context().quic_context->params()->try_quic_by_default);
    return 12;
  }

  if (!request.Run(e122, Join(https, "/alt-svc-expired"),
                   net::LOAD_DISABLE_CACHE)) {
    return 13;
  }
  bool alt_svc_expired = false;
  base::WaitableEvent alt_svc_expired_checked;
  e122->task_runner()->PostTask(
      FROM_HERE,
      base::BindOnce(
          [](net::URLRequestContext* context, url::SchemeHostPort origin,
             net::NetworkAnonymizationKey key, bool* result,
             base::WaitableEvent* done) {
            *result = context->http_server_properties()
                          ->GetAlternativeServiceInfos(origin, key)
                          .empty();
            done->Signal();
          },
          c122, url::SchemeHostPort(origin_url), key, &alt_svc_expired,
          &alt_svc_expired_checked));
  alt_svc_expired_checked.Wait();
  if (!alt_svc_expired) {
    return 14;
  }

  std::puts("real cache, cookie, Alt-Svc expiry, TLS session and pool isolation passed");
  return 0;
}
