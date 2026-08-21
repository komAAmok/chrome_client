"""
Client factory functions for creating Client and AsyncClient.
"""

import os
import json as json_lib
import random
import threading
from copy import deepcopy
from functools import wraps
from typing import Optional, Union, Dict, List
from urllib.parse import urlparse

from ._session import Session as _SyncSession
from ._async_session import AsyncSession as _AsyncSession
from ._response import ConnectionError, RequestError, Timeout


# Module-level cache for TLS profiles (loaded once on first use)
# Users can directly set this to customize TLS profiles without modifying tls_profiles.json
_TLS_PROFILES_CACHE: Optional[Dict[str, Dict]] = None
_TLS_PROFILES_LOCK = threading.RLock()


def _load_tls_profiles() -> Dict[str, Dict]:
    """Load all TLS profiles from tls_profiles.json (cached)

    If _TLS_PROFILES_CACHE is already set (by user or previous load), return it directly.
    Otherwise, load from tls_profiles.json file.
    """
    global _TLS_PROFILES_CACHE
    with _TLS_PROFILES_LOCK:
        # If user has set the cache directly, use it
        if _TLS_PROFILES_CACHE is not None:
            return _TLS_PROFILES_CACHE

        config_path = os.path.join(os.path.dirname(__file__), "tls_profiles.json")
        if not os.path.exists(config_path):
            raise RequestError("TLS profile file not found: %s" % config_path)

        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                _TLS_PROFILES_CACHE = json_lib.load(f)
                return _TLS_PROFILES_CACHE
        except (OSError, ValueError) as exc:
            raise RequestError("Failed to load TLS profiles: %s" % exc) from exc


def set_tls_profiles(profiles: Dict[str, Dict]) -> None:
    """Set custom TLS profiles (replaces file-based profiles)

    Args:
        profiles: Dictionary of TLS profiles, e.g.:
            {
                "chrome_test": {
                    "version": "Chrome test",
                    "cipher_suites": ["TLS_AES_128_GCM_SHA256", ...],
                    "tls_curves": ["X25519", ...],
                    "tls_extensions": [...],
                    "signature_algorithms": [...]
                }
            }

    Example:
        import chrome_client
        chrome_client.set_tls_profiles({
            "chrome_test": {
                "cipher_suites": ["TLS_ECDHE_RSA_WITH_AES_256_CBC_SHA"],
                "tls_curves": ["X25519"],
                "tls_extensions": [],
                "signature_algorithms": []
            }
        })
        session = chrome_client.Client(impersonate="chrome_test")
    """
    global _TLS_PROFILES_CACHE
    if not isinstance(profiles, dict):
        raise TypeError("profiles must be a dictionary")
    with _TLS_PROFILES_LOCK:
        _TLS_PROFILES_CACHE = deepcopy(profiles)


def add_tls_profile(name: str, profile: Dict) -> None:
    """Add or update a single TLS profile

    Args:
        name: Profile name (e.g., "chrome_test")
        profile: Profile configuration with cipher_suites, tls_curves, tls_extensions,
            signature_algorithms

    Example:
        import chrome_client
        chrome_client.add_tls_profile("chrome_test", {
            "cipher_suites": ["TLS_ECDHE_RSA_WITH_AES_256_CBC_SHA"],
            "tls_curves": ["X25519"],
            "tls_extensions": [],
            "signature_algorithms": []
        })
    """
    if not isinstance(name, str) or not name:
        raise ValueError("profile name must be a non-empty string")
    if not isinstance(profile, dict):
        raise TypeError("profile must be a dictionary")
    with _TLS_PROFILES_LOCK:
        profiles = _load_tls_profiles()
        profiles[name] = deepcopy(profile)


def get_tls_profiles() -> Dict[str, Dict]:
    """Get all loaded TLS profiles

    Returns:
        Dictionary of all TLS profiles
    """
    with _TLS_PROFILES_LOCK:
        return deepcopy(_load_tls_profiles())


