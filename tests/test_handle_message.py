"""
Location   : ChromeKontrol/tests/test_handle_message.py
Purpose    : Unit tests for ChromeKontrolServer._handle_message().
Why        : This is where every post-handshake WebSocket frame lands before
             becoming a command response. Its size guard (bytes and str
             paths separately), JSON parsing, and the "type"-based branching
             (ISSUES.md P0-1, Phase 2a) — identify messages warned-and-
             dropped, any other unrecognized "type" silently dropped so a
             future notification shape is never mistaken for a stale
             command's response, and the absence of "type" entirely (today's
             actual command response shape) still stored as normal — are all
             security- or correctness-relevant and must each be pinned down.
             _handle_message() gained a required `key` parameter in Phase F7
             (ISSUES.md P1-5) so it can attribute a {"type": "focus", ...}
             notification to the client that sent it; every call site below
             passes "chrome" since these tests only exercise the
             parsing/branching logic, not which client's _focus_ts entry
             gets written (that is test_handle_message_focus.py's job).
Related    : server.py, ISSUES.md P0-1, ISSUES.md P1-5
"""

from __future__ import annotations

import json

import pytest

import server


@pytest.fixture
def kontrol() -> server.ChromeKontrolServer:
    return server.ChromeKontrolServer()


async def test_handle_message_stores_valid_json_and_sets_event(
    kontrol: server.ChromeKontrolServer,
) -> None:
    payload = json.dumps({"result": "ok", "data": "<html></html>"})
    await kontrol._handle_message(payload, "chrome")
    assert kontrol._pending_response == {"result": "ok", "data": "<html></html>"}
    assert kontrol._response_event.is_set() is True


async def test_handle_message_accepts_bytes_input(kontrol: server.ChromeKontrolServer) -> None:
    payload = json.dumps({"result": "ok"}).encode("utf-8")
    await kontrol._handle_message(payload, "chrome")
    assert kontrol._pending_response == {"result": "ok"}
    assert kontrol._response_event.is_set() is True


async def test_handle_message_rejects_oversized_bytes_message(
    kontrol: server.ChromeKontrolServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server, "MAX_MESSAGE_BYTES", 8)
    await kontrol._handle_message(b"this is far more than eight bytes", "chrome")
    assert kontrol._pending_response == {"result": "error", "message": "Response too large."}
    assert kontrol._response_event.is_set() is True


async def test_handle_message_rejects_oversized_str_message(
    kontrol: server.ChromeKontrolServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server, "MAX_MESSAGE_BYTES", 8)
    await kontrol._handle_message("this is far more than eight bytes", "chrome")
    assert kontrol._pending_response == {"result": "error", "message": "Response too large."}
    assert kontrol._response_event.is_set() is True


async def test_handle_message_measures_str_size_in_utf8_bytes_not_code_points(
    kontrol: server.ChromeKontrolServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A multi-byte UTF-8 character must count by its encoded byte length, not 1 code point.

    "あ" (U+3042) encodes to 3 bytes in UTF-8; with a 2-byte limit this must
    be rejected even though it is a single Python character.
    """
    monkeypatch.setattr(server, "MAX_MESSAGE_BYTES", 2)
    await kontrol._handle_message("あ", "chrome")
    assert kontrol._pending_response == {"result": "error", "message": "Response too large."}


async def test_handle_message_rejects_non_json_payload(kontrol: server.ChromeKontrolServer) -> None:
    await kontrol._handle_message("not json", "chrome")
    assert kontrol._pending_response == {
        "result": "error",
        "message": "Non-JSON response from extension.",
    }
    assert kontrol._response_event.is_set() is True


async def test_handle_message_ignores_stray_identify_message(
    kontrol: server.ChromeKontrolServer,
) -> None:
    """An identify message arriving after the handshake is dropped, not treated as a response.

    Neither _pending_response nor the event should be touched, since a
    misbehaving/duplicated identify frame must never be mistaken for a
    command response.
    """
    kontrol._pending_response = None
    await kontrol._handle_message(json.dumps({"type": "identify", "browser": "chrome"}), "chrome")
    assert kontrol._pending_response is None
    assert kontrol._response_event.is_set() is False


async def test_handle_message_silently_ignores_unrecognized_type_field(
    kontrol: server.ChromeKontrolServer,
) -> None:
    """ISSUES.md P0-1 (Phase 2a): a dict with any "type" other than "identify"/"focus" is
    dropped silently (no warning log, unlike identify), not treated as a command response.

    This guards forthcoming notification types the server has not yet been taught about:
    once the extension starts sending some new {"type": "...", ...} shape, a server that
    predates it must not misinterpret one as a stale command's response. ("focus" itself
    graduated out of this bucket in Phase F7 — see test_handle_message_focus.py — so this
    test now uses a type the server is guaranteed to never recognize.)
    """
    kontrol._pending_response = None
    await kontrol._handle_message(json.dumps({"type": "other", "value": 1}), "chrome")
    assert kontrol._pending_response is None
    assert kontrol._response_event.is_set() is False


async def test_handle_message_treats_dict_without_type_field_as_normal_response(
    kontrol: server.ChromeKontrolServer,
) -> None:
    """A dict with no "type" key at all — the shape every current command response takes,
    since background.js never sets "type" on a response — is stored as the pending
    response. "type" itself is optional/legacy: its absence must not be treated as an
    unrecognized type and dropped.
    """
    await kontrol._handle_message(json.dumps({"result": "ok", "value": 1}), "chrome")
    assert kontrol._pending_response == {"result": "ok", "value": 1}
    assert kontrol._response_event.is_set() is True


async def test_handle_message_accepts_non_dict_json_as_response(
    kontrol: server.ChromeKontrolServer,
) -> None:
    """A JSON array response is stored as-is; only dicts are inspected for "type"=="identify"."""
    await kontrol._handle_message(json.dumps([1, 2, 3]), "chrome")
    # server.py declares _pending_response as `dict[str, Any] | None`, but
    # _handle_message actually assigns `data` (the raw json.loads() result,
    # typed Any) without narrowing it to a dict first — so at runtime it can
    # legitimately hold a list, as exercised here. The type: ignore documents
    # that mismatch between the declared type and actual behavior rather
    # than silencing an unrelated error; this test is exactly what surfaced it.
    assert kontrol._pending_response == [1, 2, 3]  # type: ignore[comparison-overlap]
    assert kontrol._response_event.is_set() is True
