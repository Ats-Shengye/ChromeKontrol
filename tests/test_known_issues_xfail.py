"""
Location   : ChromeKontrol/tests/test_known_issues_xfail.py
Purpose    : Encodes the *desired* (not current) behavior for known defects
             documented in ISSUES.md. P1-1 and P1-4 remain open and are
             marked xfail(strict=True) so their fix's completion is detected
             automatically the moment it lands.
Why        : Phase 0's job was to freeze server.py's current behavior as a
             regression net for later rewrites — except for defects
             ISSUES.md already flags as bugs. Writing the *current* (buggy)
             behavior into the suite would make fixing P1-1/P1-3/P1-4 a test
             failure; writing the *desired* behavior as a normal test would
             just fail every run until the fix lands, hiding real
             regressions in the noise. xfail(strict=True) resolves both
             problems: the test documents and continuously checks for the
             correct behavior, fails (as expected) today, and turns into an
             XPASS - which pytest reports as a failure under strict mode -
             the moment the underlying fix makes the assertion true. That
             XPASS is the trigger to remove the xfail marker.

             P1-3 (the CLOSING-state duplicate-rejection bug) was fixed in
             Phase 2a (ISSUES.md P0-1): handle_connection now checks
             `old_client.websocket.open` instead of `.closed`, so its test
             below is no longer marked xfail — it runs as an ordinary
             passing test, updated only to construct ClientInfo (Phase 2a's
             new _clients value type) instead of a bare WebSocket.
Related    : server.py, ISSUES.md (P1-1, P1-4; P1-3 fixed in Phase 2a / P0-1)
"""

from __future__ import annotations

import asyncio
import json

import pytest
import websockets.exceptions

import server
from tests.conftest import FakeWebSocket, as_ws_protocol


@pytest.fixture
def kontrol() -> server.ChromeKontrolServer:
    return server.ChromeKontrolServer()


@pytest.mark.xfail(strict=True, reason="ISSUES.md P1-1")
async def test_stale_response_must_not_be_delivered_to_a_later_command(
    kontrol: server.ChromeKontrolServer,
) -> None:
    """Desired behavior: a command must never receive a *different* command's response.

    Reproduces the exact race ISSUES.md P1-1 documents:
      1. cmd1 is sent and times out (no response ever arrives for it).
      2. cmd2 starts, clears the shared response state, and sends its own command.
      3. cmd1's response arrives late, after cmd2 has already sent its request.

    Current (buggy) behavior: because there is no per-request correlation ID,
    step 3's stale message is indistinguishable from cmd2's real response, so
    cmd2 incorrectly receives cmd1's data. This assertion fails today for
    exactly that reason (result equals the stale payload) and is expected to
    start passing once a reqId-based correlation mechanism (ISSUES.md's
    documented fix direction) lets cmd2 either ignore the stale reply and
    keep waiting, or match responses to the request that produced them.
    """
    ws = FakeWebSocket()
    kontrol._clients["chrome"] = server.ClientInfo(browser="chrome", websocket=as_ws_protocol(ws))

    # Step 1: cmd1 times out (nothing is ever fed for it).
    result1 = await kontrol.send_command({"cmd": "get_dom"}, browser="chrome", timeout=0.02)
    assert result1["result"] == "error"

    # Step 2: cmd2 starts concurrently; let it reach "awaiting response".
    task2 = asyncio.create_task(kontrol.send_command({"cmd": "list_tabs"}, browser="chrome", timeout=0.3))
    for _ in range(5):
        await asyncio.sleep(0)

    # Step 3: cmd1's late response finally arrives, after cmd2 has already sent its request.
    await kontrol._handle_message(json.dumps({"result": "ok", "data": "STALE_CMD1_RESPONSE"}), "chrome")

    result2 = await task2
    assert result2.get("data") != "STALE_CMD1_RESPONSE"


async def test_duplicate_rejection_must_not_treat_closing_connection_as_alive(
    kontrol: server.ChromeKontrolServer,
) -> None:
    """Fixed (ISSUES.md P1-3, Phase 2a): a connection mid-close (CLOSING) must not block
    a legitimate reconnect.

    websockets 10.x's `.closed` property is True only once the connection
    reaches the fully CLOSED state; during the CLOSING handshake it still
    reads False, while `.open` is False in both CLOSING and CLOSED (True only
    in OPEN). FakeWebSocket(closed=False, is_open=False) reproduces exactly
    that "still closing, not yet closed" state without needing the real
    websockets.legacy.protocol.State enum.

    handle_connection's duplicate check now inspects
    `old_client.websocket.open` rather than `not old_client.closed`, so a
    CLOSING peer is no longer mistaken for "alive" and the new connection is
    accepted rather than rejected.
    """
    old_ws = FakeWebSocket(closed=False, is_open=False)  # simulating CLOSING, not yet CLOSED
    kontrol._clients["chrome"] = server.ClientInfo(browser="chrome", websocket=as_ws_protocol(old_ws))

    new_ws = FakeWebSocket()
    new_ws.feed(json.dumps({"browser": "chrome"}))
    new_ws.feed(websockets.exceptions.ConnectionClosedOK(None, None))  # end the loop promptly

    await kontrol.handle_connection(as_ws_protocol(new_ws))

    assert new_ws.close_calls == []


@pytest.mark.xfail(strict=True, reason="ISSUES.md P1-4")
async def test_target_client_disconnect_must_be_reported_distinctly(
    kontrol: server.ChromeKontrolServer,
) -> None:
    """Desired behavior: losing the specific target client must be reported as a disconnect,
    not conflated with the generic "no response body arrived" case.

    Setup: two clients are connected ("chrome" and "edge"); a command
    targets "chrome". While it is in flight, only "chrome" disconnects
    (mirroring handle_connection's real cleanup: remove the browser's own
    entry from self._clients, then unconditionally set
    self._response_event, exactly as its `finally` block does for every
    disconnect regardless of which other clients remain).

    Current (buggy) behavior: _send_command_locked's disconnect check
    (`if not self._clients and self._pending_response is None:`) only fires
    when *every* client is gone. Since "edge" is still connected, this
    specific-target disconnect instead falls through to the generic
    'Empty response from extension.' message - which does not indicate a
    disconnect occurred at all, let alone which browser. This assertion
    fails today for exactly that reason, and is expected to start passing
    once the fix (ISSUES.md's documented direction: track the specific
    target client and detect its removal from self._clients, independent of
    whether other clients remain) reports a clear, target-specific
    disconnect message instead.
    """
    kontrol._clients["chrome"] = server.ClientInfo(browser="chrome", websocket=as_ws_protocol(FakeWebSocket()))
    kontrol._clients["edge"] = server.ClientInfo(browser="edge", websocket=as_ws_protocol(FakeWebSocket()))

    task = asyncio.create_task(kontrol.send_command({"cmd": "get_dom"}, browser="chrome", timeout=0.3))
    for _ in range(5):
        await asyncio.sleep(0)  # let the task reach "awaiting response"

    # Simulate exactly what handle_connection's finally block does when
    # "chrome" (and only "chrome") disconnects, leaving "edge" connected.
    del kontrol._clients["chrome"]
    kontrol._response_event.set()

    result = await task
    assert result["result"] == "error"
    assert "disconnect" in result["message"].lower()
    assert "chrome" in result["message"].lower()
