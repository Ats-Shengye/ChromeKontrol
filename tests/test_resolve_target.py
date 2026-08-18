"""
Location   : ChromeKontrol/tests/test_resolve_target.py
Purpose    : Unit tests for ChromeKontrolServer._resolve_by_target() / _resolve_client()'s
             "target" path, and the standalone helpers _split_resolved_target() /
             _match_by_identifier_order() (ISSUES.md P0-1, Phase 2b).
Why        : This is the alias-resolution algorithm the whole Phase 2b feature exists to
             deliver: alias indirection (exactly once), the ":" split into
             browser_part/identifier_part, wildcard vs. the 4-step identifier match order
             (label -> email -> email local-part -> profileId prefix), case-insensitivity,
             NFC normalization, and the two distinct error shapes (ambiguity vs.
             not-connected). Getting any step wrong silently reintroduces the exact
             "wrong profile picked" failure mode ISSUES.md P0-1 was raised to fix.
Related    : server.py, ISSUES.md P0-1
"""

from __future__ import annotations

import asyncio
import json
import unicodedata

import pytest

import server
from tests.conftest import FakeWebSocket, as_ws_protocol


def _register(
    kontrol: server.ChromeKontrolServer,
    key: str,
    browser: str,
    *,
    profile_id: str | None = None,
    email: str | None = None,
    label: str | None = None,
) -> server.ClientInfo:
    """Register a fake client under `key`, returning its ClientInfo for identity checks."""
    client = server.ClientInfo(
        browser=browser,
        websocket=as_ws_protocol(FakeWebSocket()),
        profile_id=profile_id,
        email=email,
        label=label,
    )
    kontrol._clients[key] = client
    return client


@pytest.fixture
def kontrol() -> server.ChromeKontrolServer:
    return server.ChromeKontrolServer()


# ---------------------------------------------------------------------------
# _split_resolved_target()
# ---------------------------------------------------------------------------


def test_split_resolved_target_splits_on_first_colon() -> None:
    assert server._split_resolved_target("chrome:work@example.com") == ("chrome", "work@example.com")


def test_split_resolved_target_splits_on_first_colon_only() -> None:
    """A profileId or identifier containing further ':' characters must not be mis-split."""
    assert server._split_resolved_target("chrome:a:b:c") == ("chrome", "a:b:c")


def test_split_resolved_target_returns_none_browser_part_without_colon() -> None:
    assert server._split_resolved_target("chrome") == (None, "chrome")


def test_split_resolved_target_wildcard() -> None:
    assert server._split_resolved_target("edge:*") == ("edge", "*")


# ---------------------------------------------------------------------------
# _match_by_identifier_order()
# ---------------------------------------------------------------------------


def test_match_by_identifier_order_matches_label_first() -> None:
    a = server.ClientInfo(
        browser="chrome", websocket=as_ws_protocol(FakeWebSocket()), label="メイン", email="x@example.com"
    )
    b = server.ClientInfo(browser="chrome", websocket=as_ws_protocol(FakeWebSocket()), email="メイン@example.com")
    assert server._match_by_identifier_order([a, b], "メイン") == [a]


def test_match_by_identifier_order_falls_back_to_email_when_no_label_matches() -> None:
    a = server.ClientInfo(browser="chrome", websocket=as_ws_protocol(FakeWebSocket()), label="サブ")
    b = server.ClientInfo(browser="chrome", websocket=as_ws_protocol(FakeWebSocket()), email="user@example.com")
    assert server._match_by_identifier_order([a, b], "user@example.com") == [b]


def test_match_by_identifier_order_falls_back_to_email_local_part() -> None:
    a = server.ClientInfo(browser="chrome", websocket=as_ws_protocol(FakeWebSocket()), email="work@example.com")
    assert server._match_by_identifier_order([a], "work") == [a]


def test_match_by_identifier_order_falls_back_to_profile_id_prefix() -> None:
    a = server.ClientInfo(browser="chrome", websocket=as_ws_protocol(FakeWebSocket()), profile_id="a3f2c1d8e9b1")
    assert server._match_by_identifier_order([a], "a3f2") == [a]