def clear_tls_profiles_cache() -> None:
    """Clear TLS profiles cache, forcing reload from file on next use"""
    global _TLS_PROFILES_CACHE
    with _TLS_PROFILES_LOCK:
        _TLS_PROFILES_CACHE = None


def _load_tls_profile(impersonate: Optional[str] = None) -> Optional[Dict[str, List[str]]]:
    """Get TLS fingerprint configuration for a specific profile"""
    if impersonate is None:
        return None

    with _TLS_PROFILES_LOCK:
        profiles = _load_tls_profiles()
        if impersonate not in profiles:
            available = ', '.join(sorted(profiles.keys())) if profiles else 'none'
            raise RequestError(
                f"TLS profile '{impersonate}' not found. Available profiles: {available}"
            )

        profile = profiles[impersonate]
        if not isinstance(profile, dict):
            raise RequestError("TLS profile %r must be a dictionary" % impersonate)
        result = {}
        for key in (
            "cipher_suites", "tls_curves", "tls_extensions",
            "signature_algorithms",
        ):
            value = profile.get(key, []) or []
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise RequestError("TLS profile %r field %s must be a list of strings" % (
                    impersonate, key
                ))
            # Take an immutable-by-convention snapshot. Callers may update the
            # profile registry concurrently while new sessions are being created.
            result[key] = list(value)
        return result


def _extract_base_url_host(base_url: Optional[str]) -> str:
    """Return the normalised host from *base_url*, or empty string."""
    if not base_url:
        return ""
    parsed = urlparse(base_url)
    host = (parsed.hostname or "").lower()
    if host.startswith("."):
        host = host[1:]
    return host


def _create_session_with_tls_profile(
    client,
    proxy_rules,
    skip_cert_verify,
    timeout_ms,
    cipher_suites,
    tls_curves,
    tls_extensions,
    signature_algorithms,
):
    try:
        return client.create_session(
            proxy_rules,
            skip_cert_verify,
            timeout_ms,
            cipher_suites,
            tls_curves,
            tls_extensions,
            signature_algorithms,
        )
    except TypeError as exc:
        if signature_algorithms:
            raise RequestError(
                "signature_algorithms requires a chrome_client native extension "
                "rebuilt with tls_signature_algorithms support"
            ) from exc
        return client.create_session(
            proxy_rules,
            skip_cert_verify,
            timeout_ms,
            cipher_suites,
            tls_curves,
            tls_extensions,
        )


def _validate_proxy_url(proxy_url: str) -> None:
    """Validate proxy URL format"""
    if not proxy_url or not isinstance(proxy_url, str):
        raise RequestError("Proxy URL must be a non-empty string")
    if '\x00' in proxy_url:
        raise RequestError("Proxy URL must not contain NUL bytes")

    # Parse proxy URL
    try:
        parsed = urlparse(proxy_url)
    except ValueError as e:
        raise RequestError(f"Invalid proxy URL '{proxy_url}': {e}")

    # Check scheme
    if not parsed.scheme:
        raise RequestError(f"Invalid proxy URL '{proxy_url}': No schema supplied")

    # Supported proxy protocols
    supported_schemes = ('http', 'https', 'socks5', 'socks5h')
    if parsed.scheme not in supported_schemes:
        raise RequestError(
            f"Invalid proxy URL '{proxy_url}': Unsupported schema '{parsed.scheme}'. "
            f"Supported schemas: {', '.join(supported_schemes)}"
        )

    # Check host
    if not parsed.netloc:
        raise RequestError(f"Invalid proxy URL '{proxy_url}': No host supplied")

    # Extract hostname (without auth and port)
    hostname = parsed.hostname
    if not hostname:
        raise RequestError(f"Invalid proxy URL '{proxy_url}': No hostname supplied")

    # Validate IP address if it looks like one
    if hostname.replace('.', '').replace(':', '').isdigit() or ':' in hostname:
        # IPv4 validation
        if '.' in hostname and ':' not in hostname:
            parts = hostname.split('.')
            if len(parts) != 4:
                raise RequestError(f"Invalid proxy URL '{proxy_url}': Invalid IPv4 address '{hostname}'")
            for part in parts:
                try:
                    num = int(part)
                    if not (0 <= num <= 255):
                        raise RequestError(
                            f"Invalid proxy URL '{proxy_url}': Invalid IPv4 address '{hostname}' "
                            f"(octet {part} must be 0-255)"
                        )
                except ValueError:
                    raise RequestError(f"Invalid proxy URL '{proxy_url}': Invalid IPv4 address '{hostname}'")

    # Check port (if provided)
    try:
        if parsed.port is not None:
            if not (1 <= parsed.port <= 65535):
                raise RequestError(f"Invalid proxy URL '{proxy_url}': Port must be between 1 and 65535")
    except ValueError as e:
        raise RequestError(f"Invalid proxy URL '{proxy_url}': {e}")


