"""
Location   : ChromeKontrol/tests/test_validate_command.py
Purpose    : Unit tests for server._validate_command().
Why        : This is the single structural gate for both the WebSocket CLI
             path (read_stdin_command -> _validate_command) and the HTTP path
             (_handle_http_request -> _validate_command), so every accepted
             or rejected shape must be pinned down: command whitelist,
             per-command selector requirement, the tabId bool-exclusion edge
             case (bool is a subclass of int in Python), and the optional
             browser field.
Related    : server.py
"""

from __future__ import annotations

from typing import Any

import pytest

import server


def test_validate_command_rejects_non_dict_payload() -> None:
    """A JSON array, string, number, or null is not a valid command envelope."""
    is_valid, message = server._validate_command(["get_dom"])
    assert is_valid is False
    assert message == "Command must be a JSON object."


def test_validate_command_rejects_missing_cmd_field() -> None:
    is_valid, message = server._validate_command({})
    assert is_valid is False
    assert message == "Unknown or missing command: None"


def test_validate_command_rejects_non_string_cmd_field() -> None:
    is_valid, message = server._validate_command({"cmd": 123})
    assert is_valid is False
    assert message == "Unknown or missing command: 123"


def test_validate_command_rejects_unknown_cmd() -> None:
    is_valid, message = server._validate_command({"cmd": "delete_everything"})
    assert is_valid is False
    assert message == "Unknown or missing command: delete_everything"


@pytest.mark.parametrize("cmd", ["get_dom", "click", "get_elements", "list_tabs", "list_clients"])
def test_validate_command_accepts_all_allowlisted_commands_with_required_fields(cmd: str) -> None:
    """Each of the five allowlisted commands passes when its own requirements are met."""
    payload: dict[str, Any] = {"cmd": cmd}
    if cmd in ("click", "get_elements"):
        payload["selector"] = "a.nav-link"
    is_valid, message = server._validate_command(payload)
    assert is_valid is True
    assert message == ""


def test_validate_command_list_clients_ignores_irrelevant_selector_field() -> None:
    """list_clients (ISSUES.md P0-1) never touches selector, mirroring get_dom/list_tabs's
    treatment: it is not one of the commands that require it, so a stray/malformed selector
    is simply ignored rather than rejected.
    """
    is_valid, message = server._validate_command({"cmd": "list_clients", "selector": 12345})
    assert is_valid is True
    assert message == ""


def test_validate_command_rejects_click_without_selector() -> None:
    is_valid, message = server._validate_command({"cmd": "click"})
    assert is_valid is False
    assert message == "Missing or invalid selector field."


def test_validate_command_rejects_get_elements_with_non_string_selector() -> None:
    is_valid, message = server._validate_command({"cmd": "get_elements", "selector": 42})
    assert is_valid is False
    assert message == "Missing or invalid selector field."


def test_validate_command_rejects_selector_exceeding_max_length() -> None:
    too_long = "a" * (server.MAX_SELECTOR_LENGTH + 1)
    is_valid, message = server._validate_command({"cmd": "click", "selector": too_long})
    assert is_valid is False
    assert message == f"Selector exceeds maximum length ({server.MAX_SELECTOR_LENGTH})."


def test_validate_command_accepts_selector_at_exact_max_length() -> None:
    """Boundary condition: exactly MAX_SELECTOR_LENGTH characters is still valid."""
    exactly_max = "a" * server.MAX_SELECTOR_LENGTH
    is_valid, message = server._validate_command({"cmd": "click", "selector": exactly_max})
    assert is_valid is True
    assert message == ""


def test_validate_command_ignores_selector_requirement_for_get_dom() -> None:
    """get_dom/list_tabs never touch the selector field, even if present or malformed."""
    is_valid, message = server._validate_command({"cmd": "get_dom", "selector": 12345})
    assert is_valid is True
    assert message == ""


def test_validate_command_rejects_tab_id_as_bool() -> None:
    """bool is a subclass of int in Python; server.py explicitly excludes it (spec item 2)."""
    is_valid, message = server._validate_command({"cmd": "get_dom", "tabId": True})
    assert is_valid is False
    assert message == "tabId field must be an integer."


def test_validate_command_rejects_tab_id_as_string() -> None:
    is_valid, message = server._validate_command({"cmd": "get_dom", "tabId": "42"})
    assert is_valid is False
    assert message == "tabId field must be an integer."


