"""
Location   : ChromeKontrol/tests/test_resolve_client.py
Purpose    : Unit tests for ChromeKontrolServer._resolve_client().
Why        : This is the target-selection logic behind every command
             dispatch: explicit-browser lookup (with and without a
             connection-wait), auto-selection when exactly one client is
             connected, and the "ambiguous, refuse to guess" error when
             multiple clients are connected. Since ISSUES.md P0-1 (Phase 2a)
             a "browser" match can now resolve to more than one profile of
             the same browser, so the ambiguity path must also be exercised
             for that case specifically — getting it wrong risks commands
             silently landing on the wrong browser/profile (the exact
             failure mode ISSUES.md P0-1 describes in production).
Related    : server.py, ISSUES.md P0-1
"""

from __future__ import annotations

import asyncio

import pytest

import server
from tests.conftest import FakeWebSocket, as_ws_protocol


@pytest.fixture
def kontrol() -> server.ChromeKontrolServer:
    return server.ChromeKontrolServer()


def _register(
    kontrol: server.ChromeKontrolServer,
    key: str,
    browser: str,
    *,
    profile_id: str | None = None,
    email: str | None = None,
    label: str | None = None,
) -> FakeWebSocket:
    """Register a fake client under `key`, returning its FakeWebSocket for identity checks."""
    ws = FakeWebSocket()
    kontrol._clients[key] = server.ClientInfo(
        browser=browser,
        websocket=as_ws_protocol(ws),
        profile_id=profile_id,
        email=email,
        label=label,
    )
    return ws


async def test_resolve_client_returns_immediately_when_browser_already_connected(
    kontrol: server.ChromeKontrolServer,
) -> None:
    ws = _register(kontrol, "chrome", "chrome")
    result = await kontrol._resolve_client("chrome", timeout=1.0)
    assert result is as_ws_protocol(ws)


async def test_resolve_client_waits_for_requested_browser_to_connect(
    kontrol: server.ChromeKontrolServer,
) -> None:
    """If the requested browser is not yet connected, _resolve_client polls until it appears."""
    ws = FakeWebSocket()

    async def connect_after_delay() -> None:
        await asyncio.sleep(0.01)
        kontrol._clients["chrome"] = server.ClientInfo(browser="chrome", websocket=as_ws_protocol(ws))

    connector = asyncio.create_task(connect_after_delay())
    try:
        # Outer bound so a broken implementation fails the test fast instead of hanging.
        result = await asyncio.wait_for(kontrol._resolve_client("chrome", timeout=1.0), timeout=1.0)
    finally:
        await connector
    assert result is as_ws_protocol(ws)


async def test_resolve_client_waits_for_any_client_when_browser_not_specified(
    kontrol: server.ChromeKontrolServer,
) -> None:
    """Mirrors the requested-browser wait test, but for the browser=None ("any client") path."""
    ws = FakeWebSocket()

    async def connect_after_delay() -> None:
        await asyncio.sleep(0.01)
        kontrol._clients["edge"] = server.ClientInfo(browser="edge", websocket=as_ws_protocol(ws))

    connector = asyncio.create_task(connect_after_delay())
    try:
        result = await asyncio.wait_for(kontrol._resolve_client(None, timeout=1.0), timeout=1.0)
    finally:
        await connector
    assert result is as_ws_protocol(ws)


async def test_resolve_client_times_out_when_requested_browser_never_connects(
    kontrol: server.ChromeKontrolServer,
) -> None:
    result = await kontrol._resolve_client("chrome", timeout=0.05)
    assert isinstance(result, dict)
    assert result["result"] == "error"
    assert "chrome" in result["message"].lower()
    assert "timed out" in result["message"].lower()


async def test_resolve_client_times_out_when_no_browser_requested_and_none_connect(
    kontrol: server.ChromeKontrolServer,
) -> None:
    """Phase F2 M-1: the message is built from ALLOWED_BROWSERS at call time
    (via server._format_browser_list), not hardcoded as "Chrome or Edge". We
    assert the fixed scaffolding plus membership of every ALLOWED_BROWSERS
    entry rather than the full string, so this test keeps passing unchanged
    if a browser is ever added to or removed from ALLOWED_BROWSERS.
    """
    result = await kontrol._resolve_client(None, timeout=0.05)
    assert isinstance(result, dict)
    assert result["result"] == "error"
    assert result["message"].startswith("Timed out waiting for extension. Is ChromeKontrol loaded in ")
    assert result["message"].endswith("?")
    for browser in server.ALLOWED_BROWSERS:
        assert browser.capitalize() in result["message"]


