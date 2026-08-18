"""
Location   : ChromeKontrol/tests/test_handle_connection.py
Purpose    : Unit tests for ChromeKontrolServer.handle_connection().
Why        : This is the full per-connection lifecycle: Origin check,
             identify handshake, duplicate/stale-replacement handling, the
             message-processing loop, and cleanup on disconnect. It is the
             one function that ties _is_allowed_origin, _receive_identify,
             and _handle_message together, so its own branching (not just
             the delegated functions) needs direct coverage — especially the
             cleanup guard that must not delete a *different* connection that
             has since replaced this one in self._clients, and (ISSUES.md
             P0-1, Phase 2a) the profileId-aware keying that lets two
             profiles of the same browser coexist in self._clients.
Related    : server.py, ISSUES.md P0-1
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


async def test_handle_connection_rejects_forbidden_origin(
    kontrol: server.ChromeKontrolServer,
) -> None:
    ws = FakeWebSocket(headers={"Origin": "http://evil.example.com"})
    await kontrol.handle_connection(as_ws_protocol(ws))
    assert ws.close_calls == [(1008, "Forbidden origin")]
    assert kontrol._clients == {}


async def test_handle_connection_never_attempts_identify_after_origin_rejection(
    kontrol: server.ChromeKontrolServer,
) -> None:
    """The queue is left untouched; a rejected origin must not consume the identify message."""
    ws = FakeWebSocket(headers={"Origin": "null"})
    ws.feed(json.dumps({"browser": "chrome"}))
    await kontrol.handle_connection(as_ws_protocol(ws))
    assert ws.close_calls == [(1008, "Forbidden origin")]
    assert kontrol._clients == {}


async def test_handle_connection_returns_without_registering_on_identify_failure(
    kontrol: server.ChromeKontrolServer,
) -> None:
    ws = FakeWebSocket()
    ws.feed("not json")
    await kontrol.handle_connection(as_ws_protocol(ws))
    # _receive_identify already closed the socket with its own reason; verified
    # independently in test_receive_identify.py. Here we only assert the
    # connection never gets registered.
    assert kontrol._clients == {}


async def test_handle_connection_rejects_duplicate_when_existing_connection_alive(
    kontrol: server.ChromeKontrolServer,
) -> None:
    old_ws = FakeWebSocket()  # open (neither closed nor closing): alive
    kontrol._clients["chrome"] = server.ClientInfo(browser="chrome", websocket=as_ws_protocol(old_ws))

    new_ws = FakeWebSocket()
    new_ws.feed(json.dumps({"browser": "chrome"}))
    await kontrol.handle_connection(as_ws_protocol(new_ws))

    assert new_ws.close_calls == [(1008, "Duplicate connection rejected")]
    assert kontrol._clients["chrome"].websocket is as_ws_protocol(old_ws)


async def test_handle_connection_replaces_stale_connection(
    kontrol: server.ChromeKontrolServer,
) -> None:
    """A prior connection reporting closed=True (stale) is replaced, not rejected."""
    old_ws = FakeWebSocket(closed=True)
    kontrol._clients["chrome"] = server.ClientInfo(browser="chrome", websocket=as_ws_protocol(old_ws))

    new_ws = FakeWebSocket()
    new_ws.feed(json.dumps({"browser": "chrome"}))
    new_ws.feed(websockets.exceptions.ConnectionClosedOK(None, None))  # end the message loop
    await kontrol.handle_connection(as_ws_protocol(new_ws))

    assert new_ws.close_calls == []  # never rejected as a duplicate


async def test_handle_connection_processes_messages_and_cleans_up_on_normal_disconnect(
    kontrol: server.ChromeKontrolServer,
) -> None:
    ws = FakeWebSocket()
    ws.feed(json.dumps({"browser": "chrome"}))
    ws.feed(json.dumps({"result": "ok", "data": "tab-list"}))
    ws.feed(websockets.exceptions.ConnectionClosedOK(None, None))

    await kontrol.handle_connection(as_ws_protocol(ws))

    # The command response was handed to _handle_message before cleanup ran.
    assert kontrol._pending_response == {"result": "ok", "data": "tab-list"}
    # Cleanup removed the (now-disconnected) client entry.
    assert "chrome" not in kontrol._clients
    # Cleanup unblocks any in-flight waiter regardless of disconnect reason.
    assert kontrol._response_event.is_set() is True


async def test_handle_connection_cleans_up_when_loop_ends_without_a_connection_closed_exception(
    kontrol: server.ChromeKontrolServer,
) -> None:
    """The `async for` loop can also end via a plain StopAsyncIteration (no
    ConnectionClosedOK/Error raised at all) - real websockets' own __aiter__
    implementation swallows a clean (code 1000/1001) close internally and
    ends iteration this way rather than propagating ConnectionClosedOK out to
    the caller. handle_connection's try/except/finally must still reach its
    cleanup in that case, without either except clause having matched.
    """
    ws = FakeWebSocket()
    ws.feed(json.dumps({"browser": "chrome"}))
    ws.feed(StopAsyncIteration())

    await kontrol.handle_connection(as_ws_protocol(ws))

    assert "chrome" not in kontrol._clients
    assert kontrol._response_event.is_set() is True


async def test_handle_connection_cleans_up_on_abnormal_disconnect(
    kontrol: server.ChromeKontrolServer,
) -> None:
    ws = FakeWebSocket()
    ws.feed(json.dumps({"browser": "chrome"}))
    ws.feed(websockets.exceptions.ConnectionClosedError(None, None))

    await kontrol.handle_connection(as_ws_protocol(ws))

    assert "chrome" not in kontrol._clients
    assert kontrol._response_event.is_set() is True


async def test_handle_connection_cleanup_does_not_delete_a_newer_replacement(
    kontrol: server.ChromeKontrolServer,
) -> None:
    """If a different connection has since taken over the same client slot, an older
    connection's own cleanup must not evict it (server.py's `is client_info` guard).

    This drives handle_connection(ws1) as a background task, lets it register
    and block inside the message loop, then simulates a newer connection
    (ws2) taking over the "chrome" slot directly (bypassing the full
    handshake dance, since that path is already covered by the
    stale-replacement test above). Only then does ws1's loop end; its
    cleanup must see that self._clients["chrome"] is no longer itself and
    leave ws2's registration untouched.
    """
    ws1 = FakeWebSocket()
    ws1.feed(json.dumps({"browser": "chrome"}))

    task1 = asyncio.create_task(kontrol.handle_connection(as_ws_protocol(ws1)))
    # Let task1 run past identify/registration until it blocks awaiting the
    # next message inside the `async for` loop.
    for _ in range(10):
        await asyncio.sleep(0)
    assert kontrol._clients["chrome"].websocket is as_ws_protocol(ws1)

    ws2 = FakeWebSocket()
    kontrol._clients["chrome"] = server.ClientInfo(
        browser="chrome", websocket=as_ws_protocol(ws2)
    )  # simulate a newer connection taking over

    ws1.feed(websockets.exceptions.ConnectionClosedOK(None, None))
    await task1

    assert kontrol._clients["chrome"].websocket is as_ws_protocol(ws2)


# ---------------------------------------------------------------------------
# ISSUES.md P0-1 (Phase 2a): profileId-aware keying and multi-profile coexistence.
# ---------------------------------------------------------------------------


async def test_handle_connection_keys_by_browser_alone_when_profile_id_absent(
    kontrol: server.ChromeKontrolServer,
) -> None:
    """Legacy identify ({"browser": "chrome"}, no profileId) must key by browser alone,
    preserving Phase 2a's backward compatibility with the not-yet-updated extension.
    """
    ws = FakeWebSocket()
    ws.feed(json.dumps({"browser": "chrome"}))

    task = asyncio.create_task(kontrol.handle_connection(as_ws_protocol(ws)))
    for _ in range(10):
        await asyncio.sleep(0)

    assert set(kontrol._clients.keys()) == {"chrome"}
    assert kontrol._clients["chrome"].profile_id is None

    ws.feed(websockets.exceptions.ConnectionClosedOK(None, None))
    await task


async def test_handle_connection_keys_by_browser_and_profile_id_when_present(
    kontrol: server.ChromeKontrolServer,
) -> None:
    ws = FakeWebSocket()
    ws.feed(json.dumps({"browser": "chrome", "profileId": "a3f2c1d8"}))

    task = asyncio.create_task(kontrol.handle_connection(as_ws_protocol(ws)))
    for _ in range(10):
        await asyncio.sleep(0)

    assert set(kontrol._clients.keys()) == {"chrome:a3f2c1d8"}

    ws.feed(websockets.exceptions.ConnectionClosedOK(None, None))
    await task


async def test_handle_connection_rejects_duplicate_with_same_browser_and_profile_id(
    kontrol: server.ChromeKontrolServer,
) -> None:
    """Same browser + same profileId, both alive: the second connection is the duplicate case."""
    old_ws = FakeWebSocket()
    kontrol._clients["chrome:a3f2c1d8"] = server.ClientInfo(
        browser="chrome", websocket=as_ws_protocol(old_ws), profile_id="a3f2c1d8"
    )

    new_ws = FakeWebSocket()
    new_ws.feed(json.dumps({"browser": "chrome", "profileId": "a3f2c1d8"}))
    await kontrol.handle_connection(as_ws_protocol(new_ws))

    assert new_ws.close_calls == [(1008, "Duplicate connection rejected")]
    assert kontrol._clients["chrome:a3f2c1d8"].websocket is as_ws_protocol(old_ws)


async def test_handle_connection_allows_two_different_profiles_of_same_browser_to_coexist(
    kontrol: server.ChromeKontrolServer,
) -> None:
    """ISSUES.md P0-1's core fix: two Chrome profiles identifying with different profileId
    values must both register, rather than the second being rejected as a false duplicate
    (the exact production failure mode: `Default` always wins, `Profile 1` never connects).
    """
    ws1 = FakeWebSocket()
    ws1.feed(json.dumps({"browser": "chrome", "profileId": "profile-one"}))
    task1 = asyncio.create_task(kontrol.handle_connection(as_ws_protocol(ws1)))
    for _ in range(10):
        await asyncio.sleep(0)

    ws2 = FakeWebSocket()
    ws2.feed(json.dumps({"browser": "chrome", "profileId": "profile-two"}))
    task2 = asyncio.create_task(kontrol.handle_connection(as_ws_protocol(ws2)))
    for _ in range(10):
        await asyncio.sleep(0)

    assert ws1.close_calls == []
    assert ws2.close_calls == []
    assert set(kontrol._clients.keys()) == {"chrome:profile-one", "chrome:profile-two"}

    ws1.feed(websockets.exceptions.ConnectionClosedOK(None, None))
    ws2.feed(websockets.exceptions.ConnectionClosedOK(None, None))
    await task1
    await task2


# ---------------------------------------------------------------------------
# ISSUES.md P1-5 (Phase F7): focus_ts is transcribed from ClientInfo into
# self._focus_ts on registration, and cleaned up on disconnect using the same
# "is this still my own registration" guard as self._clients.
# ---------------------------------------------------------------------------


async def test_handle_connection_transcribes_focus_ts_from_identify(
    kontrol: server.ChromeKontrolServer,
) -> None:
    """An identify carrying "focusTs" must land in _focus_ts under the same key used for
    self._clients, so that _resolve_client's auto-selection can find it immediately —
    without waiting for a separate {"type": "focus", ...} notification.
    """
    ws = FakeWebSocket()
    ws.feed(json.dumps({"browser": "chrome", "profileId": "a", "focusTs": 42}))

    task = asyncio.create_task(kontrol.handle_connection(as_ws_protocol(ws)))
    for _ in range(10):
        await asyncio.sleep(0)

    assert kontrol._focus_ts == {"chrome:a": 42}

    ws.feed(websockets.exceptions.ConnectionClosedOK(None, None))
    await task


async def test_handle_connection_does_not_create_focus_ts_entry_when_identify_omits_it(
    kontrol: server.ChromeKontrolServer,
) -> None:
    """A client that never reports focusTs must simply be absent from _focus_ts (not
    present with some placeholder value) — _resolve_client's auto-selection treats key
    absence as "never reported focus," which is the correct interpretation here.
    """
    ws = FakeWebSocket()
    ws.feed(json.dumps({"browser": "chrome"}))

    task = asyncio.create_task(kontrol.handle_connection(as_ws_protocol(ws)))
    for _ in range(10):
        await asyncio.sleep(0)

    assert kontrol._focus_ts == {}

    ws.feed(websockets.exceptions.ConnectionClosedOK(None, None))
    await task


async def test_handle_connection_cleans_up_focus_ts_on_disconnect(
    kontrol: server.ChromeKontrolServer,
) -> None:
    ws = FakeWebSocket()
    ws.feed(json.dumps({"browser": "chrome", "focusTs": 100}))
    ws.feed(websockets.exceptions.ConnectionClosedOK(None, None))

    await kontrol.handle_connection(as_ws_protocol(ws))

    assert "chrome" not in kontrol._clients
    assert "chrome" not in kontrol._focus_ts


async def test_handle_connection_focus_ts_cleanup_does_not_delete_a_newer_replacements_entry(
    kontrol: server.ChromeKontrolServer,
) -> None:
    """Mirrors test_handle_connection_cleanup_does_not_delete_a_newer_replacement for
    self._clients: if a newer connection has already taken over the same key (and recorded
    its own focus_ts) by the time an older connection's handler reaches cleanup, the older
    handler's finally block must not clobber the newer connection's _focus_ts entry either.
    """
    ws1 = FakeWebSocket()
    ws1.feed(json.dumps({"browser": "chrome", "focusTs": 100}))

    task1 = asyncio.create_task(kontrol.handle_connection(as_ws_protocol(ws1)))
    for _ in range(10):
        await asyncio.sleep(0)
    assert kontrol._focus_ts == {"chrome": 100}

    # Simulate a newer connection taking over the "chrome" slot directly, the same way
    # test_handle_connection_cleanup_does_not_delete_a_newer_replacement does.
    ws2 = FakeWebSocket()
    kontrol._clients["chrome"] = server.ClientInfo(browser="chrome", websocket=as_ws_protocol(ws2), focus_ts=200)
    kontrol._focus_ts["chrome"] = 200

    ws1.feed(websockets.exceptions.ConnectionClosedOK(None, None))
    await task1

    # ws1's cleanup must see self._clients["chrome"] is no longer its own ClientInfo and
    # leave both self._clients and self._focus_ts (ws2's registration) untouched.
    assert kontrol._clients["chrome"].websocket is as_ws_protocol(ws2)
    assert kontrol._focus_ts == {"chrome": 200}