def test_match_by_identifier_order_stops_at_first_step_even_if_ambiguous() -> None:
    """Two clients sharing the same label must be returned together (ambiguity), not fall
    through to a later step where one of them might uniquely match some other field.
    """
    a = server.ClientInfo(
        browser="chrome", websocket=as_ws_protocol(FakeWebSocket()), label="サブ", email="a@example.com"
    )
    b = server.ClientInfo(
        browser="chrome", websocket=as_ws_protocol(FakeWebSocket()), label="サブ", email="b@example.com"
    )
    assert server._match_by_identifier_order([a, b], "サブ") == [a, b]


def test_match_by_identifier_order_case_insensitive() -> None:
    a = server.ClientInfo(browser="chrome", websocket=as_ws_protocol(FakeWebSocket()), email="User@Example.com")
    assert server._match_by_identifier_order([a], "user@example.com") == [a]


def test_match_by_identifier_order_returns_empty_when_nothing_matches() -> None:
    a = server.ClientInfo(browser="chrome", websocket=as_ws_protocol(FakeWebSocket()), label="メイン")
    assert server._match_by_identifier_order([a], "存在しない") == []


# ---------------------------------------------------------------------------
# _resolve_by_target(): browser_part present, wildcard ("*")
# ---------------------------------------------------------------------------


async def test_resolve_by_target_wildcard_selects_single_client(kontrol: server.ChromeKontrolServer) -> None:
    client = _register(kontrol, "edge", "edge")
    result = kontrol._resolve_by_target("edge:*")
    assert result is client


async def test_resolve_by_target_wildcard_rejects_ambiguous(kontrol: server.ChromeKontrolServer) -> None:
    _register(kontrol, "chrome:a", "chrome", profile_id="a", label="メイン")
    _register(kontrol, "chrome:b", "chrome", profile_id="b", label="サブ")
    result = kontrol._resolve_by_target("chrome:*")
    assert isinstance(result, dict)
    assert result["result"] == "error"
    assert "chrome:a (メイン)" in result["message"]
    assert "chrome:b (サブ)" in result["message"]


async def test_resolve_by_target_wildcard_reports_not_connected(kontrol: server.ChromeKontrolServer) -> None:
    result = kontrol._resolve_by_target("edge:*")
    assert isinstance(result, dict)
    assert result["result"] == "error"
    assert "edge:*" in result["message"]
    assert "not connected" in result["message"]
    assert "(none)" in result["message"]


# ---------------------------------------------------------------------------
# _resolve_by_target(): browser_part present, unknown browser name
# ---------------------------------------------------------------------------


async def test_resolve_by_target_rejects_unknown_browser_part(kontrol: server.ChromeKontrolServer) -> None:
    """ "safari" (not "firefox") is used here: firefox joined ALLOWED_BROWSERS in Phase F2
    (see test_resolve_by_target_firefox_wildcard_selects_single_client below), so a
    "firefox:..." target no longer exercises the unknown-browser rejection branch -- it
    now falls through to the ordinary "not connected" path instead.
    """
    result = kontrol._resolve_by_target("safari:someone@example.com")
    assert isinstance(result, dict)
    assert result["result"] == "error"
    assert "safari" in result["message"].lower()


# ---------------------------------------------------------------------------
# Phase F2: 'firefox' joins ALLOWED_BROWSERS. Alias/target resolution is
# already browser-name-agnostic (_match_by_identifier_order, wildcard
# handling, etc. never special-case "chrome"/"edge" literals), so firefox
# must resolve exactly like any other allowlisted browser with no code
# changes to this module -- this test pins that down.
# ---------------------------------------------------------------------------


async def test_resolve_by_target_firefox_wildcard_selects_single_client(
    kontrol: server.ChromeKontrolServer,
) -> None:
    client = _register(kontrol, "firefox", "firefox")
    result = kontrol._resolve_by_target("firefox:*")
    assert result is client


# ---------------------------------------------------------------------------
# _resolve_by_target(): browser_part present, 4-step identifier matching
# ---------------------------------------------------------------------------


