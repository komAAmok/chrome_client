#!/usr/bin/env python3
"""Consumer-side type-check fixture for the ``chrome_client`` stubs.

The ``.pyi`` files can be internally consistent and still fail a user: hover
showing ``Any``, completion missing a keyword, ``bytes`` where ``str`` was asked
for. This file exercises the public surface the way a caller does, and every
``reveal_type`` below is an assertion on what an IDE would show.

Run it with::

    MYPYPATH=bindings/python python -m mypy tools/typecheck-consumer.py \\
        --python-version 3.10 --ignore-missing-imports --warn-unused-ignores

Expected: ``Success: no issues found``. The two deliberate mistakes at the end
carry ``# type: ignore`` codes, and ``--warn-unused-ignores`` turns them into an
assertion -- if ``impersonate="chrome_200"`` or an unknown keyword ever stops
being an error, the ignore becomes unused and this fixture fails.

Not collected by the test suite: it needs a type checker, not an interpreter.
"""

import asyncio
from typing import Any

from chrome_client import (
    AsyncSession,
    CurlMime,
    ExtraFingerprints,
    Headers,
    HttpVersion,
    Impersonate,
    Response,
    RetryStrategy,
    Session,
    WebSocket,
    codes,
)
from chrome_client.requests import AsyncSession as RequestsAsyncSession
from chrome_client.requests import get as requests_get


def sync_use() -> None:
    with Session(impersonate="chrome_153", http_version="v2", retry=3) as session:
        response = session.get(
            "https://example.com", params={"page": 1}, impersonate="chrome152"
        )
        reveal_type(response)  # Response

        reveal_type(response.json())  # Any
        reveal_type(response.text)  # str
        reveal_type(response.headers.get_list("set-cookie"))  # list[str]
        reveal_type(response.content)  # bytes
        reveal_type(response.status_code)  # int | None
        reveal_type(session.cookies.get_dict(domain="example.com"))  # dict[str, str]

        # ``decode_unicode`` picks the yielded type through @overload.
        for chunk in response.iter_content(65536):
            reveal_type(chunk)  # bytes
        for line in response.iter_content(65536, decode_unicode=True):
            reveal_type(line)  # str

        # Body shapes: what the call accepts matters as much as what it returns.
        session.post("https://example.com", json={"a": 1})
        session.post("https://example.com", files={"f": ("a.txt", b"...", "text/plain")})
        session.put("https://example.com", data=b"x", json={"a": 1})
        session.get("https://example.com", headers=Headers({"Accept": "application/json"}))
        session.get("https://example.com", timeout=(5, 15), stream=True)
        session.get(
            "https://example.com",
            cookies={"sid": "1"},
            proxies={"https": "http://127.0.0.1:8080"},
            verify=False,
            extra_fp=ExtraFingerprints(header_order=["accept", "cookie"]),
        )

        mime = CurlMime()
        mime.addpart(name="title", data="hello")
        session.post("https://example.com", multipart=mime)

        socket = session.websocket("wss://echo.example.com", protocols=["chat"])
        reveal_type(socket)  # WebSocket
        socket.send_str("ping")
        reveal_type(socket.recv_str())  # str
        for message in socket:
            reveal_type(message)  # str | bytes

        # ``session.stream`` is both a flag and a curl_cffi-style helper.
        with session.stream("GET", "https://example.com") as streamed:
            reveal_type(streamed)  # Response

        reveal_type(ExtraFingerprints(header_order=["accept"]).as_dict())  # dict[str, Any]
        reveal_type(RetryStrategy(2, backoff="exponential"))  # RetryStrategy
        reveal_type(codes.not_found)  # Any
        reveal_type(session.proxies)  # Mapping[str, str | None]


async def async_use() -> None:
    async with AsyncSession(impersonate="chrome_153", max_clients=32) as session:
        response = await session.get("https://example.com")
        reveal_type(response)  # AsyncResponse

        reveal_type(await response.acontent())  # bytes
        reveal_type(await response.atext())  # str
        reveal_type(await response.ajson())  # Any

        async for chunk in response.aiter_content(65536):
            reveal_type(chunk)  # bytes
        async for line in response.aiter_lines(decode_unicode=True):
            reveal_type(line)  # str
        async for item in response.aiter_bytes(65536):
            reveal_type(item)  # bytes

        # The async flag resolves to the *async* context manager, not the sync one.
        async with session.stream("GET", "https://example.com") as streamed:
            reveal_type(streamed)  # AsyncResponse

        socket = await session.websocket("wss://echo.example.com")
        reveal_type(socket)  # AsyncWebSocket
        await socket.send_json({"op": "ping"})
        reveal_type(await socket.recv_json())  # Any
        async with socket:
            await socket.ping()
        await session.upkeep()
        await session.aclose()


def facade_use() -> None:
    """The ``chrome_client.requests`` namespace carries the same types."""
    reveal_type(requests_get("https://example.com", impersonate="chrome_153"))  # Response
    reveal_type(RequestsAsyncSession(impersonate="chrome_153", max_clients=4))  # AsyncSession
    asyncio.run(async_use())


def annotations() -> None:
    """The exported aliases must be usable in a user's own annotations."""
    profile: Impersonate = "chrome_153"
    version: HttpVersion = "v2"
    headers = Headers({"Accept": "application/json"})
    response: Response | None = None
    socket: WebSocket | None = None
    del profile, version, headers, response, socket


def deliberate_mistakes() -> None:
    """Two errors an IDE must flag while typing; see the module docstring."""
    with Session(impersonate="chrome_153") as session:
        session.get("https://example.com", impersonate="chrome_200")  # type: ignore[arg-type]  # outside chrome_99..chrome_153
        session.get("https://example.com", unknowable_option=True)  # type: ignore[call-arg]  # not a request option


unused: Any = None