def test_validate_command_rejects_negative_tab_id() -> None:
    is_valid, message = server._validate_command({"cmd": "get_dom", "tabId": -1})
    assert is_valid is False
    assert message == "tabId must be a non-negative integer."


def test_validate_command_accepts_zero_tab_id() -> None:
    """Boundary condition: 0 is a valid (non-negative) tabId."""
    is_valid, message = server._validate_command({"cmd": "get_dom", "tabId": 0})
    assert is_valid is True
    assert message == ""


def test_validate_command_accepts_positive_tab_id() -> None:
    is_valid, message = server._validate_command({"cmd": "get_dom", "tabId": 42})
    assert is_valid is True
    assert message == ""


def test_validate_command_ignores_absent_tab_id() -> None:
    """tabId is optional; omitting it entirely is valid."""
    is_valid, message = server._validate_command({"cmd": "list_tabs"})
    assert is_valid is True
    assert message == ""


def test_validate_command_rejects_non_string_browser_field() -> None:
    is_valid, message = server._validate_command({"cmd": "get_dom", "browser": 1})
    assert is_valid is False
    assert message == "browser field must be a string."


def test_validate_command_rejects_unknown_browser() -> None:
    """ "safari" (not "firefox") is used here: firefox joined ALLOWED_BROWSERS in Phase F2,
    so it is now covered by test_validate_command_accepts_allowlisted_browser instead
    (parametrized dynamically over server.ALLOWED_BROWSERS).
    """
    is_valid, message = server._validate_command({"cmd": "get_dom", "browser": "safari"})
    assert is_valid is False
    assert message == "Unknown browser: safari"


@pytest.mark.parametrize("browser", sorted(server.ALLOWED_BROWSERS))
def test_validate_command_accepts_allowlisted_browser(browser: str) -> None:
    is_valid, message = server._validate_command({"cmd": "get_dom", "browser": browser})
    assert is_valid is True
    assert message == ""


def test_validate_command_ignores_absent_browser_field() -> None:
    """browser is optional; omitting it entirely is valid (auto-selection is a caller concern)."""
    is_valid, message = server._validate_command({"cmd": "get_dom"})
    assert is_valid is True
    assert message == ""


def test_validate_command_accepts_fully_populated_valid_command() -> None:
    """Integration-style check: every optional field present and individually valid."""
    is_valid, message = server._validate_command(
        {"cmd": "get_elements", "selector": "div.item", "tabId": 7, "browser": "edge"}
    )
    assert is_valid is True
    assert message == ""


# ---------------------------------------------------------------------------
# ISSUES.md P0-1 (Phase 2b): optional "target" field.
# ---------------------------------------------------------------------------


def test_validate_command_rejects_non_string_target_field() -> None:
    is_valid, message = server._validate_command({"cmd": "get_dom", "target": 42})
    assert is_valid is False
    assert message == "target field must be a string."


def test_validate_command_rejects_empty_string_target() -> None:
    is_valid, message = server._validate_command({"cmd": "get_dom", "target": ""})
    assert is_valid is False
    assert message == f"target field must be between 1 and {server.MAX_TARGET_LENGTH} characters."


def test_validate_command_rejects_target_exceeding_max_length() -> None:
    too_long = "a" * (server.MAX_TARGET_LENGTH + 1)
    is_valid, message = server._validate_command({"cmd": "get_dom", "target": too_long})
    assert is_valid is False
    assert message == f"target field must be between 1 and {server.MAX_TARGET_LENGTH} characters."


def test_validate_command_accepts_target_at_exact_max_length() -> None:
    exactly_max = "a" * server.MAX_TARGET_LENGTH
    is_valid, message = server._validate_command({"cmd": "get_dom", "target": exactly_max})
    assert is_valid is True
    assert message == ""


def test_validate_command_accepts_target_alone() -> None:
    is_valid, message = server._validate_command({"cmd": "get_dom", "target": "仕事"})
    assert is_valid is True
    assert message == ""


def test_validate_command_ignores_absent_target_field() -> None:
    """target is optional; omitting it entirely is valid (mirrors browser's behavior)."""
    is_valid, message = server._validate_command({"cmd": "get_dom"})
    assert is_valid is True
    assert message == ""


def test_validate_command_rejects_target_and_browser_specified_together() -> None:
    """Spec section 2: specifying both is a conflict that must not be silently resolved."""
    is_valid, message = server._validate_command({"cmd": "get_dom", "target": "仕事", "browser": "chrome"})
    assert is_valid is False
    assert message == 'Specify either "target" or "browser", not both.'