async def test_resolve_by_target_browser_scoped_label_match(kontrol: server.ChromeKontrolServer) -> None:
    client = _register(kontrol, "chrome:a", "chrome", profile_id="a", label="メイン")
    _register(kontrol, "edge", "edge", label="メイン")  # different browser; must not match
    result = kontrol._resolve_by_target("chrome:メイン")
    assert result is client


async def test_resolve_by_target_browser_scoped_email_match(kontrol: server.ChromeKontrolServer) -> None:
    client = _register(kontrol, "chrome:a", "chrome", profile_id="a", email="work@example.com")
    result = kontrol._resolve_by_target("chrome:work@example.com")
    assert result is client


async def test_resolve_by_target_browser_scoped_email_local_part_match(kontrol: server.ChromeKontrolServer) -> None:
    client = _register(kontrol, "chrome:a", "chrome", profile_id="a", email="work@example.com")
    result = kontrol._resolve_by_target("chrome:work")
    assert result is client


async def test_resolve_by_target_browser_scoped_profile_id_prefix_match(
    kontrol: server.ChromeKontrolServer,
) -> None:
    client = _register(kontrol, "chrome:a3f2c1d8e9b1", "chrome", profile_id="a3f2c1d8e9b1")
    result = kontrol._resolve_by_target("chrome:a3f2")
    assert result is client


async def test_resolve_by_target_browser_scoped_ambiguous_at_label_step(
    kontrol: server.ChromeKontrolServer,
) -> None:
    _register(kontrol, "chrome:a", "chrome", profile_id="a", label="サブ")
    _register(kontrol, "chrome:b", "chrome", profile_id="b", label="サブ")
    result = kontrol._resolve_by_target("chrome:サブ")
    assert isinstance(result, dict)
    assert result["result"] == "error"


async def test_resolve_by_target_browser_scoped_not_connected(kontrol: server.ChromeKontrolServer) -> None:
    _register(kontrol, "chrome:a", "chrome", profile_id="a", label="メイン")
    result = kontrol._resolve_by_target("chrome:サブ")
    assert isinstance(result, dict)
    assert result["result"] == "error"
    assert "not connected" in result["message"]
    assert "chrome:a (メイン)" in result["message"]  # still lists what IS connected


# ---------------------------------------------------------------------------
# _resolve_by_target(): no browser_part, identifier_part is a bare browser name
# ---------------------------------------------------------------------------


async def test_resolve_by_target_bare_browser_name_selects_single_client(
    kontrol: server.ChromeKontrolServer,
) -> None:
    client = _register(kontrol, "chrome", "chrome")
    result = kontrol._resolve_by_target("chrome")
    assert result is client


async def test_resolve_by_target_bare_browser_name_case_insensitive(kontrol: server.ChromeKontrolServer) -> None:
    client = _register(kontrol, "chrome", "chrome")
    result = kontrol._resolve_by_target("Chrome")
    assert result is client


async def test_resolve_by_target_bare_browser_name_ambiguous(kontrol: server.ChromeKontrolServer) -> None:
    _register(kontrol, "chrome:a", "chrome", profile_id="a")
    _register(kontrol, "chrome:b", "chrome", profile_id="b")
    result = kontrol._resolve_by_target("chrome")
    assert isinstance(result, dict)
    assert result["result"] == "error"


async def test_resolve_by_target_bare_browser_name_not_connected(kontrol: server.ChromeKontrolServer) -> None:
    result = kontrol._resolve_by_target("edge")
    assert isinstance(result, dict)
    assert result["result"] == "error"
    assert "not connected" in result["message"]


# ---------------------------------------------------------------------------
# _resolve_by_target(): no browser_part, global 4-step identifier matching
# ---------------------------------------------------------------------------


async def test_resolve_by_target_global_identifier_match_across_browsers(
    kontrol: server.ChromeKontrolServer,
) -> None:
    client = _register(kontrol, "edge", "edge", label="メイン")
    _register(kontrol, "chrome", "chrome", label="サブ")
    result = kontrol._resolve_by_target("メイン")
    assert result is client


async def test_resolve_by_target_global_identifier_not_connected(kontrol: server.ChromeKontrolServer) -> None:
    result = kontrol._resolve_by_target("存在しない")
    assert isinstance(result, dict)
    assert result["result"] == "error"
    assert "not connected" in result["message"]
    assert "(none)" in result["message"]


