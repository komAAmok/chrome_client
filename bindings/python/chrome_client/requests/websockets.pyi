"""``WebSocket`` and ``AsyncWebSocket``.

At runtime this module *is* ``chrome_client._python_impl.websockets``: the package aliases it
into ``sys.modules`` on import. The names below are re-exported
explicitly, so a type checker follows the same path instead of
seeing ``Any`` (PEP 561).
"""

from .._python_impl.websockets import (
    OK as OK,
    MAX_QUEUED_EVENTS as MAX_QUEUED_EVENTS,
    MAX_QUEUED_BYTES as MAX_QUEUED_BYTES,
    FrameKind as FrameKind,
    Message as Message,
    OnMessage as OnMessage,
    OnError as OnError,
    OnOpen as OnOpen,
    OnClose as OnClose,
    WsCloseCode as WsCloseCode,
    CurlWsFrame as CurlWsFrame,
    WebSocket as WebSocket,
    AsyncWebSocket as AsyncWebSocket,
)