def _normalise_timeout(timeout, timeout_ms):
    """Return Cronet's millisecond timeout without silently truncating values."""
    if timeout_ms is not None:
        if timeout != 30:
            raise TypeError("timeout and timeout_ms are mutually exclusive")
        value = timeout_ms
    elif timeout is None:
        # ponytail: native Cronet cannot represent Requests' unbounded timeout;
        # use its safe default until the native API gains optional deadlines.
        value = 30000
    elif isinstance(timeout, tuple):
        if len(timeout) != 2:
            raise ValueError("timeout tuple must be (connect, read)")
        if any(not isinstance(item, (int, float)) or item < 0 for item in timeout):
            raise ValueError("timeout values must be non-negative numbers")
        value = sum(timeout) * 1000
    else:
        value = timeout * 1000
    if not isinstance(value, (int, float)) or value < 0:
        raise ValueError("timeout must be None, a non-negative number, or a pair")
    return int(value)


def _normalise_proxy(proxies, proxy):
    if proxies is not None and proxy is not None:
        raise TypeError("proxy and proxies are mutually exclusive")
    configured = proxy if proxy is not None else proxies
    if isinstance(configured, dict):
        configured = (configured.get("https") or configured.get("http") or
                      configured.get("all") or configured.get("all://"))
    if configured:
        _validate_proxy_url(configured)
        if configured.startswith("socks5h://"):
            configured = "socks5://" + configured[len("socks5h://"):]
    return configured


def _create_native_session(
    verify, proxies, proxy, timeout, timeout_ms, impersonate,
    random_tls_extension_order,
):
    from .cronet_cloak import PyCronetClient

    profile = _load_tls_profile(impersonate)
    if profile and random_tls_extension_order:
        # Cronet consumes this list in order. This changes the ClientHello
        # extension order without mutating the shared profile cache.
        profile = dict(profile)
        profile["tls_extensions"] = list(profile.get("tls_extensions") or [])
        random.SystemRandom().shuffle(profile["tls_extensions"])
    client = PyCronetClient()
    session_id = _create_session_with_tls_profile(
        client,
        _normalise_proxy(proxies, proxy),
        not verify,
        _normalise_timeout(timeout, timeout_ms),
        profile.get("cipher_suites", []) if profile else None,
        profile.get("tls_curves", []) if profile else None,
        profile.get("tls_extensions", []) if profile else None,
        profile.get("signature_algorithms", []) if profile else None,
    )
    if not session_id:
        raise RequestError("Cronet failed to create a native session")

    class _ClientWrapper:
        def __init__(self, native):
            self._client = native

    return _ClientWrapper(client), session_id