async def test_resolve_client_auto_selects_single_connected_client(
    kontrol: server.ChromeKontrolServer,
) -> None:
    ws = _register(kontrol, "edge", "edge")
    result = await kontrol._resolve_client(None, timeout=1.0)
    assert result is as_ws_protocol(ws)


async def test_resolve_client_rejects_ambiguous_multiple_clients(
    kontrol: server.ChromeKontrolServer,
) -> None:
    """With no explicit browser and 2+ clients connected, the caller must specify one."""
    _register(kontrol, "chrome", "chrome")
    _register(kontrol, "edge", "edge")
    result = await kontrol._resolve_client(None, timeout=1.0)
    assert isinstance(result, dict)
    assert result == {
        "result": "error",
        "message": ('Multiple clients matched (chrome, edge); ' 'specify "browser" or "target" to select one.'),
    }


async def test_resolve_client_multiple_clients_message_lists_names_sorted() -> None:
    """The candidate list in the ambiguity error is sorted by key, independent of connection order."""
    kontrol = server.ChromeKontrolServer()
    _register(kontrol, "edge", "edge")
    _register(kontrol, "chrome", "chrome")
    result = await kontrol._resolve_client(None, timeout=1.0)
    assert isinstance(result, dict)
    assert "chrome, edge" in result["message"]


# ---------------------------------------------------------------------------
# ISSUES.md P0-1 (Phase 2a): a "browser" match can now span multiple profiles.
# ---------------------------------------------------------------------------


async def test_resolve_client_auto_selects_when_browser_matches_exactly_one_profile(
    kontrol: server.ChromeKontrolServer,
) -> None:
    """browser="chrome" with only one Chrome profile connected still auto-selects it,
    even though the client is keyed by "chrome:profileId" rather than bare "chrome".
    """
    ws = _register(kontrol, "chrome:profile-a", "chrome", profile_id="profile-a")
    other_ws = _register(kontrol, "edge", "edge")

    result = await kontrol._resolve_client("chrome", timeout=1.0)

    assert result is as_ws_protocol(ws)
    assert result is not as_ws_protocol(other_ws)


async def test_resolve_client_rejects_ambiguous_when_browser_matches_multiple_profiles(
    kontrol: server.ChromeKontrolServer,
) -> None:
    """ISSUES.md P0-1's core fix: specifying browser="chrome" with two Chrome profiles
    connected must not silently pick one; it must refuse and list both candidates
    (this is exactly the scenario where the pre-fix server always resolved to whichever
    profile connected first — "Default" — and "Profile 1" was unreachable).
    """
    _register(kontrol, "chrome:profile-a", "chrome", profile_id="profile-a", label="メイン")
    _register(kontrol, "chrome:profile-b", "chrome", profile_id="profile-b", label="サブ")

    result = await kontrol._resolve_client("chrome", timeout=1.0)

    assert isinstance(result, dict)
    assert result["result"] == "error"
    assert "chrome:profile-a (メイン)" in result["message"]
    assert "chrome:profile-b (サブ)" in result["message"]
    assert 'specify "browser" or "target" to select one.' in result["message"]


async def test_resolve_client_waits_then_finds_multiple_profiles_for_requested_browser(
    kontrol: server.ChromeKontrolServer,
) -> None:
    """If the requested browser isn't connected yet, _resolve_client waits for *any* match —
    but once one appears, it must still check for ambiguity among all profiles of that
    browser rather than assuming the first arrival is the only one.
    """
    ws1 = FakeWebSocket()

    async def connect_two_profiles_after_delay() -> None:
        await asyncio.sleep(0.01)
        kontrol._clients["chrome:profile-a"] = server.ClientInfo(
            browser="chrome", websocket=as_ws_protocol(ws1), profile_id="profile-a"
        )
        kontrol._clients["chrome:profile-b"] = server.ClientInfo(
            browser="chrome", websocket=as_ws_protocol(FakeWebSocket()), profile_id="profile-b"
        )

    connector = asyncio.create_task(connect_two_profiles_after_delay())
    try:
        result = await asyncio.wait_for(kontrol._resolve_client("chrome", timeout=1.0), timeout=1.0)
    finally:
        await connector

    assert isinstance(result, dict)
    assert result["result"] == "error"
    assert "chrome:profile-a" in result["message"]
    assert "chrome:profile-b" in result["message"]


