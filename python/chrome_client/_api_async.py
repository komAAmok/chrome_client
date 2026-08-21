"""Asynchronous convenience API; AsyncSession remains the primary interface."""

from typing import Optional, Dict, Any

from ._response import Response, StreamResponse
from ._client import AsyncSession

_SESSION_KEYS = {
    "verify", "proxies", "proxy", "timeout", "impersonate", "timeout_ms",
    "base_url", "default_domain", "default_headers", "random_tls_extension_order",
}


async def _async_send(method, url, **kwargs):
    session_kwargs = {
        key: kwargs.pop(key) for key in tuple(kwargs) if key in _SESSION_KEYS
    }
    session = AsyncSession(**session_kwargs)
    try:
        response = await session.request(method, url, **kwargs)
        if isinstance(response, StreamResponse):
            response._session = session
        else:
            await session.close()
        return response
    except Exception:
        await session.close()
        raise


async def async_request(method, url, **kwargs):
    return await _async_send(method, url, **kwargs)


async def async_get(url, params=None, **kwargs):
    return await async_request("GET", url, params=params, **kwargs)


async def async_options(url, **kwargs):
    return await async_request("OPTIONS", url, **kwargs)


async def async_head(url, **kwargs):
    kwargs.setdefault("allow_redirects", False)
    return await async_request("HEAD", url, **kwargs)


async def async_post(url, data=None, json=None, **kwargs):
    return await async_request("POST", url, data=data, json=json, **kwargs)


async def async_put(url, data=None, **kwargs):
    return await async_request("PUT", url, data=data, **kwargs)


async def async_patch(url, data=None, **kwargs):
    return await async_request("PATCH", url, data=data, **kwargs)


async def async_delete(url, **kwargs):
    return await async_request("DELETE", url, **kwargs)


async def async_upload_file(
    url: str, file_path: str, *, field_name: str = "file",
    additional_fields: Optional[Dict[str, str]] = None,
    verify: bool = True, timeout: Optional[float] = None,
    impersonate: Optional[str] = "chrome_150", **kwargs,
) -> Response:
    session_kwargs = {
        key: kwargs.pop(key) for key in tuple(kwargs) if key in _SESSION_KEYS
    }
    request_kwargs = {
        key: kwargs.pop(key) for key in ("headers", "cookies") if key in kwargs
    }
    if kwargs:
        raise TypeError("unexpected keyword argument %r" % next(iter(kwargs)))
    async with AsyncSession(
        verify=verify, timeout=timeout, impersonate=impersonate, **session_kwargs
    ) as session:
        return await session.upload_file(
            url, file_path, field_name=field_name,
            additional_fields=additional_fields,
            **request_kwargs,
        )


async def async_download_file(
    url: str, save_path: str, *, verify: bool = True,
    timeout: Optional[float] = None, chunk_size: int = 8192,
    impersonate: Optional[str] = "chrome_150", **kwargs,
) -> Dict[str, Any]:
    session_kwargs = {
        key: kwargs.pop(key) for key in tuple(kwargs) if key in _SESSION_KEYS
    }
    request_kwargs = {
        key: kwargs.pop(key) for key in ("headers", "cookies") if key in kwargs
    }
    if kwargs:
        raise TypeError("unexpected keyword argument %r" % next(iter(kwargs)))
    async with AsyncSession(
        verify=verify, timeout=timeout, impersonate=impersonate, **session_kwargs
    ) as session:
        return await session.download_file(
            url, save_path, chunk_size=chunk_size,
            **request_kwargs,
        )
