"""Requests-first synchronous module API."""

from typing import Optional, Dict, Any

from ._types import HeadersType, CookiesType
from ._response import Response, StreamResponse
from ._client import Session

_SESSION_KEYS = {
    "verify", "proxies", "proxy", "timeout", "impersonate", "timeout_ms",
    "base_url", "default_domain", "default_headers",
}


def _send(method, url, **kwargs):
    """Create one temporary Session, preserving it only for streamed responses."""
    session_kwargs = {
        key: kwargs.pop(key) for key in tuple(kwargs) if key in _SESSION_KEYS
    }
    session = Session(**session_kwargs)
    try:
        response = session.request(method, url, **kwargs)
        if isinstance(response, StreamResponse):
            response._session = session
        else:
            session.close()
        return response
    except Exception:
        session.close()
        raise


def request(method, url, **kwargs):
    return _send(method, url, **kwargs)


def session(**kwargs):
    return Session(**kwargs)


def get(url, params=None, **kwargs):
    return request("GET", url, params=params, **kwargs)


def options(url, **kwargs):
    return request("OPTIONS", url, **kwargs)


def head(url, **kwargs):
    kwargs.setdefault("allow_redirects", False)
    return request("HEAD", url, **kwargs)


def post(url, data=None, json=None, **kwargs):
    return request("POST", url, data=data, json=json, **kwargs)


def put(url, data=None, **kwargs):
    return request("PUT", url, data=data, **kwargs)


def patch(url, data=None, **kwargs):
    return request("PATCH", url, data=data, **kwargs)


def delete(url, **kwargs):
    return request("DELETE", url, **kwargs)


def trace(url, **kwargs):
    return request("TRACE", url, **kwargs)


def query(url, **kwargs):
    return request("QUERY", url, **kwargs)


def upload_file(
    url: str,
    file_path: str,
    *,
    field_name: str = "file",
    additional_fields: Optional[Dict[str, str]] = None,
    headers: Optional[HeadersType] = None,
    cookies: Optional[CookiesType] = None,
    timeout: Optional[float] = None,
    verify: bool = True,
    impersonate: Optional[str] = "chrome_150",
    **kwargs: Any
) -> Response:
    with Session(verify=verify, timeout=timeout, impersonate=impersonate, **kwargs) as session:
        return session.upload_file(
            url, file_path, field_name=field_name,
            additional_fields=additional_fields, headers=headers, cookies=cookies,
        )


def download_file(
    url: str,
    save_path: str,
    *,
    headers: Optional[HeadersType] = None,
    cookies: Optional[CookiesType] = None,
    timeout: Optional[float] = None,
    verify: bool = True,
    chunk_size: int = 8192,
    impersonate: Optional[str] = "chrome_150",
    **kwargs: Any
) -> Dict[str, Any]:
    with Session(verify=verify, timeout=timeout, impersonate=impersonate, **kwargs) as session:
        return session.download_file(
            url, save_path, headers=headers, cookies=cookies, chunk_size=chunk_size,
        )
