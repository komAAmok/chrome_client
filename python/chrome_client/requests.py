"""Requests-compatible namespace backed by chrome_client's Cronet transport."""

from ._client import Session
from ._response import (
    Request, PreparedRequest, Response, HTTPStatusError, RequestError,
    Timeout, ConnectionError, ProxyError, SSLError,
)
from ._api_sync import (
    request, session, get, options, head, post, put, patch, delete, trace, query,
)
from ._typing import BrowserTypeLiteral

RequestException = RequestError
HTTPError = HTTPStatusError

__all__ = [
    "Session", "Request", "PreparedRequest", "Response", "RequestException", "HTTPError",
    "Timeout", "ConnectionError", "ProxyError", "SSLError",
    "request", "session", "get", "options", "head", "post", "put", "patch",
    "delete", "trace", "query",
    "BrowserTypeLiteral",
]