class Client(_SyncSession):
    """Lowest-level synchronous client; also implements the Session API."""

    def __init__(
        self,
        verify: bool = True,
        proxies: Optional[Union[str, Dict[str, str]]] = None,
        timeout: Optional[Union[float, tuple]] = 30,
        impersonate: Optional[str] = "chrome_150",
        headers: Optional[Dict[str, str]] = None,
        cookies=None,
        auth=None,
        proxy: Optional[str] = None,
        base_url: Optional[str] = None,
        params: Optional[Dict] = None,
        allow_redirects: bool = True,
        max_redirects: int = 30,
        default_headers: bool = True,
        timeout_ms: Optional[int] = None,
        default_domain: Optional[str] = None,
        random_tls_extension_order: bool = False,
    ):
        if not isinstance(random_tls_extension_order, bool):
            raise TypeError("random_tls_extension_order must be a bool")
        if not isinstance(max_redirects, int) or max_redirects < 0:
            raise ValueError("max_redirects must be a non-negative integer")
        wrapper, session_id = _create_native_session(
            verify, proxies, proxy, timeout, timeout_ms, impersonate,
            random_tls_extension_order,
        )
        super().__init__(
            wrapper,
            session_id,
            verify,
            headers=headers if default_headers else None,
            default_domain=(default_domain or _extract_base_url_host(base_url) or None),
            base_url=base_url,
            params=params,
            auth=auth,
            proxies=proxy if proxy is not None else proxies,
            timeout=timeout,
            allow_redirects=allow_redirects,
            max_redirects=max_redirects,
            impersonate=impersonate,
            random_tls_extension_order=random_tls_extension_order,
        )
        if cookies:
            self.cookies.update(cookies)

    @wraps(_SyncSession.request)
    def request(self, method, url, *args, **kwargs):
        overrides = {
            key: kwargs[key] for key in ("verify", "timeout", "impersonate")
            if key in kwargs and kwargs[key] is not None
        }
        if "proxy" in kwargs and kwargs["proxy"] is not None:
            overrides["proxy"] = kwargs["proxy"]
        elif "proxies" in kwargs and kwargs["proxies"] is not None:
            overrides["proxies"] = kwargs["proxies"]

        current = {
            "verify": self.verify,
            "timeout": self.timeout,
            "impersonate": self.impersonate,
            "proxies": self.proxies,
            "proxy": self.proxies,
        }
        if not any(current.get(key) != value for key, value in overrides.items()):
            try:
                return super().request(method, url, *args, **kwargs)
            except (RequestError, TypeError, ValueError, NotImplementedError):
                raise
            except TimeoutError as exc:
                raise Timeout(str(exc)) from exc
            except OSError as exc:
                raise ConnectionError(str(exc)) from exc
            except Exception as exc:
                raise RequestError(str(exc)) from exc

        child_options = dict(
            verify=overrides.get("verify", self.verify),
            timeout=overrides.get("timeout", self.timeout),
            impersonate=overrides.get("impersonate", self.impersonate),
            headers=self.headers,
            cookies=self.cookies,
            auth=self.auth,
            base_url=self.base_url or None,
            params=self.params,
            allow_redirects=self.allow_redirects,
            max_redirects=self.max_redirects,
            random_tls_extension_order=self.random_tls_extension_order,
        )
        if "proxy" in overrides:
            child_options["proxy"] = overrides["proxy"]
        else:
            child_options["proxies"] = overrides.get("proxies", self.proxies)
        child = Client(**child_options)
        try:
            response = child.request(method, url, *args, **kwargs)
            self.cookies.update(child.cookies)
            if hasattr(response, "_session"):
                response._session = child
            else:
                child.close()
            return response
        except Exception:
            child.close()
            raise


