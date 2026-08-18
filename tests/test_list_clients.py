"""
Location   : ChromeKontrol/tests/test_list_clients.py
Purpose    : Unit tests for the "list_clients" command (ISSUES.md P0-1,
             Phase 2a): ChromeKontrolServer._list_clients_response() and the
             send_command() short-circuit that serves it without ever
             touching a connected extension.
Why        : list_clients is architecturally different from every other
             command: it must never reach the extension over WebSocket, it
             must reflect every field ClientInfo carries (with null for
             anything a legacy pre-Phase-3 client never sent), and its
             ordering/displayName resolution must be deterministic so
             callers can present a stable list. It must also not be blocked
             by _command_lock, since it never performs the WebSocket
             round-trip that lock exists to serialize.
Related    : server.py, ISSUES.md P0-1
"""

from __future__ import annotations

import asyncio
import json

import server
from tests.conftest import FakeWebSocket, as_ws_protocol


def _client(
    browser: str,
    *,
    profile_id: str | None = None,
    email: str | None = None,
    label: str | None = None,
) -> server.ClientInfo:
    return server.ClientInfo(
        browser=browser,
        websocket=as_ws_protocol(FakeWebSocket()),
        profile_id=profile_id,
        email=email,
        label=label,
    )


async def test_list_clients_never_sends_anything_to_the_extension() -> None:
    """list_clients is answered entirely from server-held state; no WebSocket traffic occurs."""
    kontrol = server.ChromeKontrolServer()
    ws = FakeWebSocket()
    kontrol._clients["chrome"] = server.ClientInfo(browser="chrome", websocket=as_ws_protocol(ws))

    result = await kontrol.send_command({"cmd": "list_clients"})

    assert ws.sent_messages == []
    assert result["result"] == "ok"


async def test_list_clients_returns_empty_list_when_no_clients_connected() -> None:
    kontrol = server.ChromeKontrolServer()
    result = await kontrol.send_command({"cmd": "list_clients"})
    assert result == {"result": "ok", "data": []}


async def test_list_clients_reports_null_fields_for_legacy_client_without_profile_info() -> None:
    """A client that identified with only {"browser": "chrome"} (pre-Phase-3 extension) must
    still be listed, with profileId/email/label all null and displayName falling back to browser.
    """
    kontrol = server.ChromeKontrolServer()
    kontrol._clients["chrome"] = _client("chrome")

    result = await kontrol.send_command({"cmd": "list_clients"})

    assert result == {
        "result": "ok",
        "data": [
            {
                "key": "chrome",
                "browser": "chrome",
                "profileId": None,
                "email": None,
                "label": None,
                "displayName": "chrome",
                "aliases": [],
            }
        ],
    }


async def test_list_clients_reports_full_profile_info() -> None:
    kontrol = server.ChromeKontrolServer()
    kontrol._clients["chrome:a3f2c1d8"] = _client(
        "chrome", profile_id="a3f2c1d8", email="user@example.com", label="メイン"
    )

    result = await kontrol.send_command({"cmd": "list_clients"})

    assert result["data"] == [
        {
            "key": "chrome:a3f2c1d8",
            "browser": "chrome",
            "profileId": "a3f2c1d8",
            "email": "user@example.com",
            "label": "メイン",
            "displayName": "メイン",
            "aliases": [],
        }
    ]


async def test_list_clients_display_name_falls_back_to_email_without_label() -> None:
    kontrol = server.ChromeKontrolServer()
    kontrol._clients["chrome:a3f2c1d8"] = _client("chrome", profile_id="a3f2c1d8", email="user@example.com")
    result = await kontrol.send_command({"cmd": "list_clients"})
    assert result["data"][0]["displayName"] == "user@example.com"


async def test_list_clients_display_name_falls_back_to_profile_id_prefix_without_label_or_email() -> None:
    kontrol = server.ChromeKontrolServer()
    kontrol._clients["chrome:a3f2c1d8e9b1"] = _client("chrome", profile_id="a3f2c1d8e9b1")
    result = await kontrol.send_command({"cmd": "list_clients"})
    assert result["data"][0]["displayName"] == "a3f2c1d8"  # first 8 chars of profileId


async def test_list_clients_display_name_falls_back_to_browser_with_no_other_info() -> None:
    kontrol = server.ChromeKontrolServer()
    kontrol._clients["edge"] = _client("edge")
    result = await kontrol.send_command({"cmd": "list_clients"})
    assert result["data"][0]["displayName"] == "edge"