# ---------------------------------------------------------------------------
# Alias resolution (spec section 3-1): exactly one level of indirection.
# ---------------------------------------------------------------------------


async def test_resolve_by_target_resolves_alias(kontrol: server.ChromeKontrolServer) -> None:
    kontrol._aliases = {"仕事": "chrome:work@example.com"}
    client = _register(kontrol, "chrome:a", "chrome", profile_id="a", email="work@example.com")
    result = kontrol._resolve_by_target("仕事")
    assert result is client


async def test_resolve_by_target_alias_lookup_skips_non_matching_keys_first(
    kontrol: server.ChromeKontrolServer,
) -> None:
    """With multiple configured aliases, a target must still resolve correctly when it
    matches a key other than the first one iterated (exercises the loop's "keep looking"
    branch in _apply_alias(), not just its first-key-matches shortcut).
    """
    kontrol._aliases = {
        "メイン": "chrome:main@example.com",
        "サブ": "chrome:sub@example.com",
        "Edge": "edge:*",
    }
    client = _register(kontrol, "chrome:b", "chrome", profile_id="b", email="sub@example.com")
    result = kontrol._resolve_by_target("サブ")
    assert result is client


async def test_resolve_by_target_alias_lookup_is_case_insensitive(kontrol: server.ChromeKontrolServer) -> None:
    kontrol._aliases = {"Edge": "edge:*"}
    client = _register(kontrol, "edge", "edge")
    result = kontrol._resolve_by_target("edge")  # lowercase target vs. "Edge" alias key
    assert result is client


async def test_resolve_by_target_alias_recursion_stops_after_one_level(
    kontrol: server.ChromeKontrolServer,
) -> None:
    """ "A" resolves to "B", and "B" happens to also be an alias key -- but "B" the VALUE
    must be used verbatim as the resolved string, never re-looked-up as another alias key.
    """
    kontrol._aliases = {"A": "B", "B": "chrome:should-not-be-used@example.com"}
    # Register a client whose bare-identifier match is the literal string "B" (via label),
    # so if recursion incorrectly happened, "A" -> "B" -> alias lookup would instead resolve
    # to the "chrome:should-not-be-used@example.com" client. Here there is no such client,
    # so a correct one-level resolution must fail to connect, not silently succeed via a
    # different client.
    result = kontrol._resolve_by_target("A")
    assert isinstance(result, dict)
    assert result["result"] == "error"
    # The not-connected message must show what "A" resolved to literally: "B", not a further
    # re-resolution of "B" as an alias key.
    assert "resolved to 'B'" in result["message"]


async def test_resolve_by_target_not_connected_message_includes_alias_and_resolved_names(
    kontrol: server.ChromeKontrolServer,
) -> None:
    kontrol._aliases = {"仕事": "chrome:work@example.com"}
    result = kontrol._resolve_by_target("仕事")
    assert isinstance(result, dict)
    assert "Target '仕事' resolved to 'chrome:work@example.com'" in result["message"]
    assert "(none)" in result["message"]


async def test_resolve_by_target_not_connected_message_omits_alias_wording_for_direct_target(
    kontrol: server.ChromeKontrolServer,
) -> None:
    """Spec section 4-2: when target was NOT an alias, the error mentions only the
    resolved string, not a nonexistent "original name".
    """
    result = kontrol._resolve_by_target("chrome:nobody@example.com")
    assert isinstance(result, dict)
    assert "resolved to" not in result["message"]
    assert "chrome:nobody@example.com" in result["message"]


# ---------------------------------------------------------------------------
# NFC normalization (spec section 3-4): target values are normalized before matching.
# ---------------------------------------------------------------------------


async def test_resolve_by_target_nfd_target_matches_nfc_label(kontrol: server.ChromeKontrolServer) -> None:
    nfc_label = "が"
    client = _register(kontrol, "chrome", "chrome", label=nfc_label)
    nfd_target = unicodedata.normalize("NFD", nfc_label)
    assert nfd_target != nfc_label  # sanity: they really differ byte-for-byte

    result = kontrol._resolve_by_target(nfd_target)
    assert result is client


