"""
Location   : ChromeKontrol/tests/test_client_info.py
Purpose    : Unit tests for server.ClientInfo's key and display_name properties.
Why        : These two properties are the load-bearing logic behind
             _clients' dict keys and every human-facing name (list_clients'
             displayName, and _resolve_client's ambiguity-error candidate
             list). Getting the fallback order wrong silently misidentifies
             which profile a command actually reached — exactly the failure
             mode ISSUES.md P0-1 describes in production.
Related    : server.py, ISSUES.md P0-1
"""

from __future__ import annotations

import dataclasses

import pytest

import server
from tests.conftest import FakeWebSocket, as_ws_protocol


def _client(
    browser: str = "chrome",
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


def test_key_is_bare_browser_name_without_profile_id() -> None:
    assert _client().key == "chrome"


def test_key_combines_browser_and_profile_id_when_present() -> None:
    assert _client(profile_id="a3f2c1d8").key == "chrome:a3f2c1d8"


def test_key_differs_by_browser_for_the_same_profile_id() -> None:
    """Sanity check that key is not just profile_id: same profileId, different browser."""
    assert _client("chrome", profile_id="shared-id").key != _client("edge", profile_id="shared-id").key


def test_display_name_prefers_label_over_everything() -> None:
    client = _client(profile_id="a3f2c1d8", email="user@example.com", label="メイン")
    assert client.display_name == "メイン"


def test_display_name_falls_back_to_email_without_label() -> None:
    client = _client(profile_id="a3f2c1d8", email="user@example.com")
    assert client.display_name == "user@example.com"


def test_display_name_falls_back_to_profile_id_prefix_without_label_or_email() -> None:
    client = _client(profile_id="a3f2c1d8e9b1")
    assert client.display_name == "a3f2c1d8"


def test_display_name_falls_back_to_browser_with_nothing_else_set() -> None:
    client = _client("edge")
    assert client.display_name == "edge"


def test_client_info_is_frozen() -> None:
    """ClientInfo instances are immutable once constructed (dataclass(frozen=True))."""
    client = _client()
    with pytest.raises(dataclasses.FrozenInstanceError):
        client.browser = "edge"  # type: ignore[misc]
