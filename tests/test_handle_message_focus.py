"""
Location   : ChromeKontrol/tests/test_handle_message_focus.py
Purpose    : Unit tests for ChromeKontrolServer._handle_message()'s "focus"
             notification branch and _handle_focus_notification().
Why        : ISSUES.md P1-5 (Phase F7) teaches _handle_message() to recognize
             {"type": "focus", "ts": ...} as a one-way notification (not a
             command response) and record it into _focus_ts, keyed by the
             sending client. Every validation edge on "ts" matters: Python's
             bool is an int subclass, so True/False must be rejected
             explicitly rather than silently accepted as 1/0. Equally
             important is what must NOT happen on this path: a focus
             notification is not a request, so it must never touch
             _pending_response or _response_event — doing so would let a
             notification masquerade as the response some concurrent
             send_command() call is waiting for.
Related    : server.py, ISSUES.md P1-5
"""

from __future__ import annotations

import json

import pytest

import server


@pytest.fixture
def kontrol() -> server.ChromeKontrolServer:
    return server.ChromeKontrolServer()


async def test_focus_notification_with_valid_ts_records_it(
    kontrol: server.ChromeKontrolServer,
) -> None:
    await kontrol._handle_message(json.dumps({"type": "focus", "ts": 1234567890}), "chrome")
    assert kontrol._focus_ts == {"chrome": 1234567890}


async def test_focus_notification_accepts_float_ts_and_stores_as_int(
    kontrol: server.ChromeKontrolServer,
) -> None:
    """The client sends Date.now() (an integer in JS), but JSON numbers with a fractional
    part must still be accepted and truncated via int() for consistent comparisons in
    _resolve_client's auto-selection.
    """
    await kontrol._handle_message(json.dumps({"type": "focus", "ts": 1234567890.7}), "chrome")
    assert kontrol._focus_ts == {"chrome": 1234567890}


@pytest.mark.parametrize(
    "bad_ts",
    [
        -1,
        0,
        -1000,
        "1234567890",
        None,
        [1234567890],
        {"value": 1234567890},
        float("inf"),
        float("-inf"),
        float("nan"),
    ],
    ids=[
        "negative",
        "zero",
        "negative-large",
        "string",
        "missing-becomes-none",
        "list",
        "dict",
        "infinity",
        "negative-infinity",
        "nan",
    ],
)
async def test_focus_notification_with_invalid_ts_does_not_record(
    kontrol: server.ChromeKontrolServer, bad_ts: object
) -> None:
    """Security-Audit.md M-1: `float('inf')` (from a JSON `1e999` literal) previously
    passed `_is_valid_positive_timestamp()` because `inf > 0` is True, and would then
    blow up `int(inf)` with OverflowError inside this notification path. `-inf` and
    `nan` were already rejected by the pre-existing `value > 0` check (`nan > 0` is
    always False), but are included here too so a future change to that condition
    cannot silently regress them.
    """
    await kontrol._handle_message(json.dumps({"type": "focus", "ts": bad_ts}), "chrome")
    assert kontrol._focus_ts == {}


async def test_focus_notification_missing_ts_field_does_not_record(
    kontrol: server.ChromeKontrolServer,
) -> None:
    await kontrol._handle_message(json.dumps({"type": "focus"}), "chrome")
    assert kontrol._focus_ts == {}


@pytest.mark.parametrize("bool_ts", [True, False], ids=["true", "false"])
async def test_focus_notification_rejects_bool_ts_despite_being_an_int_subclass(
    kontrol: server.ChromeKontrolServer, bool_ts: bool
) -> None:
    """Python's bool is a subclass of int, so `isinstance(True, int)` is True and `True > 0`
    is True. Without an explicit `isinstance(value, bool)` guard, a stray boolean would be
    silently accepted as ts=1 (True) or rejected only via the >0 check (False) — the True
    case is the dangerous one, since it would record a bogus but "valid-looking" timestamp.
    """
    await kontrol._handle_message(json.dumps({"type": "focus", "ts": bool_ts}), "chrome")
    assert kontrol._focus_ts == {}


async def test_focus_notification_updates_existing_entry_for_same_key(
    kontrol: server.ChromeKontrolServer,
) -> None:
    kontrol._focus_ts["chrome"] = 1000
    await kontrol._handle_message(json.dumps({"type": "focus", "ts": 2000}), "chrome")
    assert kontrol._focus_ts == {"chrome": 2000}


async def test_focus_notification_with_invalid_ts_leaves_existing_entry_untouched(
    kontrol: server.ChromeKontrolServer,
) -> None:
    """An invalid ts must not clear a prior valid recording — it is simply ignored."""
    kontrol._focus_ts["chrome"] = 1000
    await kontrol._handle_message(json.dumps({"type": "focus", "ts": -5}), "chrome")
    assert kontrol._focus_ts == {"chrome": 1000}


async def test_focus_notification_only_updates_the_sending_clients_key(
    kontrol: server.ChromeKontrolServer,
) -> None:
    """Two clients' focus records must not clobber each other."""
    await kontrol._handle_message(json.dumps({"type": "focus", "ts": 100}), "chrome:a")
    await kontrol._handle_message(json.dumps({"type": "focus", "ts": 200}), "chrome:b")
    assert kontrol._focus_ts == {"chrome:a": 100, "chrome:b": 200}


async def test_focus_notification_does_not_touch_pending_response(
    kontrol: server.ChromeKontrolServer,
) -> None:
    """Core correctness property: a focus notification is a one-way message, not a command
    response. It must never set _pending_response or _response_event, since a concurrent
    send_command() call waiting on those would otherwise mistake this notification for its
    own response.
    """
    kontrol._pending_response = {"result": "ok", "data": "unrelated-in-flight-response"}
    await kontrol._handle_message(json.dumps({"type": "focus", "ts": 42}), "chrome")
    assert kontrol._pending_response == {"result": "ok", "data": "unrelated-in-flight-response"}
    assert kontrol._response_event.is_set() is False


async def test_focus_notification_with_invalid_ts_also_does_not_touch_pending_response(
    kontrol: server.ChromeKontrolServer,
) -> None:
    kontrol._pending_response = {"result": "ok", "data": "unrelated-in-flight-response"}
    await kontrol._handle_message(json.dumps({"type": "focus", "ts": -1}), "chrome")
    assert kontrol._pending_response == {"result": "ok", "data": "unrelated-in-flight-response"}
    assert kontrol._response_event.is_set() is False