async def test_resolve_by_target_nfd_alias_key_matches_nfc_target(kontrol: server.ChromeKontrolServer) -> None:
    """Aliases are stored NFC-normalized by _load_aliases(); an NFD-form target string
    must still match the alias key (both are normalized before the casefold comparison).
    """
    nfc_key = "が"
    kontrol._aliases = {nfc_key: "chrome:*"}
    client = _register(kontrol, "chrome", "chrome")

    nfd_target = unicodedata.normalize("NFD", nfc_key)
    result = kontrol._resolve_by_target(nfd_target)
    assert result is client


# ---------------------------------------------------------------------------
# _resolve_client()'s target-aware dispatch.
# ---------------------------------------------------------------------------


async def test_resolve_client_uses_target_when_provided(kontrol: server.ChromeKontrolServer) -> None:
    client = _register(kontrol, "chrome", "chrome", label="メイン")
    result = await kontrol._resolve_client(None, timeout=1.0, target="メイン")
    assert result is client.websocket


async def test_resolve_client_target_does_not_wait_for_a_future_connection(
    kontrol: server.ChromeKontrolServer,
) -> None:
    """Unlike the "browser" path, an unresolved target must return immediately rather
    than polling for a client that might connect later (Phase 2c auto-launch does not
    exist yet; waiting here would just time out uselessly).
    """
    result = await asyncio.wait_for(
        kontrol._resolve_client(None, timeout=5.0, target="chrome:nobody@example.com"), timeout=0.5
    )
    assert isinstance(result, dict)
    assert result["result"] == "error"


async def test_resolve_client_target_error_dict_propagates_through_send_command(
    kontrol: server.ChromeKontrolServer,
) -> None:
    """Integration-style check across send_command() -> _send_command_locked() ->
    _resolve_client() -> _resolve_by_target(), confirming the target kwarg is threaded
    all the way through without being dropped.
    """
    kontrol._aliases = {"仕事": "chrome:work@example.com"}
    result = await kontrol.send_command({"cmd": "list_tabs"}, target="仕事", timeout=0.5)
    assert result["result"] == "error"
    assert "仕事" in result["message"]


async def test_resolve_client_target_success_propagates_through_send_command(
    kontrol: server.ChromeKontrolServer,
) -> None:
    client = _register(kontrol, "chrome:a", "chrome", profile_id="a", label="メイン")
    task = asyncio.create_task(kontrol.send_command({"cmd": "list_tabs"}, target="メイン", timeout=1.0))
    for _ in range(5):
        await asyncio.sleep(0)
    ws = client.websocket
    assert isinstance(ws, FakeWebSocket)
    assert ws.sent_messages == [json.dumps({"cmd": "list_tabs"})]

    await kontrol._handle_message(json.dumps({"result": "ok", "data": []}), "chrome:a")
    result = await task
    assert result == {"result": "ok", "data": []}


# ---------------------------------------------------------------------------
# ISSUES.md P0-5 (Phase F6): the "Connected: ..." listing in the not-connected
# error must never leak email via display_name (same regression as the
# ambiguity message covered in tests/test_resolve_client.py; this file covers
# the _format_not_connected_message() side, reached through
# _resolve_by_target()).
# ---------------------------------------------------------------------------


async def test_resolve_by_target_not_connected_excludes_email_when_label_unset(
    kontrol: server.ChromeKontrolServer,
) -> None:
    """The one connected client has no label, so display_name would have fallen back to
    its email. The "Connected: ..." listing must show its key instead.
    """
    _register(kontrol, "chrome:a", "chrome", profile_id="a", email="carol@example.com")
    result = kontrol._resolve_by_target("edge")
    assert isinstance(result, dict)
    assert result["result"] == "error"
    assert "@" not in result["message"]
    assert "chrome:a" in result["message"]


async def test_resolve_by_target_not_connected_shows_none_placeholder_when_empty(
    kontrol: server.ChromeKontrolServer,
) -> None:
    """Re-asserted alongside the PII regression tests above so the "(none)" contract for
    zero connected clients is documented next to the code path that could regress it.
    """
    result = kontrol._resolve_by_target("edge")
    assert isinstance(result, dict)
    assert "Connected: (none)." in result["message"]
