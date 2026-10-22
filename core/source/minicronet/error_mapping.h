#ifndef MINICRONET_CORE_ERROR_MAPPING_H_
#define MINICRONET_CORE_ERROR_MAPPING_H_

#include "minicronet.h"
#include "net/base/net_errors.h"

namespace minicronet {

inline mn_result_t MapNetError(int net_error) {
  switch (net_error) {
    case net::ERR_PROXY_AUTH_UNSUPPORTED:
    case net::ERR_PROXY_AUTH_REQUESTED:
    case net::ERR_PROXY_AUTH_REQUESTED_WITH_NO_CONNECTION:
    case net::ERR_PROXY_CONNECTION_FAILED:
    case net::ERR_PROXY_CERTIFICATE_INVALID:
    case net::ERR_PROXY_UNABLE_TO_CONNECT_TO_DESTINATION:
    case net::ERR_PROXY_DELEGATE_CANCELED_CONNECT_REQUEST:
    case net::ERR_PROXY_DELEGATE_CANCELED_CONNECT_RESPONSE:
    case net::ERR_PROXY_HTTP_1_1_REQUIRED:
    case net::ERR_SOCKS_CONNECTION_FAILED:
    case net::ERR_SOCKS_CONNECTION_HOST_UNREACHABLE:
    case net::ERR_TUNNEL_CONNECTION_FAILED:
    // Present in net_error_list.h but previously missing here, so these
    // reached the binding layer as MN_ERROR_NETWORK. The facade's own table
    // rescued -131 by name, but -323/-170/-364/-366 have no row there and
    // surfaced as ConnectionError instead of ProxyError.
    case net::ERR_MANDATORY_PROXY_CONFIGURATION_FAILED:
    case net::ERR_UNEXPECTED_PROXY_AUTH:
    case net::ERR_UNABLE_TO_REUSE_CONNECTION_FOR_PROXY_AUTH:
    // The proxy rule list parsed to nothing, which is a proxy configuration
    // failure, not a generic network one.
    case net::ERR_NO_SUPPORTED_PROXIES:
      return MN_ERROR_PROXY;
    case net::ERR_CACHE_MISS:
      return MN_ERROR_CACHE_MISS;
    case net::ERR_TIMED_OUT:
    case net::ERR_CONNECTION_TIMED_OUT:
      return MN_ERROR_TIMEOUT;
    case net::ERR_ALPN_NEGOTIATION_FAILED:
    case net::ERR_INVALID_CHUNKED_ENCODING:
    case net::ERR_WS_PROTOCOL_ERROR:
    case net::ERR_HTTP2_PROTOCOL_ERROR:
    case net::ERR_INCOMPLETE_HTTP2_HEADERS:
    case net::ERR_HTTP2_SERVER_REFUSED_STREAM:
    case net::ERR_HTTP2_PING_FAILED:
    case net::ERR_HTTP2_INADEQUATE_TRANSPORT_SECURITY:
    case net::ERR_HTTP2_FLOW_CONTROL_ERROR:
    case net::ERR_HTTP2_FRAME_SIZE_ERROR:
    case net::ERR_HTTP2_COMPRESSION_ERROR:
    case net::ERR_HTTP2_RST_STREAM_NO_ERROR_RECEIVED:
    case net::ERR_HTTP2_STREAM_CLOSED:
    case net::ERR_QUIC_PROTOCOL_ERROR:
    case net::ERR_QUIC_HANDSHAKE_FAILED:
      return MN_ERROR_PROTOCOL;
    case net::ERR_QUIC_CERT_ROOT_NOT_KNOWN:
      return MN_ERROR_TLS;
    case net::ERR_SSL_PROTOCOL_ERROR:
    case net::ERR_SSL_CLIENT_AUTH_CERT_NEEDED:
    case net::ERR_SSL_VERSION_OR_CIPHER_MISMATCH:
    case net::ERR_SSL_RENEGOTIATION_REQUESTED:
    case net::ERR_SSL_NO_RENEGOTIATION:
    case net::ERR_SSL_DECOMPRESSION_FAILURE_ALERT:
    case net::ERR_SSL_BAD_RECORD_MAC_ALERT:
    case net::ERR_SSL_CLIENT_AUTH_PRIVATE_KEY_ACCESS_DENIED:
    case net::ERR_SSL_CLIENT_AUTH_CERT_NO_PRIVATE_KEY:
    case net::ERR_SSL_CLIENT_AUTH_SIGNATURE_FAILED:
    case net::ERR_SSL_PINNED_KEY_NOT_IN_CERT_CHAIN:
    case net::ERR_SSL_DECRYPT_ERROR_ALERT:
    case net::ERR_SSL_SERVER_CERT_CHANGED:
    case net::ERR_SSL_UNRECOGNIZED_NAME_ALERT:
    case net::ERR_SSL_CLIENT_AUTH_CERT_BAD_FORMAT:
    case net::ERR_SSL_SERVER_CERT_BAD_FORMAT:
    case net::ERR_SSL_OBSOLETE_CIPHER:
    case net::ERR_SSL_CLIENT_AUTH_NO_COMMON_ALGORITHMS:
    case net::ERR_SSL_KEY_USAGE_INCOMPATIBLE:
      return MN_ERROR_TLS;
    default:
      return net::IsCertificateError(net_error) ? MN_ERROR_TLS
                                                : MN_ERROR_NETWORK;
  }
}

}  // namespace minicronet

#endif  // MINICRONET_CORE_ERROR_MAPPING_H_
