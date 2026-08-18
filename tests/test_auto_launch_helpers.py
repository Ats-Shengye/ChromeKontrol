"""
Location   : ChromeKontrol/tests/test_auto_launch_helpers.py
Purpose    : Unit tests for ChromeKontrolServer._lookup_profile_directory(),
             ._match_candidates_for_resolved(), ._count_candidates_for_resolved(), and
             ._wait_for_target() (ISSUES.md P0-1, Phase 2c).
Why        : These are the building blocks _auto_launch_response() (tested separately in
             test_auto_launch.py) composes to decide *whether* a resolved target is
             eligible for auto-launch and to detect when a freshly-launched browser's
             extension has connected. _match_candidates_for_resolved() in particular was
             extracted from the pre-existing, well-tested _resolve_resolved_string() by
             pure code motion (no behavior change) specifically so this refactor could be
             verified in isolation without touching the Phase 2b alias-resolution tests
             in test_resolve_target.py.
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
) -> server.ClientInfo:
    client = server.ClientInfo(
        browser=browser,
        websocket=as_ws_protocol(FakeWebSocket()),
        profile_id=profile_id,
        email=email,
        label=label,
    )
    kontrol._clients[key] = client
    return client


# ---------------------------------------------------------------------------
# _lookup_profile_directory()
# ---------------------------------------------------------------------------


def test_lookup_profile_directory_finds_exact_match(kontrol: server.ChromeKontrolServer) -> None:
    kontrol._profiles = {"chrome:work@example.com": "Profile 1"}
    assert kontrol._lookup_profile_directory("chrome:work@example.com") == "Profile 1"


def test_lookup_profile_directory_is_case_insensitive(kontrol: server.ChromeKontrolServer) -> None:
    kontrol._profiles = {"chrome:Work@Example.com": "Profile 1"}
    assert kontrol._lookup_profile_directory("chrome:work@example.com") == "Profile 1"


def test_lookup_profile_directory_returns_none_when_absent(kontrol: server.ChromeKontrolServer) -> None:
    kontrol._profiles = {"chrome:other@example.com": "Profile 1"}
    assert kontrol._lookup_profile_directory("chrome:work@example.com") is None


def test_lookup_profile_directory_returns_none_when_empty(kontrol: server.ChromeKontrolServer) -> None:
    assert kontrol._lookup_profile_directory("chrome:work@example.com") is None


# ---------------------------------------------------------------------------
# _match_candidates_for_resolved() / _count_candidates_for_resolved()
# ---------------------------------------------------------------------------


def test_match_candidates_returns_empty_list_when_nothing_connected(kontrol: server.ChromeKontrolServer) -> None:
    assert kontrol._match_candidates_for_resolved("chrome:work@example.com") == []
    assert kontrol._count_candidates_for_resolved("chrome:work@example.com") == 0


def test_match_candidates_returns_matching_client(kontrol: server.ChromeKontrolServer) -> None:
    client = _register(kontrol, "chrome:a", "chrome", profile_id="a", email="work@example.com")
    result = kontrol._match_candidates_for_resolved("chrome:work@example.com")
    assert result == [client]
    assert kontrol._count_candidates_for_resolved("chrome:work@example.com") == 1


def test_match_candidates_returns_multiple_for_wildcard(kontrol: server.ChromeKontrolServer) -> None:
    a = _register(kontrol, "chrome:a", "chrome", profile_id="a")
    b = _register(kontrol, "chrome:b", "chrome", profile_id="b")
    result = kontrol._match_candidates_for_resolved("chrome:*")
    assert set(result) == {a, b}
    assert kontrol._count_candidates_for_resolved("chrome:*") == 2


def test_match_candidates_returns_error_dict_for_unknown_browser(kontrol: server.ChromeKontrolServer) -> None:
    """ "safari" (not "firefox") is used here: firefox joined ALLOWED_BROWSERS in Phase F2
    (see test_match_candidates_treats_firefox_as_recognised_browser below), so it no
    longer exercises this "unrecognised browser part" branch.
    """
    result = kontrol._match_candidates_for_resolved("safari:someone@example.com")
    assert isinstance(result, dict)
    assert result["result"] == "error"


def test_count_candidates_returns_zero_for_unknown_browser(kontrol: server.ChromeKontrolServer) -> None:
    """An error dict (unrecognised browser part) counts as zero candidates -- auto-launch
    is not a meaningful outcome for a target naming an unsupported browser.
    """
    assert kontrol._count_candidates_for_resolved("safari:someone@example.com") == 0


# ---------------------------------------------------------------------------
# Phase F2: 'firefox' joins ALLOWED_BROWSERS. It must be treated as a
# recognised (if currently client-less) browser here, not as the
# "unrecognised browser part" error dict case above.
# ---------------------------------------------------------------------------


def test_match_candidates_treats_firefox_as_recognised_browser(kontrol: server.ChromeKontrolServer) -> None:
    result = kontrol._match_candidates_for_resolved("firefox:someone@example.com")
    assert result == []
    assert kontrol._count_candidates_for_resolved("firefox:someone@example.com") == 0


# ---------------------------------------------------------------------------
# _wait_for_target()
# ---------------------------------------------------------------------------


async def test_wait_for_target_returns_immediately_when_already_connected(
    kontrol: server.ChromeKontrolServer,
) -> None:
    client = _register(kontrol, "chrome:a", "chrome", profile_id="a", email="work@example.com")
    result = await asyncio.wait_for(kontrol._wait_for_target("chrome:work@example.com", timeout=1.0), timeout=0.5)
    assert result is client


async def test_wait_for_target_polls_until_connection_appears(kontrol: server.ChromeKontrolServer) -> None:
    async def connect_after_delay() -> None:
        await asyncio.sleep(0.15)
        _register(kontrol, "chrome:a", "chrome", profile_id="a", email="work@example.com")

    connector = asyncio.create_task(connect_after_delay())
    try:
        result = await asyncio.wait_for(kontrol._wait_for_target("chrome:work@example.com", timeout=2.0), timeout=2.0)
    finally:
        await connector
    assert result is not None
    assert result.email == "work@example.com"


async def test_wait_for_target_times_out_when_nothing_connects(kontrol: server.ChromeKontrolServer) -> None:
    result = await asyncio.wait_for(kontrol._wait_for_target("chrome:work@example.com", timeout=0.05), timeout=1.0)
    assert result is None


async def test_wait_for_target_keeps_polling_through_ambiguous_state(
    kontrol: server.ChromeKontrolServer,
) -> None:
    """If two clients briefly both match (e.g. the user manually opened another profile
    concurrently), _wait_for_target must not return an ambiguity error -- it should keep
    polling until the match count settles at exactly one connection, or time out.
    """
    _register(kontrol, "chrome:a", "chrome", profile_id="a", label="サブ")
    _register(kontrol, "chrome:b", "chrome", profile_id="b", label="サブ")
    result = await asyncio.wait_for(kontrol._wait_for_target("サブ", timeout=0.05), timeout=1.0)
    assert result is None  # never resolved to a single match; times out rather than erroring