class AsyncClient(_AsyncSession):
    """Lowest-level asynchronous client with the same request surface as Client."""

    def __init__(
        self,
        verify: bool = True,
        proxies: Optional[Union[str, Dict[str, str]]] = None,
        timeout: Optional[Union[float, tuple]] = 30,
        impersonate: Optional[str] = "chrome_150",
        headers: Optional[Dict[str, str]] = None,
        cookies=None,
        auth=None,
        proxy: Optional[str] = None,
        base_url: Optional[str] = None,
        params: Optional[Dict] = None,
        allow_redirects: bool = True,
        max_redirects: int = 30,
        default_headers: bool = True,
        timeout_ms: Optional[int] = None,
        default_domain: Optional[str] = None,
        random_tls_extension_order: bool = False,
    ):
        if not isinstance(random_tls_extension_order, bool):
            raise TypeError("random_tls_extension_order must be a bool")
        if not isinstance(max_redirects, int) or max_redirects < 0:
            raise ValueError("max_redirects must be a non-negative integer")
        wrapper, session_id = _create_native_session(
            verify, proxies, proxy, timeout, timeout_ms, impersonate,
            random_tls_extension_order,
        )
        super().__init__(
            wrapper,
            session_id,
            verify,
            headers=headers if default_headers else None,
            default_domain=(default_domain or _extract_base_url_host(base_url) or None),
            base_url=base_url,
            params=params,
            auth=auth,
            proxies=proxy if proxy is not None else proxies,
            timeout=timeout,
            allow_redirects=allow_redirects,
            max_redirects=max_redirects,
            impersonate=impersonate,
            random_tls_extension_order=random_tls_extension_order,
        )
        if cookies:
            self.cookies.update(cookies)

    @wraps(_AsyncSession.request)
    async def request(self, method, url, *args, **kwargs):
        overrides = {
            key: kwargs[key] for key in ("verify", "timeout", "impersonate")
            if key in kwargs and kwargs[key] is not None
        }
        if "proxy" in kwargs and kwargs["proxy"] is not None:
            overrides["proxy"] = kwargs["proxy"]
        elif "proxies" in kwargs and kwargs["proxies"] is not None:
            overrides["proxies"] = kwargs["proxies"]
        current = {
            "verify": self.verify,
            "timeout": self.timeout,
            "impersonate": self.impersonate,
            "proxies": self.proxies,
            "proxy": self.proxies,
        }
        if not any(current.get(key) != value for key, value in overrides.items()):
            try:
                return await super().request(method, url, *args, **kwargs)
            except (RequestError, TypeError, ValueError, NotImplementedError):
                raise
            except TimeoutError as exc:
                raise Timeout(str(exc)) from exc
            except OSError as exc:
                raise ConnectionError(str(exc)) from exc
            except Exception as exc:
                raise RequestError(str(exc)) from exc

        child_options = dict(
            verify=overrides.get("verify", self.verify),
            timeout=overrides.get("timeout", self.timeout),
            impersonate=overrides.get("impersonate", self.impersonate),
            headers=self.headers,
            cookies=self.cookies,
            auth=self.auth,
            base_url=self.base_url or None,
            params=self.params,
            allow_redirects=self.allow_redirects,
            max_redirects=self.max_redirects,
            random_tls_extension_order=self.random_tls_extension_order,
        )
        if "proxy" in overrides:
            child_options["proxy"] = overrides["proxy"]
        else:
            child_options["proxies"] = overrides.get("proxies", self.proxies)
        child = AsyncClient(**child_options)
        try:
            response = await child.request(method, url, *args, **kwargs)
            self.cookies.update(child.cookies)
            if hasattr(response, "_session"):
                response._session = child
            else:
                await child.close()
            return response
        except Exception:
            await child.close()
            raise


class Session(Client):
    """Requests-compatible public session backed by Client."""

    def __init__(
        self,
        verify: bool = True,
        proxies: Optional[Union[str, Dict[str, str]]] = None,
        timeout: Optional[Union[float, tuple]] = None,
        impersonate: Optional[str] = "chrome_150",
        headers: Optional[Dict[str, str]] = None,
        cookies=None,
        auth=None,
        proxy: Optional[str] = None,
        base_url: Optional[str] = None,
        params: Optional[Dict] = None,
        allow_redirects: bool = True,
        max_redirects: int = 30,
        default_headers: bool = True,
        timeout_ms: Optional[int] = None,
        default_domain: Optional[str] = None,
        random_tls_extension_order: bool = False,
    ):
        super().__init__(
            verify=verify, proxies=proxies,
            timeout=30 if timeout_ms is not None and timeout is None else timeout,
            impersonate=impersonate, headers=headers, cookies=cookies, auth=auth,
            proxy=proxy, base_url=base_url, params=params,
            allow_redirects=allow_redirects, max_redirects=max_redirects,
            default_headers=default_headers, timeout_ms=timeout_ms,
            default_domain=default_domain,
            random_tls_extension_order=random_tls_extension_order,
        )


class AsyncSession(AsyncClient):
    """Public asynchronous session backed by AsyncClient."""