# ---------------------------------------------------------------------------
# ISSUES.md P0-5 (Phase F6): candidate listing must never leak email.
#
# _format_ambiguous_clients_message() previously listed each candidate as
# "key (display_name)". display_name falls back to email when no label is
# configured, so an unlabelled client's email address leaked into an error
# message reachable from every MCP tool's error path (list_tabs / get_dom /
# get_elements / click / get_text), where it persists into the calling LLM's
# context. _format_client_candidates() replaces display_name with key + label
# only; the tests below pin that contract directly (unit-level) and through
# the public error-message path (integration-level). The "@" not in message
# assertions are deliberately phrased as an absence check on a class of value
# rather than the full message string, so a future change to the message
# wording cannot silently let email back in without failing a test.
# ---------------------------------------------------------------------------


def test_format_client_candidates_empty_list_returns_empty_string() -> None:
    """Empty-input contract that _format_not_connected_message() depends on to detect
    "zero clients connected" and substitute the "(none)" placeholder.
    """
    assert server._format_client_candidates([]) == ""


def test_format_client_candidates_shows_key_only_when_label_unset() -> None:
    """ISSUES.md P0-5 core regression: a client with an email but no label must render
    as its bare key. Before this fix, display_name fell through to email here, so this
    client would have rendered as "chrome (user@example.com)".
    """
    client = server.ClientInfo(
        browser="chrome",
        websocket=as_ws_protocol(FakeWebSocket()),
        email="user@example.com",
    )
    result = server._format_client_candidates([client])
    assert result == "chrome"
    assert "@" not in result


def test_format_client_candidates_shows_label_in_parentheses_when_set() -> None:
    """A configured label is not PII (the user chose it themselves) and remains useful
    for telling candidates apart, so it is still appended in parentheses.
    """
    client = server.ClientInfo(
        browser="chrome",
        websocket=as_ws_protocol(FakeWebSocket()),
        profile_id="a",
        label="仕事用",
    )
    result = server._format_client_candidates([client])
    assert result == "chrome:a (仕事用)"


def test_format_client_candidates_sorts_by_key_regardless_of_input_order() -> None:
    client_edge = server.ClientInfo(browser="edge", websocket=as_ws_protocol(FakeWebSocket()))
    client_chrome = server.ClientInfo(browser="chrome", websocket=as_ws_protocol(FakeWebSocket()))
    result = server._format_client_candidates([client_edge, client_chrome])
    assert result == "chrome, edge"


async def test_resolve_client_ambiguous_message_excludes_email_when_label_unset(
    kontrol: server.ChromeKontrolServer,
) -> None:
    """Integration-level counterpart of the display_name regression above, exercised
    through the actual error path a caller (including an MCP tool) receives: two
    unlabelled clients with distinct emails must not leak either address.
    """
    _register(kontrol, "chrome:a", "chrome", profile_id="a", email="alice@example.com")
    _register(kontrol, "chrome:b", "chrome", profile_id="b", email="bob@example.com")
    result = await kontrol._resolve_client(None, timeout=1.0)
    assert isinstance(result, dict)
    assert "@" not in result["message"]
    assert "chrome:a" in result["message"]
    assert "chrome:b" in result["message"]


async def test_resolve_client_ambiguous_message_includes_label_but_not_unset_ones(
    kontrol: server.ChromeKontrolServer,
) -> None:
    """Labelled and unlabelled clients can appear side by side in the same ambiguity
    error: the labelled one shows "key (label)", the unlabelled one shows bare key with
    no parentheses at all (not empty parentheses).
    """
    _register(kontrol, "chrome:a", "chrome", profile_id="a", label="仕事用")
    _register(kontrol, "chrome:b", "chrome", profile_id="b")
    result = await kontrol._resolve_client(None, timeout=1.0)
    assert isinstance(result, dict)
    assert "chrome:a (仕事用)" in result["message"]
    assert "chrome:b" in result["message"]
    assert "chrome:b (" not in result["message"]


# ---------------------------------------------------------------------------
# ISSUES.md P1-5 (Phase F7): browser/target-omitted auto-selection prefers
# the most recently focused client over refusing with an ambiguity error.
# ---------------------------------------------------------------------------


async def test_resolve_client_auto_selects_most_recently_focused_client(
    kontrol: server.ChromeKontrolServer,
) -> None:
    """With no browser/target and multiple clients connected, if _focus_ts has entries,
    the one with the largest ts wins instead of refusing with an ambiguity error.
    """
    _register(kontrol, "chrome", "chrome")
    ws_edge = _register(kontrol, "edge", "edge")
    kontrol._focus_ts = {"chrome": 100, "edge": 200}

    result = await kontrol._resolve_client(None, timeout=1.0)

    assert result is as_ws_protocol(ws_edge)


