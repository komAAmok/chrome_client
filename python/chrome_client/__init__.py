"""
Chrome Client - Python Client for Cronet-Cloak

使用方式:
    import chrome_client

    # 同步模式 - 模块级函数
    response = chrome_client.get("https://example.com", verify=False)

    # 同步模式 - Session
    session = chrome_client.Client(verify=False)
    response = session.get("https://example.com")

    # 异步模式 - 模块级函数
    import asyncio
    response = await chrome_client.async_get("https://example.com", verify=False)

    # 异步模式 - AsyncSession
    async with chrome_client.AsyncClient(verify=False) as session:
        response = await session.get("https://example.com")

    # 使用代理
    session = chrome_client.Client(
        verify=False,
        proxies={"https": "http://127.0.0.1:8080"}
    )

    # Response 对象
    print(response.status_code)
    print(response.text)
    print(response.json())
"""

import sys
import os

# Load native libraries first (must be before importing Rust extension)
from ._native_loader import load_native_libraries
load_native_libraries()

# Import Rust extension module
try:
    from . import cronet_cloak as _cronet_cloak
except ImportError as e:
    # If import fails, provide helpful error message
    if sys.platform == "linux" and "libcronet" in str(e):
        package_dir = os.path.dirname(__file__)
        raise ImportError(
            f"Failed to load libcronet.so: {e}\n\n"
            f"Quick fix: Run this command before starting Python:\n"
            f"  export LD_LIBRARY_PATH={package_dir}:$LD_LIBRARY_PATH\n\n"
            f"See LINUX_INSTALL_GUIDE.md for more solutions."
        ) from e
    else:
        raise

# Import public API
from ._types import HeadersType, CookiesType, DataType
from ._typing import BrowserTypeLiteral
from ._cookies import Cookie, CookieJar
from ._response import (
    Request, PreparedRequest, Response, StreamResponse, HTTPStatusError, RequestError,
    Timeout, ConnectionError, ProxyError, SSLError,
)
from ._client import (
    Client, AsyncClient, Session, AsyncSession,
    set_tls_profiles, add_tls_profile, get_tls_profiles, clear_tls_profiles_cache,
    _TLS_PROFILES_CACHE
)
from ._api_sync import (
    request, session, get, post, put, delete, patch, head, options, trace, query,
    upload_file, download_file
)
from ._api_async import (
    async_request, async_get, async_post, async_put, async_delete, async_patch,
    async_head, async_options, async_upload_file, async_download_file
)
from ._websocket import WebSocketApp
from . import requests as requests

RequestException = RequestError
HTTPError = HTTPStatusError

__all__ = [
    "Client", "Session", "Request", "PreparedRequest", "Response", "StreamResponse", "HTTPStatusError", "RequestError",
    "Cookie", "CookieJar",
    "requests", "request", "session", "get", "post", "put", "delete", "patch", "head", "options", "trace", "query",
    "upload_file", "download_file",
    "AsyncClient", "AsyncSession",
    "async_request", "async_get", "async_post", "async_put", "async_delete", "async_patch",
    "async_head", "async_options", "async_upload_file", "async_download_file",
    "set_tls_profiles", "add_tls_profile", "get_tls_profiles", "clear_tls_profiles_cache",
    "WebSocketApp"
]

__all__ += ["BrowserTypeLiteral"]

__all__ += [
    "RequestException", "HTTPError", "Timeout", "ConnectionError",
    "ProxyError", "SSLError",
]
