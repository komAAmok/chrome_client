"""
Synchronous Session class for chrome_client.
"""

import os
import json as json_lib
import base64
import threading
from typing import Optional, Dict, List, Tuple, Any
from urllib.parse import urlparse, urlencode, urljoin

from ._types import HeadersType, CookiesType, DataType
from ._cookies import CookieJar
from ._response import Request, PreparedRequest, Response, StreamResponse, HTTPStatusError, RequestError
from ._utils import (
    extract_domain,
    prepare_redirect_headers, should_strip_auth,
    validate_headers,
)


def _prepare_request(session, request: Request) -> PreparedRequest:
    if not isinstance(request, Request):
        raise TypeError("request must be a chrome_client.Request")
    if not request.url:
        raise RequestError("URL must be a non-empty string")
    url = urljoin(session.base_url.rstrip('/') + '/', request.url) if session.base_url else request.url
    params = dict(session.params)
    if request.params:
        if isinstance(request.params, dict):
            params.update(request.params)
        else:
            params = list(params.items()) + list(request.params)
    if params:
        url += ('&' if '?' in url else '?') + urlencode(params, doseq=True)
    headers = dict(session.headers)
    headers.update(request.headers)
    body = request.data
    if request.json is not None:
        body = json_lib.dumps(request.json).encode('utf-8')
        if not any(name.lower() == 'content-type' for name in headers):
            headers['Content-Type'] = 'application/json'
    elif isinstance(body, (dict, list, tuple)):
        body = urlencode(body).encode('utf-8')
        if not any(name.lower() == 'content-type' for name in headers):
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
    elif isinstance(body, str):
        body = body.encode('utf-8')
    return PreparedRequest((request.method or 'GET').upper(), url, headers, body)