async def test_list_clients_orders_by_key_ascending() -> None:
    kontrol = server.ChromeKontrolServer()
    kontrol._clients["edge"] = _client("edge")
    kontrol._clients["chrome:zzz"] = _client("chrome", profile_id="zzz")
    kontrol._clients["chrome:aaa"] = _client("chrome", profile_id="aaa")

    result = await kontrol.send_command({"cmd": "list_clients"})

    assert [entry["key"] for entry in result["data"]] == ["chrome:aaa", "chrome:zzz", "edge"]


async def test_list_clients_does_not_deduplicate_identical_display_names() -> None:
    """Two profiles that both resolve to the same displayName (e.g. same label) must both
    appear; only `key` disambiguates them.
    """
    kontrol = server.ChromeKontrolServer()
    kontrol._clients["chrome:aaa"] = _client("chrome", profile_id="aaa", label="サブ")
    kontrol._clients["chrome:bbb"] = _client("chrome", profile_id="bbb", label="サブ")

    result = await kontrol.send_command({"cmd": "list_clients"})

    assert len(result["data"]) == 2
    assert {entry["key"] for entry in result["data"]} == {"chrome:aaa", "chrome:bbb"}
    assert all(entry["displayName"] == "サブ" for entry in result["data"])


# ---------------------------------------------------------------------------
# ISSUES.md P0-1 (Phase 2b): "aliases" field listing config-file aliases that
# currently resolve uniquely to each client.
# ---------------------------------------------------------------------------


async def test_list_clients_aliases_field_empty_when_no_aliases_configured() -> None:
    kontrol = server.ChromeKontrolServer()
    kontrol._clients["chrome"] = _client("chrome")
    result = await kontrol.send_command({"cmd": "list_clients"})
    assert result["data"][0]["aliases"] == []


async def test_list_clients_aliases_field_lists_single_matching_alias() -> None:
    kontrol = server.ChromeKontrolServer(aliases={"メイン": "chrome:work@example.com"})
    kontrol._clients["chrome:a"] = _client("chrome", profile_id="a", email="work@example.com")
    result = await kontrol.send_command({"cmd": "list_clients"})
    assert result["data"][0]["aliases"] == ["メイン"]


async def test_list_clients_aliases_field_lists_multiple_aliases_sorted() -> None:
    """Spec section 6: every alias resolving to the client is listed, sorted (sorted())."""
    kontrol = server.ChromeKontrolServer(
        aliases={
            "サブ": "chrome:work@example.com",
            "仕事": "chrome:work@example.com",
        }
    )
    kontrol._clients["chrome:a"] = _client("chrome", profile_id="a", email="work@example.com")
    result = await kontrol.send_command({"cmd": "list_clients"})
    assert result["data"][0]["aliases"] == sorted(["サブ", "仕事"])


async def test_list_clients_aliases_field_omits_alias_pointing_elsewhere() -> None:
    kontrol = server.ChromeKontrolServer(aliases={"メイン": "chrome:someone-else@example.com"})
    kontrol._clients["chrome:a"] = _client("chrome", profile_id="a", email="work@example.com")
    result = await kontrol.send_command({"cmd": "list_clients"})
    assert result["data"][0]["aliases"] == []


async def test_list_clients_aliases_field_omits_ambiguously_resolving_alias() -> None:
    """An alias whose value currently matches 2+ clients is not "resolved" to any one of
    them, so it must not appear in either client's aliases list (spec section 6:
    "そのクライアントに解決されるエイリアス").
    """
    kontrol = server.ChromeKontrolServer(aliases={"Edge": "edge:*"})
    kontrol._clients["edge:a"] = _client("edge", profile_id="a")
    kontrol._clients["edge:b"] = _client("edge", profile_id="b")
    result = await kontrol.send_command({"cmd": "list_clients"})
    assert all(entry["aliases"] == [] for entry in result["data"])


async def test_list_clients_does_not_require_the_command_lock() -> None:
    """list_clients must resolve even while a real command holds _command_lock, since it
    never performs the WebSocket round-trip that lock serializes.
    """
    kontrol = server.ChromeKontrolServer()
    ws = FakeWebSocket()
    kontrol._clients["chrome"] = server.ClientInfo(browser="chrome", websocket=as_ws_protocol(ws))

    blocked_task = asyncio.create_task(kontrol.send_command({"cmd": "get_dom"}, browser="chrome", timeout=0.5))
    for _ in range(5):
        await asyncio.sleep(0)  # let it acquire _command_lock and send its request
    assert ws.sent_messages == [json.dumps({"cmd": "get_dom"})]

    result = await asyncio.wait_for(kontrol.send_command({"cmd": "list_clients"}), timeout=0.1)
    assert result["result"] == "ok"

    blocked_result = await blocked_task
    assert blocked_result["result"] == "error"  # times out on its own, unaffected by list_clients
