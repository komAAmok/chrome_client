from typing import Any, Dict, Optional, Tuple, Union

from . import (
    CookiesType, DataType, HeadersType, HTTPError, PreparedRequest, Request,
    RequestException, Response, Session,
    StreamResponse, Timeout, ConnectionError, ProxyError, SSLError,
)
from ._typing import BrowserTypeLiteral

_ResponseType = Union[Response, StreamResponse]

def request(
    method: str, url: str, *, params: Any = ..., headers: Optional[HeadersType] = ...,
    cookies: Optional[CookiesType] = ..., data: DataType = ..., content: Any = ...,
    json: Any = ..., files: Any = ..., auth: Any = ..., timeout: Any = ...,
    verify: bool = ..., allow_redirects: Optional[bool] = ...,
    proxies: Optional[Union[str, Dict[str, str]]] = ..., proxy: Optional[str] = ...,
    hooks: Any = ..., stream: Optional[bool] = ..., cert: Any = ...,
    impersonate: Optional[BrowserTypeLiteral] = ..., max_redirects: Optional[int] = ...,
    timeout_ms: Optional[int] = ..., base_url: Optional[str] = ...,
    default_domain: Optional[str] = ..., default_headers: bool = ...,
) -> _ResponseType: ...

def session(
    *, verify: bool = ..., proxies: Optional[Union[str, Dict[str, str]]] = ...,
    timeout: Any = ..., impersonate: Optional[BrowserTypeLiteral] = ...,
    headers: Optional[Dict[str, str]] = ..., cookies: Optional[CookiesType] = ...,
    auth: Optional[Tuple[str, str]] = ..., proxy: Optional[str] = ...,
    base_url: Optional[str] = ..., params: Optional[Dict[str, Any]] = ...,
    allow_redirects: bool = ..., max_redirects: int = ...,
    default_headers: bool = ..., timeout_ms: Optional[int] = ...,
    default_domain: Optional[str] = ...,
) -> Session: ...

def get(
    url: str, params: Any = ..., *, headers: Optional[HeadersType] = ...,
    cookies: Optional[CookiesType] = ..., data: DataType = ..., content: Any = ...,
    json: Any = ..., auth: Any = ..., timeout: Any = ..., verify: bool = ...,
    allow_redirects: Optional[bool] = ..., proxies: Any = ..., proxy: Optional[str] = ...,
    hooks: Any = ..., stream: Optional[bool] = ...,
    impersonate: Optional[BrowserTypeLiteral] = ..., max_redirects: Optional[int] = ...,
) -> _ResponseType: ...

def options(
    url: str, *, params: Any = ..., headers: Optional[HeadersType] = ...,
    cookies: Optional[CookiesType] = ..., data: DataType = ..., content: Any = ...,
    json: Any = ..., auth: Any = ..., timeout: Any = ..., verify: bool = ...,
    allow_redirects: Optional[bool] = ..., proxies: Any = ..., proxy: Optional[str] = ...,
    hooks: Any = ..., stream: Optional[bool] = ...,
    impersonate: Optional[BrowserTypeLiteral] = ..., max_redirects: Optional[int] = ...,
) -> _ResponseType: ...

def head(
    url: str, *, params: Any = ..., headers: Optional[HeadersType] = ...,
    cookies: Optional[CookiesType] = ..., auth: Any = ..., timeout: Any = ...,
    verify: bool = ..., allow_redirects: Optional[bool] = ..., proxies: Any = ...,
    proxy: Optional[str] = ..., hooks: Any = ..., stream: Optional[bool] = ...,
    impersonate: Optional[BrowserTypeLiteral] = ..., max_redirects: Optional[int] = ...,
) -> _ResponseType: ...

def post(
    url: str, data: DataType = ..., json: Any = ..., *, params: Any = ...,
    headers: Optional[HeadersType] = ..., cookies: Optional[CookiesType] = ...,
    content: Any = ..., auth: Any = ..., timeout: Any = ..., verify: bool = ...,
    allow_redirects: Optional[bool] = ..., proxies: Any = ..., proxy: Optional[str] = ...,
    hooks: Any = ..., stream: Optional[bool] = ...,
    impersonate: Optional[BrowserTypeLiteral] = ..., max_redirects: Optional[int] = ...,
) -> _ResponseType: ...

def put(
    url: str, data: DataType = ..., *, params: Any = ...,
    headers: Optional[HeadersType] = ..., cookies: Optional[CookiesType] = ...,
    content: Any = ..., json: Any = ..., auth: Any = ..., timeout: Any = ...,
    verify: bool = ..., allow_redirects: Optional[bool] = ..., proxies: Any = ...,
    proxy: Optional[str] = ..., hooks: Any = ..., stream: Optional[bool] = ...,
    impersonate: Optional[BrowserTypeLiteral] = ..., max_redirects: Optional[int] = ...,
) -> _ResponseType: ...

def patch(
    url: str, data: DataType = ..., *, params: Any = ...,
    headers: Optional[HeadersType] = ..., cookies: Optional[CookiesType] = ...,
    content: Any = ..., json: Any = ..., auth: Any = ..., timeout: Any = ...,
    verify: bool = ..., allow_redirects: Optional[bool] = ..., proxies: Any = ...,
    proxy: Optional[str] = ..., hooks: Any = ..., stream: Optional[bool] = ...,
    impersonate: Optional[BrowserTypeLiteral] = ..., max_redirects: Optional[int] = ...,
) -> _ResponseType: ...

def delete(
    url: str, *, params: Any = ..., headers: Optional[HeadersType] = ...,
    cookies: Optional[CookiesType] = ..., data: DataType = ..., content: Any = ...,
    json: Any = ..., auth: Any = ..., timeout: Any = ..., verify: bool = ...,
    allow_redirects: Optional[bool] = ..., proxies: Any = ..., proxy: Optional[str] = ...,
    hooks: Any = ..., stream: Optional[bool] = ...,
    impersonate: Optional[BrowserTypeLiteral] = ..., max_redirects: Optional[int] = ...,
) -> _ResponseType: ...

def trace(
    url: str, *, params: Any = ..., headers: Optional[HeadersType] = ...,
    cookies: Optional[CookiesType] = ..., data: DataType = ..., content: Any = ...,
    json: Any = ..., auth: Any = ..., timeout: Any = ..., verify: bool = ...,
    allow_redirects: Optional[bool] = ..., proxies: Any = ..., proxy: Optional[str] = ...,
    hooks: Any = ..., stream: Optional[bool] = ...,
    impersonate: Optional[BrowserTypeLiteral] = ..., max_redirects: Optional[int] = ...,
) -> _ResponseType: ...

def query(
    url: str, *, params: Any = ..., headers: Optional[HeadersType] = ...,
    cookies: Optional[CookiesType] = ..., data: DataType = ..., content: Any = ...,
    json: Any = ..., auth: Any = ..., timeout: Any = ..., verify: bool = ...,
    allow_redirects: Optional[bool] = ..., proxies: Any = ..., proxy: Optional[str] = ...,
    hooks: Any = ..., stream: Optional[bool] = ...,
    impersonate: Optional[BrowserTypeLiteral] = ..., max_redirects: Optional[int] = ...,
) -> _ResponseType: ...