class Session:
    """Session object - compatible with requests.Session"""

    MAX_REDIRECTS = 30  # Same default as requests

    def __init__(self, client: Any, session_id: str, verify: bool = True,
                 headers: Optional[Dict[str, str]] = None,
                 default_domain: Optional[str] = None,
                 base_url: Optional[str] = None,
                 params: Optional[Dict[str, Any]] = None,
                 auth=None, proxies=None, timeout=30,
                 allow_redirects: bool = True, max_redirects: int = 30,
                 impersonate: Optional[str] = None,
                 random_tls_extension_order: bool = False):
        self._client = client
        self._session_id = session_id
        self._closed = False
        self._verify = verify
        self._cookies = CookieJar(default_domain=default_domain)
        self._state_lock = threading.RLock()
        self._request_sequence = 0
        self._cookie_sequences = {}
        self.headers = dict(headers) if headers else {}
        self.params = dict(params) if params else {}
        self.auth = auth
        self.proxies = proxies or {}
        self.verify = verify
        self.stream = False
        self.cert = None
        self.max_redirects = max_redirects
        self.timeout = timeout
        self.allow_redirects = allow_redirects
        self.impersonate = impersonate
        self.random_tls_extension_order = bool(random_tls_extension_order)
        self.base_url = base_url or ""

    @property
    def cookies(self) -> CookieJar:
        """Get current session's CookieJar"""
        return self._cookies

    @cookies.setter
    def cookies(self, value: CookiesType) -> None:
        """Replace the session cookie jar with a CookieJar or mapping."""
        with self._state_lock:
            if isinstance(value, CookieJar):
                self._cookies = value
            else:
                jar = CookieJar(default_domain=self._cookies.default_domain or None)
                jar.update(value)
                self._cookies = jar
            # An explicitly assigned jar is newer than every already-issued
            # request, so old responses cannot overwrite its auth cookies.
            marker = self._request_sequence + 1
            self._cookie_sequences = {
                cookie.name: marker for cookie in self._cookies.iter_cookies()
            }

    @property
    def proxies(self):
        """Proxy configuration used by the native session."""
        return self._proxies

    @proxies.setter
    def proxies(self, value) -> None:
        # Keep assignment compatible with requests.Session. Client.request
        # compares this value with the native snapshot and rebuilds lazily.
        self._proxies = value

    @property
    def impersonate(self):
        return self._impersonate

    @impersonate.setter
    def impersonate(self, value) -> None:
        self._impersonate = value

    def _adjust_chrome_headers(self, headers: Dict[str, str], method: str, has_body: bool = False, is_json: bool = False) -> Dict[str, str]:
        """
        Adjust existing headers to match Chrome browser behavior.
        Only modifies headers that already exist in the dict.
        """
        # Create copy to avoid modifying original data
        adjusted = headers.copy()

        # Create lowercase key mapping for lookup
        headers_lower_map = {k.lower(): k for k in adjusted.keys()}

        method_upper = method.upper()

        # Adjust headers based on request type
        if method_upper in ('GET', 'HEAD'):
            # GET/HEAD request - navigation mode
            if 'accept' in headers_lower_map:
                original_key = headers_lower_map['accept']
                adjusted[original_key] = 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7'

            if 'sec-fetch-dest' in headers_lower_map:
                original_key = headers_lower_map['sec-fetch-dest']
                adjusted[original_key] = 'document'

            if 'sec-fetch-mode' in headers_lower_map:
                original_key = headers_lower_map['sec-fetch-mode']
                adjusted[original_key] = 'navigate'

            if 'sec-fetch-site' in headers_lower_map:
                original_key = headers_lower_map['sec-fetch-site']
                adjusted[original_key] = 'none'

            if 'sec-fetch-user' in headers_lower_map:
                original_key = headers_lower_map['sec-fetch-user']
                adjusted[original_key] = '?1'

        elif method_upper in ('POST', 'PUT', 'PATCH', 'DELETE'):
            # POST/PUT/PATCH/DELETE request
            if is_json:
                # JSON request
                if 'accept' in headers_lower_map:
                    original_key = headers_lower_map['accept']
                    adjusted[original_key] = 'application/json, text/plain, */*'

            elif has_body:
                # Form request
                if 'accept' in headers_lower_map:
                    original_key = headers_lower_map['accept']
                    adjusted[original_key] = 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'

            else:
                # No body POST/DELETE
                if 'accept' in headers_lower_map:
                    original_key = headers_lower_map['accept']
                    adjusted[original_key] = '*/*'

            # Modify sec-fetch-* headers (if they exist)
            if 'sec-fetch-dest' in headers_lower_map:
                original_key = headers_lower_map['sec-fetch-dest']
                adjusted[original_key] = 'empty'

            if 'sec-fetch-mode' in headers_lower_map:
                original_key = headers_lower_map['sec-fetch-mode']
                adjusted[original_key] = 'cors'

            if 'sec-fetch-site' in headers_lower_map:
                original_key = headers_lower_map['sec-fetch-site']
                adjusted[original_key] = 'same-origin'

            # POST requests usually don't have sec-fetch-user
            if 'sec-fetch-user' in headers_lower_map:
                original_key = headers_lower_map['sec-fetch-user']
                # Remove it for POST requests
                del adjusted[original_key]

        return adjusted

    def _prepare_headers(
        self,
        headers: Optional[HeadersType] = None,
        cookies: Optional[CookiesType] = None,
        request_url: str = "",
        method: str = "GET",
        has_body: bool = False,
        is_json: bool = False,
        need_content_type: Optional[str] = None
    ) -> List[Tuple[str, str]]:
        """Prepare request headers with session defaults"""

        validate_headers(self.headers)
        headers_dict = self._adjust_chrome_headers(
            self.headers, method, has_body=has_body, is_json=is_json
        )
        if headers:
            incoming_headers = dict(headers)
            validate_headers(incoming_headers, allow_none=True)
            for key, value in incoming_headers.items():
                old = next((name for name in headers_dict if name.lower() == key.lower()), None)
                if old is not None:
                    del headers_dict[old]
                if value is not None:
                    headers_dict[key] = value

        # Add content-type if needed (and not already present)
        if need_content_type:
            headers_lower_map = {k.lower(): k for k in headers_dict.keys()}
            if 'content-type' not in headers_lower_map:
                headers_dict['content-type'] = need_content_type

        # Convert to list
        headers_list = list(headers_dict.items())

        # Process cookie, priority headers (keep original logic)
        normal_headers = []
        priority_headers = []
        cookie_headers = []

        for k, v in headers_list:
            k_lower = k.lower()
            if k_lower == 'cookie':
                cookie_headers.append((k, v))
            elif k_lower == 'priority':
                priority_headers.append((k, v))
            else:
                normal_headers.append((k, v))

        cookie_pairs = [
            (cookie.name, cookie.value)
            for cookie in self._cookies.cookies_for_request(request_url)
        ]
        if cookies:
            request_pairs = (
                [(cookie.name, cookie.value) for cookie in cookies.cookies_for_request(request_url)]
                if isinstance(cookies, CookieJar) else list(cookies.items())
            )
            overridden = {name for name, _ in request_pairs}
            cookie_pairs = [pair for pair in cookie_pairs if pair[0] not in overridden]
            cookie_pairs.extend(request_pairs)

        result = normal_headers

        if not cookie_headers and cookie_pairs:
            cookie_str = "; ".join([f"{k}={v}" for k, v in cookie_pairs])
            result.append(("cookie", cookie_str))
        elif cookie_headers:
            result.extend(cookie_headers)

        result.extend(priority_headers)
        validate_headers(dict(result))
        return result

    def _next_request_sequence(self) -> int:
        with self._state_lock:
            self._request_sequence += 1
            return self._request_sequence

    def _update_cookies_from_response(
        self, headers: Dict[str, List[str]], request_url: str,
        request_sequence: int,
    ):
        # Responses can complete out of order. Apply each cookie name in
        # request order so an older in-flight response cannot roll back a newer
        # auth token without blocking unrelated cookie names.
        with self._state_lock:
            for name, values in headers.items():
                if name.lower() == 'set-cookie':
                    for value in values:
                        cookie_name = value.split(';', 1)[0].partition('=')[0].strip()
                        if not cookie_name:
                            continue
                        if request_sequence < self._cookie_sequences.get(cookie_name, -1):
                            continue
                        self._cookies.update_from_set_cookie([value], request_url)
                        self._cookie_sequences[cookie_name] = request_sequence

    def request(
        self,
        method: str,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[HeadersType] = None,
        cookies: Optional[CookiesType] = None,
        data: DataType = None,
        content=None,
        json: Any = None,
        files=None,
        auth=None,
        timeout: Optional[float] = None,
        verify: Optional[bool] = None,
        allow_redirects: Optional[bool] = None,
        proxies=None,
        proxy=None,
        hooks=None,
        stream: Optional[bool] = None,
        cert=None,
        impersonate=None,
        max_redirects: Optional[int] = None,
        **kwargs
    ):
        """Send HTTP request - compatible with requests.request()"""
        if self._closed:
            raise RequestError("Session is closed")

        if not isinstance(method, str) or not method:
            raise RequestError("HTTP method must be a non-empty string")
        if '\x00' in method:
            raise RequestError("HTTP method must not contain NUL bytes")

        if self.base_url:
            url = urljoin(self.base_url.rstrip('/') + '/', url)

        # Validate URL
        if not url or not isinstance(url, str):
            raise RequestError("URL must be a non-empty string")
        if '\x00' in url:
            raise RequestError("URL must not contain NUL bytes")

        # Validate URL format
        parsed = urlparse(url)
        if not parsed.scheme:
            raise RequestError(f"Invalid URL '{url}': No schema supplied. Perhaps you meant http://{url}?")
        if parsed.scheme not in ('http', 'https'):
            raise RequestError(f"Invalid URL '{url}': Unsupported schema '{parsed.scheme}'. Only http and https are supported.")
        if not parsed.netloc:
            raise RequestError(f"Invalid URL '{url}': No host supplied")

        if verify is not None and verify != self.verify:
            raise RequestError("per-request verify cannot differ from the Client setting")
        if cert is not None:
            raise NotImplementedError("client certificates are not supported by this Cronet backend")
        if proxies is not None or proxy is not None:
            configured = proxy if proxy is not None else proxies
            if configured != self.proxies:
                raise RequestError("per-request proxy cannot differ from the Client setting")
        if impersonate is not None and impersonate != self.impersonate:
            raise RequestError("per-request impersonate cannot differ from the Client setting")
        if files is not None:
            raise NotImplementedError("files= is not supported; use upload_file()")
        if content is not None:
            if data is not None or json is not None:
                raise ValueError("content cannot be combined with data or json")
            data = content
        suppress_auth = kwargs.pop('_suppress_auth', False)
        skip_default_params = kwargs.pop('_skip_default_params', False)
        request_sequence = kwargs.pop('_request_sequence', None)
        if request_sequence is None:
            request_sequence = self._next_request_sequence()
        if kwargs and set(kwargs) != {'_redirects_remaining'}:
            name = next(key for key in kwargs if key != '_redirects_remaining')
            raise TypeError("unexpected keyword argument %r" % name)

        allow_redirects = self.allow_redirects if allow_redirects is None else allow_redirects
        stream = self.stream if stream is None else stream
        redirect_limit = self.max_redirects if max_redirects is None else max_redirects
        if not isinstance(redirect_limit, int) or redirect_limit < 0:
            raise ValueError("max_redirects must be a non-negative integer")

        query = {} if skip_default_params else dict(self.params)
        if params:
            if isinstance(params, dict):
                query.update(params)
            else:
                query = list(query.items()) + list(params)
        if query:
            url = url + ('&' if '?' in url else '?') + urlencode(query, doseq=True)

        domain = extract_domain(url)

        # Auto-detect default domain from first request when none was
        # supplied at construction time (fulfils the documented behaviour).
        self._cookies._set_default_domain_if_empty(domain)

        # Per-request cookies are NOT merged into session (matching requests behavior).
        # They are only used for this single request via _prepare_headers.

        if headers is None:
            headers_to_prepare = None
        elif isinstance(headers, dict):
            headers_to_prepare = headers.copy()
        else:
            headers_to_prepare = list(headers)

        request_auth = None if suppress_auth else (self.auth if auth is None else auth)
        if request_auth is not None:
            if not isinstance(request_auth, (tuple, list)) or len(request_auth) != 2:
                raise TypeError("auth must be a (username, password) pair")
            token = base64.b64encode(
                (str(request_auth[0]) + ':' + str(request_auth[1])).encode('latin-1')
            ).decode('ascii')
            auth_header = {'Authorization': 'Basic ' + token}
            if headers_to_prepare:
                auth_header.update(dict(headers_to_prepare))
            headers_to_prepare = auth_header

        # Determine request type
        is_json_request = json is not None
        has_body = data is not None or json is not None
        need_content_type = None

        # JSON accepts any serialisable value, not only dictionaries.
        if json is not None:
            data = json_lib.dumps(json).encode('utf-8')
            need_content_type = 'application/json'

        # Handle data parameter
        elif data is not None:
            if isinstance(data, (dict, list, tuple)):
                data = urlencode(data)
                need_content_type = 'application/x-www-form-urlencoded'

        # Prepare request body
        if data is None:
            body = b""
        elif isinstance(data, dict):
            body = json_lib.dumps(data).encode('utf-8')
        elif isinstance(data, str):
            body = data.encode('utf-8')
        else:
            body = data

        # Prepare headers (pass request type information)
        prepared_headers = self._prepare_headers(
            headers_to_prepare,
            cookies,
            url,
            method=method,
            has_body=has_body,
            is_json=is_json_request,
            need_content_type=need_content_type
        )

        # Always disable redirects at Rust layer, handle in Python
        # Use request_sync for synchronous blocking call
        # Streaming path
        if stream:
            reader = self._client._client.request_stream_sync(
                self._session_id,
                url,
                method.upper(),
                prepared_headers,
                body,
                False  # Always False - handle redirects in Python
            )

            status_code = reader.status_code
            resp_headers_list = list(reader.headers)

            resp_headers = {}
            for name, value in resp_headers_list:
                if name not in resp_headers:
                    resp_headers[name] = []
                resp_headers[name].append(value)

            # Update session cookies from response
            self._update_cookies_from_response(resp_headers, url, request_sequence)

            # Create response CookieJar
            response_cookies = CookieJar()
            for header_name, values in resp_headers.items():
                if header_name.lower() == 'set-cookie':
                    response_cookies.update_from_set_cookie(values, url)

            # Handle redirects for streaming
            if allow_redirects and status_code in (301, 302, 303, 307, 308):
                location = None
                for header_name, values in resp_headers.items():
                    if header_name.lower() == 'location':
                        location = values[0] if values else None
                        break
                if location:
                    reader.close()
                    redirects_remaining = kwargs.get('_redirects_remaining')
                    if redirects_remaining is None:
                        redirects_remaining = redirect_limit
                    if redirects_remaining <= 0:
                        raise RequestError(f"Exceeded maximum redirects ({redirect_limit})")
                    if not location.startswith(('http://', 'https://')):
                        from urllib.parse import urljoin
                        location = urljoin(url, location)
                    switch_to_get = status_code == 303 or (
                        status_code in (301, 302) and method.upper() != 'HEAD'
                    )
                    redirect_method = 'GET' if switch_to_get else method
                    redirect_headers = prepare_redirect_headers(
                        headers_to_prepare, url, location, switch_to_get
                    )
                    return self.request(
                        redirect_method, location,
                        params=None, headers=redirect_headers, cookies=cookies,
                        data=None if switch_to_get else data, json=None,
                        timeout=timeout, verify=verify, allow_redirects=True,
                        stream=True, _redirects_remaining=redirects_remaining - 1,
                        _suppress_auth=should_strip_auth(url, location),
                        _skip_default_params=True,
                        _request_sequence=request_sequence,
                    )

            response = StreamResponse(
                reader, url=url, cookies=response_cookies
            )
            response.request = PreparedRequest(method.upper(), url, dict(prepared_headers), body)
            return response

        # Non-streaming path
        response_dict = self._client._client.request_sync(
            self._session_id,
            url,
            method.upper(),
            prepared_headers,
            body,
            False  # Always False - handle redirects in Python
        )

        status_code = response_dict['status_code']
        resp_headers_list = response_dict['headers']
        body_bytes = response_dict['body']

        resp_headers = {}
        for name, value in resp_headers_list:
            if name not in resp_headers:
                resp_headers[name] = []
            resp_headers[name].append(value)

        # Create response CookieJar
        response_cookies = CookieJar()
        for header_name, values in resp_headers.items():
            if header_name.lower() == 'set-cookie':
                response_cookies.update_from_set_cookie(values, url)

        # Update session cookies from response
        self._update_cookies_from_response(resp_headers, url, request_sequence)

        # Handle redirects in Python layer
        if allow_redirects and status_code in (301, 302, 303, 307, 308):
            location = None
            for header_name, values in resp_headers.items():
                if header_name.lower() == 'location':
                    location = values[0] if values else None
                    break

            if location:
                # Enforce redirect depth limit
                redirects_remaining = kwargs.get('_redirects_remaining')
                if redirects_remaining is None:
                    redirects_remaining = redirect_limit
                if redirects_remaining <= 0:
                    raise RequestError(
                        f"Exceeded maximum redirects ({redirect_limit})"
                    )

                # Handle relative URLs
                if not location.startswith(('http://', 'https://')):
                    from urllib.parse import urljoin
                    location = urljoin(url, location)

                # Follow redirect with updated cookies and headers
                # For 303, change method to GET
                switch_to_get = status_code == 303 or (
                    status_code in (301, 302) and method.upper() != 'HEAD'
                )
                redirect_method = 'GET' if switch_to_get else method
                redirect_headers = prepare_redirect_headers(
                    headers_to_prepare, url, location, switch_to_get
                )

                current = Response(
                    status_code=status_code, _headers=resp_headers,
                    content=body_bytes, url=url, _cookies=response_cookies,
                )
                current.request = PreparedRequest(
                    method.upper(), url, dict(prepared_headers), body
                )
                followed = self.request(
                    redirect_method,
                    location,
                    params=None,  # Don't carry params on redirect
                    headers=redirect_headers,
                    cookies=cookies,  # Carry per-request cookies through redirect
                    data=None if switch_to_get else data,
                    json=None,
                    timeout=timeout,
                    verify=verify,
                    allow_redirects=True,  # Continue following redirects
                    _redirects_remaining=redirects_remaining - 1,
                    _suppress_auth=should_strip_auth(url, location),
                    _skip_default_params=True,
                    _request_sequence=request_sequence,
                )
                followed.history = [current] + list(followed.history)
                return followed

        response = Response(
            status_code=status_code,
            _headers=resp_headers,
            content=body_bytes,
            url=url,
            _cookies=response_cookies
        )
        response.request = PreparedRequest(method.upper(), url, dict(prepared_headers), body)
        if hooks:
            callbacks = hooks.get('response', []) if isinstance(hooks, dict) else []
            if callable(callbacks):
                callbacks = [callbacks]
            for callback in callbacks:
                replacement = callback(response)
                if replacement is not None:
                    response = replacement
        return response

    def get(self, url: str, params=None, **kwargs):
        return self.request("GET", url, params=params, **kwargs)

    def options(self, url: str, **kwargs):
        return self.request("OPTIONS", url, **kwargs)

    def head(self, url: str, **kwargs):
        kwargs.setdefault("allow_redirects", False)
        return self.request("HEAD", url, **kwargs)

    def post(self, url: str, data=None, json=None, **kwargs):
        return self.request("POST", url, data=data, json=json, **kwargs)

    def put(self, url: str, data=None, **kwargs):
        return self.request("PUT", url, data=data, **kwargs)

    def patch(self, url: str, data=None, **kwargs):
        return self.request("PATCH", url, data=data, **kwargs)

    def delete(self, url: str, **kwargs):
        return self.request("DELETE", url, **kwargs)

    def trace(self, url: str, **kwargs):
        return self.request("TRACE", url, **kwargs)

    def query(self, url: str, **kwargs):
        return self.request("QUERY", url, **kwargs)

    def prepare_request(self, request: Request) -> PreparedRequest:
        return _prepare_request(self, request)

    def send(self, request: PreparedRequest, **kwargs):
        if not isinstance(request, PreparedRequest):
            raise TypeError("request must be a PreparedRequest")
        return self.request(
            request.method, request.url, headers=request.headers,
            data=request.body, _skip_default_params=True, **kwargs
        )

    def upload_file(
        self,
        url: str,
        file_path: str,
        *,
        field_name: str = "file",
        additional_fields: Optional[Dict[str, str]] = None,
        headers: Optional[HeadersType] = None,
        cookies: Optional[CookiesType] = None,
        timeout: Optional[float] = None,
        verify: Optional[bool] = None
    ) -> Response:
        """Upload file"""
        import mimetypes

        # Read file
        if not os.path.exists(file_path):
            raise RequestError(f"File not found: {file_path}")

        with open(file_path, 'rb') as f:
            file_content = f.read()

        # Get filename and MIME type
        filename = os.path.basename(file_path)
        mime_type, _ = mimetypes.guess_type(file_path)
        if mime_type is None:
            mime_type = 'application/octet-stream'

        # Build multipart/form-data
        boundary = f'----ChromeClientFormBoundary{os.urandom(16).hex()}'
        body_parts = []

        # Add additional fields
        if additional_fields:
            for key, value in additional_fields.items():
                body_parts.append(f'--{boundary}\r\n'.encode())
                body_parts.append(f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode())
                body_parts.append(f'{value}\r\n'.encode())

        # Add file
        body_parts.append(f'--{boundary}\r\n'.encode())
        body_parts.append(
            f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'.encode()
        )
        body_parts.append(f'Content-Type: {mime_type}\r\n\r\n'.encode())
        body_parts.append(file_content)
        body_parts.append(b'\r\n')
        body_parts.append(f'--{boundary}--\r\n'.encode())

        # Merge body
        body = b''.join(body_parts)

        # Set Content-Type
        if headers is None:
            headers = {}
        elif isinstance(headers, list):
            headers = dict(headers)
        else:
            headers = dict(headers)

        headers['Content-Type'] = f'multipart/form-data; boundary={boundary}'

        # Send request
        return self.request(
            "POST",
            url,
            headers=headers,
            cookies=cookies,
            data=body,
            timeout=timeout,
            verify=verify
        )

    def download_file(
        self,
        url: str,
        save_path: str,
        *,
        headers: Optional[HeadersType] = None,
        cookies: Optional[CookiesType] = None,
        timeout: Optional[float] = None,
        verify: Optional[bool] = None,
        chunk_size: int = 8192
    ) -> Dict[str, Any]:
        """Download file"""
        if not isinstance(chunk_size, int) or chunk_size <= 0:
            raise ValueError("chunk_size must be a positive integer")
        response = self.get(
            url,
            headers=headers,
            cookies=cookies,
            timeout=timeout,
            verify=verify,
            stream=True,
        )
        try:
            response.raise_for_status()
            save_dir = os.path.dirname(save_path)
            if save_dir:
                os.makedirs(save_dir, exist_ok=True)
            size = 0
            with open(save_path, 'wb') as file:
                for chunk in response.iter_content(chunk_size):
                    file.write(chunk)
                    size += len(chunk)
            return {
                'file_path': save_path,
                'size': size,
                'status_code': response.status_code,
                'headers': response.headers,
            }
        finally:
            response.close()

    def websocket(self, url, *, on_open=None, on_message=None, on_close=None,
                  on_error=None, sub_protocols=None, origin=None, headers=None):
        """Create a callback-based WebSocket connection.

        Args:
            url: WebSocket URL (ws:// or wss://)
            on_open: callback(ws) - called when connected
            on_message: callback(ws, message) - called on message
            on_close: callback(ws, code, reason) - called on close
            on_error: callback(ws, error) - called on error
            headers: list of (name, value) tuples for custom HTTP headers

        Returns:
            WebSocketApp instance. Call .run_forever() or .run_in_background() to start.
        """
        from ._websocket import WebSocketApp
        return WebSocketApp(
            self,
            url,
            on_open=on_open,
            on_message=on_message,
            on_close=on_close,
            on_error=on_error,
            sub_protocols=sub_protocols,
            origin=origin,
            headers=headers,
        )

    def close(self):
        """Close session"""
        with self._state_lock:
            if not self._closed:
                # Publish the closed state before native teardown so another
                # Python thread cannot start work on a session being destroyed.
                self._closed = True
                self._client._client.close_session(self._session_id)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