async def test_resolve_client_falls_back_to_ambiguous_error_when_no_client_has_focus_ts(
    kontrol: server.ChromeKontrolServer,
) -> None:
    """If no connected client has ever reported a focus timestamp (e.g. all extensions
    predate Phase F7), the pre-existing ambiguity error is unchanged.
    """
    _register(kontrol, "chrome", "chrome")
    _register(kontrol, "edge", "edge")

    result = await kontrol._resolve_client(None, timeout=1.0)

    assert isinstance(result, dict)
    assert result["result"] == "error"
    assert "chrome, edge" in result["message"]


async def test_resolve_client_auto_selects_among_partial_focus_ts_coverage(
    kontrol: server.ChromeKontrolServer,
) -> None:
    """Only some connected clients having reported a focus timestamp is enough to trigger
    auto-selection; clients with no entry are simply excluded from consideration (they do
    not force a fallback to the ambiguity error).
    """
    ws_chrome = _register(kontrol, "chrome", "chrome")
    _register(kontrol, "edge", "edge")  # never reports focus
    kontrol._focus_ts = {"chrome": 500}

    result = await kontrol._resolve_client(None, timeout=1.0)

    assert result is as_ws_protocol(ws_chrome)


async def test_resolve_client_focus_ts_tie_breaks_deterministically_by_key_ascending(
    kontrol: server.ChromeKontrolServer,
) -> None:
    """Equal ts values must not depend on max()/min()'s "first seen wins" behavior, which
    would silently depend on dict iteration order (== connection order). The tie-break is
    pinned to key-ascending so the choice is reproducible regardless of connection order.
    """
    ws_chrome = _register(kontrol, "chrome", "chrome")
    _register(kontrol, "edge", "edge")
    kontrol._focus_ts = {"chrome": 999, "edge": 999}

    result = await kontrol._resolve_client(None, timeout=1.0)

    assert result is as_ws_protocol(ws_chrome)  # "chrome" < "edge"


async def test_resolve_client_focus_ts_tie_break_is_independent_of_registration_order(
    kontrol: server.ChromeKontrolServer,
) -> None:
    """Same tie as above, but with clients registered in the opposite order, to confirm
    the key-ascending tie-break -- not dict insertion order -- decides the winner.
    """
    _register(kontrol, "edge", "edge")
    ws_chrome = _register(kontrol, "chrome", "chrome")
    kontrol._focus_ts = {"edge": 999, "chrome": 999}

    result = await kontrol._resolve_client(None, timeout=1.0)

    assert result is as_ws_protocol(ws_chrome)


async def test_resolve_client_ignores_focus_ts_entry_for_disconnected_client(
    kontrol: server.ChromeKontrolServer,
) -> None:
    """A stale _focus_ts entry for a key no longer present in _clients (should not happen
    in practice -- handle_connection's cleanup removes it -- but defended against anyway)
    must not be treated as a candidate.
    """
    ws_edge = _register(kontrol, "edge", "edge")
    kontrol._focus_ts = {"chrome": 99999, "edge": 1}  # "chrome" has no matching client

    result = await kontrol._resolve_client(None, timeout=1.0)

    assert result is as_ws_protocol(ws_edge)


async def test_resolve_client_auto_selection_does_not_apply_to_single_connected_client(
    kontrol: server.ChromeKontrolServer,
) -> None:
    """The pre-existing single-client auto-select path is untouched by Phase F7: it does
    not need to consult _focus_ts at all (there is nothing to disambiguate with only one
    client), and must still succeed even with zero _focus_ts entries.
    """
    ws = _register(kontrol, "edge", "edge")
    result = await kontrol._resolve_client(None, timeout=1.0)
    assert result is as_ws_protocol(ws)


async def test_resolve_client_target_path_ignores_focus_ts_even_when_ambiguous(
    kontrol: server.ChromeKontrolServer,
) -> None:
    """Focus-based auto-selection is scoped to the browser/target-omitted path only. When
    target resolves to multiple candidates, the ambiguity error still wins even if one
    candidate has a _focus_ts entry -- target selection must stay deterministic based on
    what the caller asked for, not on which candidate happens to be more recently focused.
    """
    _register(kontrol, "chrome:a", "chrome", profile_id="a", label="仕事")
    _register(kontrol, "chrome:b", "chrome", profile_id="b", label="仕事")
    kontrol._focus_ts = {"chrome:a": 999}

    result = await kontrol._resolve_client(None, timeout=1.0, target="仕事")

    assert isinstance(result, dict)
    assert result["result"] == "error"
