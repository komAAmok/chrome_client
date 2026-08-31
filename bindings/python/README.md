# Python binding

This package is imported as `chrome_client`; `chrome_client._python_impl` is a
private compatibility implementation and is not a public API. The package is a thin
PyO3 adapter over the safe `minicronet` crate. It must
not implement TLS, HTTP, QUIC, WebSocket, or proxy behavior.

The public facade is `chrome_client.Client`/`Session` and
`chrome_client.AsyncClient`/`AsyncSession`, with requests-shaped `get`, `post`,
`json`, `timeout`, `verify`, `proxy`, `proxies`, and `impersonate` parameters.
`proxies` accepts a Requests-style mapping such as
`{"http": "http://...", "https": "http://..."}`. Async
requests are driven by Core callbacks and `asyncio`'s
`loop.call_soon_threadsafe`; no request thread pool is used. Network callback
threads only copy native events and schedule the loop, so user Python code is
never run while holding the callback thread.

The Rust request queue has a 1 MiB per-request body ceiling. Once full, Core
body delivery waits until the asyncio consumer drains chunks, preventing an
unbounded native queue during large responses.

The native API is intentionally small: `PyEngine.request()` creates a request and
`Request.send()` waits for the Core response and returns `Response`. The Core
binary is selected by `MINICRONET_CORE_DIR`/the target manifest exactly as for
Rust applications.

`Request.send()` releases the Python GIL while waiting for Core I/O, so other
Python threads can continue running. The current native extension is built
with PyO3 0.28.3 (`abi3-py37`) and therefore supports Python 3.7–3.13. Python 3.6 requires a
separate extension built with a PyO3 release that still supports 3.6; it is
not silently claimed by this binary.

```python
import chrome_client

response = chrome_client.get("https://example.com")

async with chrome_client.AsyncClient(impersonate="chrome120") as client:
    response = await client.get("https://example.com")
```
