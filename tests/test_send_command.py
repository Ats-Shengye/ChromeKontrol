"""
Location   : ChromeKontrol/tests/test_send_command.py
Purpose    : Unit tests for ChromeKontrolServer.send_command() / _send_command_locked().
Why        : This is the WebSocket round-trip at the heart of every command
             dispatch. Three properties matter most: (1) _command_lock truly
             serializes overlapping calls rather than letting them interleave
             on the wire, (2) a send-time disconnect is reported distinctly
             from a response-time timeout, and (3) the timeout path returns
             promptly and with a caller-actionable message. (The response
             mix-up hazard _command_lock exists to prevent, and its
             remaining gap, is covered separately in
             test_known_issues_xfail.py per ISSUES.md P1-1.)
Related    : server.py
"""

from __future__ import annotations

import asyncio
import json

import pytest

import server
from tests.conftest import FakeWebSocket, as_ws_protocol


@pytest.fixture
def kontrol() -> server.ChromeKontrolServer:
    return server.ChromeKontrolServer()


async def test_send_command_returns_extension_response_on_success(
    kontrol: server.ChromeKontrolServer,
) -> None:
    """A response only reaches send_command() via _handle_message() (as handle_connection's
    real message loop would deliver it), not by queuing a raw message on the socket — the
    fake socket's queue is consumed by that loop, which this unit test intentionally does
    not run (it is covered separately in test_handle_connection.py). So the response is
    injected directly here, once send_command has had a chance to actually send its request.
    """
    ws = FakeWebSocket()
    kontrol._clients["chrome"] = server.ClientInfo(browser="chrome", websocket=as_ws_protocol(ws))

    task = asyncio.create_task(kontrol.send_command({"cmd": "get_dom"}, browser="chrome", timeout=1.0))
    for _ in range(5):
        await asyncio.sleep(0)
    assert ws.sent_messages == [json.dumps({"cmd": "get_dom"})]

    await kontrol._handle_message(json.dumps({"result": "ok", "data": "<html></html>"}), "chrome")
    result = await task

    assert result == {"result": "ok", "data": "<html></html>"}


async def test_send_command_reports_disconnect_before_send(
    kontrol: server.ChromeKontrolServer,
) -> None:
    """If the socket drops between resolution and send(), a distinct message is returned."""
    ws = FakeWebSocket()
    ws.send_should_fail = True
    kontrol._clients["chrome"] = server.ClientInfo(browser="chrome", websocket=as_ws_protocol(ws))

    result = await kontrol.send_command({"cmd": "get_dom"}, browser="chrome", timeout=1.0)

    assert result == {
        "result": "error",
        "message": "Extension disconnected before command was sent.",
    }


async def test_send_command_times_out_when_no_response_arrives(
    kontrol: server.ChromeKontrolServer,
) -> None:
    ws = FakeWebSocket()
    kontrol._clients["chrome"] = server.ClientInfo(browser="chrome", websocket=as_ws_protocol(ws))

    result = await kontrol.send_command({"cmd": "get_dom"}, browser="chrome", timeout=0.05)

    assert result == {
        "result": "error",
        "message": "Timed out waiting for extension response (0.05s).",
    }


async def test_send_command_reports_disconnect_when_all_clients_vanish_while_waiting(
    kontrol: server.ChromeKontrolServer,
) -> None:
    """The "all clients gone" branch fires distinctly from a plain timeout when every
    connected client (there was only one here) disconnects mid-wait, simulating exactly
    what handle_connection's cleanup does: remove the entry, then wake any waiter.
    """
    ws = FakeWebSocket()
    kontrol._clients["chrome"] = server.ClientInfo(browser="chrome", websocket=as_ws_protocol(ws))

    task = asyncio.create_task(kontrol.send_command({"cmd": "get_dom"}, browser="chrome", timeout=1.0))
    for _ in range(5):
        await asyncio.sleep(0)

    del kontrol._clients["chrome"]
    kontrol._response_event.set()

    result = await task
    assert result == {
        "result": "error",
        "message": "Extension disconnected while waiting for response.",
    }


async def test_send_command_propagates_resolution_error_without_sending(
    kontrol: server.ChromeKontrolServer,
) -> None:
    """When _resolve_client can't find a target, send_command returns its error dict as-is."""
    result = await kontrol.send_command({"cmd": "get_dom"}, browser="chrome", timeout=0.05)
    assert isinstance(result, dict)
    assert result["result"] == "error"
    assert "chrome" in result["message"].lower()


async def test_send_command_serializes_concurrent_calls_via_lock(
    kontrol: server.ChromeKontrolServer,
) -> None:
    """_command_lock must prevent a second call from sending before the first one resolves.

    Without the lock, two overlapping HTTP requests could interleave their
    WebSocket round-trips and receive each other's responses. This test
    drives two overlapping send_command() calls with cooperative
    asyncio.sleep(0) yields (rather than real-time sleeps) to deterministically
    observe that call B's command is not written to the socket until call A
    has fully completed and released the lock.
    """
    ws = FakeWebSocket()
    kontrol._clients["chrome"] = server.ClientInfo(browser="chrome", websocket=as_ws_protocol(ws))

    task_a = asyncio.create_task(kontrol.send_command({"cmd": "get_dom"}, browser="chrome", timeout=1.0))
    # Let task A run until it blocks on the response event (past its send()).
    for _ in range(5):
        await asyncio.sleep(0)
    assert ws.sent_messages == [json.dumps({"cmd": "get_dom"})]

    task_b = asyncio.create_task(kontrol.send_command({"cmd": "list_tabs"}, browser="chrome", timeout=1.0))
    # Task B should now be blocked acquiring _command_lock, held by task A.
    for _ in range(5):
        await asyncio.sleep(0)
    assert ws.sent_messages == [json.dumps({"cmd": "get_dom"})], "task B must not send before task A completes"

    # Resolve A (via _handle_message, exactly as handle_connection's real
    # message loop would deliver a response); this releases the lock so B
    # can proceed. See test_send_command_returns_extension_response_on_success
    # for why _handle_message is called directly rather than queuing on ws.
    await kontrol._handle_message(json.dumps({"result": "ok", "data": "A"}), "chrome")
    result_a = await task_a
    assert result_a == {"result": "ok", "data": "A"}

    for _ in range(5):
        await asyncio.sleep(0)
    assert ws.sent_messages == [
        json.dumps({"cmd": "get_dom"}),
        json.dumps({"cmd": "list_tabs"}),
    ], "task B must send only after task A released the lock"

    await kontrol._handle_message(json.dumps({"result": "ok", "data": "B"}), "chrome")
    result_b = await task_b
    assert result_b == {"result": "ok", "data": "B"}
