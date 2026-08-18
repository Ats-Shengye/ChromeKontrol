"""
Location   : ChromeKontrol/tests/conftest.py
Purpose    : Shared pytest fixtures and test doubles for the server.py test suite.
Why        : server.py's public surface is exercised almost entirely through
             websockets.server.WebSocketServerProtocol instances and raw
             asyncio.StreamReader/StreamWriter pairs. Binding real sockets for
             every test would be slow and would risk colliding with the
             already-running production server (pid tracked separately,
             ports 9765/9766). FakeWebSocket below implements only the subset
             of the WebSocketServerProtocol interface server.py actually
             calls (see server.py module docstring's "テスト用の fake/stub"
             note in the delegation brief), letting tests control connection
             lifecycle deterministically without any real I/O.
Related    : server.py
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest
import websockets.exceptions
import websockets.server


class FakeWebSocket:
    """Minimal stand-in for websockets.server.WebSocketServerProtocol.

    Implements exactly the attributes/methods server.py uses:
      - request_headers (dict-like, `.get()`)
      - recv() / send() / close() (awaitable)
      - closed (bool)
      - open (bool)
      - ping() (awaitable, returns an awaitable "pong waiter")
      - async iteration (`async for message in websocket`)

    Design Decision: recv() and the async iterator share a single internal
    asyncio.Queue, mirroring how a real WebSocket delivers all messages
    (identify included) over one ordered stream regardless of whether the
    consumer reads them via recv() or iteration. Queue items are either a
    message payload (str) to return, or an exception instance to raise —
    this lets tests simulate both normal messages and disconnects
    (ConnectionClosedOK/Error) at precise points in a coroutine's execution
    without any real waiting, by feeding items exactly when the test wants
    the fake socket to "receive" them.

    `closed` and `open` are independent flags, not one another's negation —
    this mirrors real websockets 10.x, whose protocol has three relevant
    states: OPEN (closed=False, open=True), CLOSING (closed=False,
    open=False), and CLOSED (closed=True, open=False). ISSUES.md P1-3 is
    exactly about code that conflated "not closed" with "alive", which
    mistook CLOSING for OPEN; FakeWebSocket needs to be able to represent
    CLOSING independently of CLOSED to test that distinction
    (see tests/test_known_issues_xfail.py's P1-3 test). `is_open` defaults to
    `not closed` so every existing call site that only ever passed `closed=`
    keeps its original OPEN/CLOSED meaning without change; passing
    `is_open=False` alongside `closed=False` is how a test opts into
    simulating CLOSING specifically.
    """

    def __init__(
        self,
        *,
        headers: dict[str, str] | None = None,
        closed: bool = False,
        is_open: bool | None = None,
    ) -> None:
        self.request_headers: dict[str, str] = headers if headers is not None else {}
        self.closed = closed
        self.open: bool = (not closed) if is_open is None else is_open
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        self.sent_messages: list[str] = []
        self.close_calls: list[tuple[int, str]] = []
        # Test hook: force send() to behave as if the peer already dropped
        # the connection, independent of the `closed` flag (covers the
        # "disconnected right before send" race distinctly from "already
        # marked closed").
        self.send_should_fail: bool = False

    def feed(self, item: Any) -> None:
        """Queue a message (str) or an exception instance for recv()/__anext__ to yield."""
        self._queue.put_nowait(item)

    async def recv(self) -> Any:
        item = await self._queue.get()
        if isinstance(item, BaseException):
            raise item
        return item

    async def send(self, data: str) -> None:
        if self.closed or self.send_should_fail:
            raise websockets.exceptions.ConnectionClosedError(None, None)
        self.sent_messages.append(data)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = True
        self.open = False
        self.close_calls.append((code, reason))

    async def ping(self) -> "asyncio.Future[None]":
        # run_ping_loop() (the only caller of ping()) is explicitly out of
        # Phase 0 test scope; this stub exists only so FakeWebSocket remains
        # a complete duck-type of the interface server.py depends on.
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        fut.set_result(None)
        return fut

    def __aiter__(self) -> "FakeWebSocket":
        return self

    async def __anext__(self) -> Any:
        item = await self._queue.get()
        if isinstance(item, BaseException):
            raise item
        return item


def as_ws_protocol(fake: FakeWebSocket) -> websockets.server.WebSocketServerProtocol:
    """Narrow a FakeWebSocket to the production protocol type for typed call sites.

    FakeWebSocket implements the same duck-typed interface server.py actually
    depends on (see its class docstring), but does not subclass
    websockets.server.WebSocketServerProtocol: `closed` and `open` are
    read-only properties with no setter on the real class, and its
    `__init__` requires internal connection state that has no meaning for a
    test double. Real subclassing was evaluated and rejected for exactly
    that reason.

    This cast documents the intentional substitution at each call site that
    needs it (assigning into ChromeKontrolServer._clients, or passing a fake
    socket to a function typed to accept the real protocol), rather than
    disabling type-checking for entire test files via a blanket
    `# type: ignore`.
    """
    return cast("websockets.server.WebSocketServerProtocol", fake)


@pytest.fixture
def fake_ws() -> FakeWebSocket:
    """A fresh FakeWebSocket with no headers, open, and an empty message queue."""
    return FakeWebSocket()
