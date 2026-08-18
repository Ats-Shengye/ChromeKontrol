"""
Location   : ChromeKontrol/tests/test_receive_identify.py
Purpose    : Unit tests for ChromeKontrolServer._receive_identify().
Why        : This is the gatekeeper for the very first message on every new
             WebSocket connection. Every rejection branch (timeout, oversized
             payload, malformed JSON, malformed identify shape, malformed or
             disallowed browser name, and — since ISSUES.md P0-1 (Phase 2a) —
             malformed profileId/email/label) must close the socket with the
             documented reason and return None; the single success path must
             return a ClientInfo carrying exactly what was validated, with no
             close() call at all.
Related    : server.py, ISSUES.md P0-1
"""

from __future__ import annotations

import json
import unicodedata

import pytest
import websockets.exceptions

import server
from tests.conftest import FakeWebSocket, as_ws_protocol


@pytest.fixture
def kontrol() -> server.ChromeKontrolServer:
    return server.ChromeKontrolServer()


async def test_receive_identify_accepts_type_and_browser_form(kontrol: server.ChromeKontrolServer) -> None:
    """The documented `{"type": "identify", "browser": "..."}` form is accepted."""
    ws = FakeWebSocket()
    ws.feed(json.dumps({"type": "identify", "browser": "chrome"}))
    result = await kontrol._receive_identify(as_ws_protocol(ws))
    assert result == server.ClientInfo(browser="chrome", websocket=as_ws_protocol(ws))
    assert ws.close_calls == []


async def test_receive_identify_accepts_bare_browser_form(kontrol: server.ChromeKontrolServer) -> None:
    """Backward-compat form `{"browser": "..."}` without a "type" field is also accepted."""
    ws = FakeWebSocket()
    ws.feed(json.dumps({"browser": "edge"}))
    result = await kontrol._receive_identify(as_ws_protocol(ws))
    assert result == server.ClientInfo(browser="edge", websocket=as_ws_protocol(ws))
    assert ws.close_calls == []


async def test_receive_identify_times_out_when_no_message_arrives(
    kontrol: server.ChromeKontrolServer,
) -> None:
    """No message ever arrives; the wait must not block indefinitely.

    _IDENTIFY_TIMEOUT is shadowed on the instance with a short value so this
    test exercises the real asyncio.wait_for cancellation path without
    waiting out the production default of 3 seconds.
    """
    kontrol._IDENTIFY_TIMEOUT = 0.05
    ws = FakeWebSocket()  # queue stays empty: recv() blocks until cancelled by wait_for
    result = await kontrol._receive_identify(as_ws_protocol(ws))
    assert result is None
    assert ws.close_calls == [(1008, "Identify timeout")]


async def test_receive_identify_rejects_connection_already_closed(
    kontrol: server.ChromeKontrolServer,
) -> None:
    """If recv() raises ConnectionClosed, the socket is already gone; no close() call is made."""
    ws = FakeWebSocket()
    ws.feed(websockets.exceptions.ConnectionClosedOK(None, None))
    result = await kontrol._receive_identify(as_ws_protocol(ws))
    assert result is None
    assert ws.close_calls == []


async def test_receive_identify_rejects_oversized_message(
    kontrol: server.ChromeKontrolServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Message size guard mirrors _handle_message's; a small limit avoids allocating 5 MiB in a test."""
    monkeypatch.setattr(server, "MAX_MESSAGE_BYTES", 16)
    ws = FakeWebSocket()
    ws.feed(json.dumps({"type": "identify", "browser": "chrome-way-too-long-to-fit"}))
    result = await kontrol._receive_identify(as_ws_protocol(ws))
    assert result is None
    assert ws.close_calls == [(1008, "Message too large")]


async def test_receive_identify_rejects_non_json_message(kontrol: server.ChromeKontrolServer) -> None:
    ws = FakeWebSocket()
    ws.feed("not json at all")
    result = await kontrol._receive_identify(as_ws_protocol(ws))
    assert result is None
    assert ws.close_calls == [(1008, "Invalid JSON in identify")]


async def test_receive_identify_rejects_json_array(kontrol: server.ChromeKontrolServer) -> None:
    """Valid JSON that is not a JSON object (e.g. an array) is rejected as the wrong shape."""
    ws = FakeWebSocket()
    ws.feed(json.dumps(["chrome"]))
    result = await kontrol._receive_identify(as_ws_protocol(ws))
    assert result is None
    assert ws.close_calls == [(1008, "Invalid identify format")]


async def test_receive_identify_rejects_missing_browser_field(kontrol: server.ChromeKontrolServer) -> None:
    ws = FakeWebSocket()
    ws.feed(json.dumps({"type": "identify"}))
    result = await kontrol._receive_identify(as_ws_protocol(ws))
    assert result is None
    assert ws.close_calls == [(1008, "Missing browser field in identify")]


async def test_receive_identify_rejects_non_string_browser_field(kontrol: server.ChromeKontrolServer) -> None:
    ws = FakeWebSocket()
    ws.feed(json.dumps({"browser": 42}))
    result = await kontrol._receive_identify(as_ws_protocol(ws))
    assert result is None
    assert ws.close_calls == [(1008, "Missing browser field in identify")]


async def test_receive_identify_rejects_empty_string_browser_field(
    kontrol: server.ChromeKontrolServer,
) -> None:
    ws = FakeWebSocket()
    ws.feed(json.dumps({"browser": ""}))
    result = await kontrol._receive_identify(as_ws_protocol(ws))
    assert result is None
    assert ws.close_calls == [(1008, "Missing browser field in identify")]


async def test_receive_identify_rejects_non_ascii_browser_field(kontrol: server.ChromeKontrolServer) -> None:
    ws = FakeWebSocket()
    ws.feed(json.dumps({"browser": "chrôme"}))
    result = await kontrol._receive_identify(as_ws_protocol(ws))
    assert result is None
    assert ws.close_calls == [(1008, "Invalid browser field value")]


async def test_receive_identify_rejects_non_printable_browser_field(
    kontrol: server.ChromeKontrolServer,
) -> None:
    ws = FakeWebSocket()
    ws.feed(json.dumps({"browser": "chrome\x07"}))
    result = await kontrol._receive_identify(as_ws_protocol(ws))
    assert result is None
    assert ws.close_calls == [(1008, "Invalid browser field value")]


async def test_receive_identify_rejects_browser_field_over_64_chars(
    kontrol: server.ChromeKontrolServer,
) -> None:
    ws = FakeWebSocket()
    ws.feed(json.dumps({"browser": "a" * 65}))
    result = await kontrol._receive_identify(as_ws_protocol(ws))
    assert result is None
    assert ws.close_calls == [(1008, "Invalid browser field value")]


async def test_receive_identify_accepts_browser_field_at_exactly_64_chars(
    kontrol: server.ChromeKontrolServer,
) -> None:
    """Boundary condition: exactly 64 ASCII printable characters is allowed through this gate.

    (It is still rejected downstream by the allowlist check, since "a"*64 is
    not in ALLOWED_BROWSERS — this test isolates the length/charset gate only.)
    """
    ws = FakeWebSocket()
    ws.feed(json.dumps({"browser": "a" * 64}))
    result = await kontrol._receive_identify(as_ws_protocol(ws))
    assert result is None
    assert ws.close_calls == [(1008, "Unknown browser")]


async def test_receive_identify_rejects_browser_not_in_allowlist(
    kontrol: server.ChromeKontrolServer,
) -> None:
    """ "safari" (not "firefox") is used here: firefox joined ALLOWED_BROWSERS in Phase F2
    (see test_receive_identify_accepts_firefox_browser below), so it no longer exercises
    this rejection branch. "safari" stays permanently outside the allowlist.
    """
    ws = FakeWebSocket()
    ws.feed(json.dumps({"browser": "safari"}))
    result = await kontrol._receive_identify(as_ws_protocol(ws))
    assert result is None
    assert ws.close_calls == [(1008, "Unknown browser")]


# ---------------------------------------------------------------------------
# Phase F2: 'firefox' joins ALLOWED_BROWSERS (server-side only; the Firefox
# extension itself does not exist until Phase F3, so no real client ever
# sends this in production yet -- these tests pin the server-side gate).
# ---------------------------------------------------------------------------


async def test_receive_identify_accepts_firefox_browser(kontrol: server.ChromeKontrolServer) -> None:
    ws = FakeWebSocket()
    ws.feed(json.dumps({"type": "identify", "browser": "firefox"}))
    result = await kontrol._receive_identify(as_ws_protocol(ws))
    assert result == server.ClientInfo(browser="firefox", websocket=as_ws_protocol(ws))
    assert ws.close_calls == []


async def test_receive_identify_firefox_client_key_includes_profile_id(
    kontrol: server.ChromeKontrolServer,
) -> None:
    """The "browser:profileId" keying scheme (ClientInfo.key) is browser-name-agnostic;
    this pins that firefox composes the same way chrome/edge already do.
    """
    ws = FakeWebSocket()
    ws.feed(json.dumps({"browser": "firefox", "profileId": "a3f2c1d8"}))
    result = await kontrol._receive_identify(as_ws_protocol(ws))
    assert result is not None
    assert result.key == "firefox:a3f2c1d8"


# ---------------------------------------------------------------------------
# ISSUES.md P0-1 (Phase 2a): optional profileId / email / label fields.
# ---------------------------------------------------------------------------


async def test_receive_identify_accepts_all_optional_fields_present(
    kontrol: server.ChromeKontrolServer,
) -> None:
    ws = FakeWebSocket()
    ws.feed(
        json.dumps(
            {
                "type": "identify",
                "browser": "chrome",
                "profileId": "a3f2c1d8",
                "email": "user@example.com",
                "label": "メイン",
            }
        )
    )
    result = await kontrol._receive_identify(as_ws_protocol(ws))
    assert result == server.ClientInfo(
        browser="chrome",
        websocket=as_ws_protocol(ws),
        profile_id="a3f2c1d8",
        email="user@example.com",
        label="メイン",
    )
    assert ws.close_calls == []


async def test_receive_identify_accepts_profile_id_without_email_or_label(
    kontrol: server.ChromeKontrolServer,
) -> None:
    """The three optional fields are independent; profileId alone must be enough."""
    ws = FakeWebSocket()
    ws.feed(json.dumps({"browser": "chrome", "profileId": "a3f2c1d8"}))
    result = await kontrol._receive_identify(as_ws_protocol(ws))
    assert result == server.ClientInfo(browser="chrome", websocket=as_ws_protocol(ws), profile_id="a3f2c1d8")
    assert ws.close_calls == []


async def test_receive_identify_rejects_profile_id_containing_colon(
    kontrol: server.ChromeKontrolServer,
) -> None:
    """ ":" is the key separator (browser:profileId); a profileId containing one would corrupt it."""
    ws = FakeWebSocket()
    ws.feed(json.dumps({"browser": "chrome", "profileId": "a3f2:c1d8"}))
    result = await kontrol._receive_identify(as_ws_protocol(ws))
    assert result is None
    assert ws.close_calls == [(1008, "Invalid profileId field value")]


async def test_receive_identify_rejects_non_string_profile_id(kontrol: server.ChromeKontrolServer) -> None:
    ws = FakeWebSocket()
    ws.feed(json.dumps({"browser": "chrome", "profileId": 12345}))
    result = await kontrol._receive_identify(as_ws_protocol(ws))
    assert result is None
    assert ws.close_calls == [(1008, "Invalid profileId field value")]


async def test_receive_identify_rejects_empty_string_profile_id(kontrol: server.ChromeKontrolServer) -> None:
    ws = FakeWebSocket()
    ws.feed(json.dumps({"browser": "chrome", "profileId": ""}))
    result = await kontrol._receive_identify(as_ws_protocol(ws))
    assert result is None
    assert ws.close_calls == [(1008, "Invalid profileId field value")]


async def test_receive_identify_rejects_non_ascii_profile_id(kontrol: server.ChromeKontrolServer) -> None:
    """profileId is spec'd as ASCII printable only (unlike label, which allows Japanese)."""
    ws = FakeWebSocket()
    ws.feed(json.dumps({"browser": "chrome", "profileId": "プロファイル"}))
    result = await kontrol._receive_identify(as_ws_protocol(ws))
    assert result is None
    assert ws.close_calls == [(1008, "Invalid profileId field value")]


async def test_receive_identify_rejects_non_printable_profile_id(kontrol: server.ChromeKontrolServer) -> None:
    ws = FakeWebSocket()
    ws.feed(json.dumps({"browser": "chrome", "profileId": "a3f2\x07c1d8"}))
    result = await kontrol._receive_identify(as_ws_protocol(ws))
    assert result is None
    assert ws.close_calls == [(1008, "Invalid profileId field value")]


async def test_receive_identify_rejects_profile_id_over_64_chars(kontrol: server.ChromeKontrolServer) -> None:
    ws = FakeWebSocket()
    ws.feed(json.dumps({"browser": "chrome", "profileId": "a" * 65}))
    result = await kontrol._receive_identify(as_ws_protocol(ws))
    assert result is None
    assert ws.close_calls == [(1008, "Invalid profileId field value")]


async def test_receive_identify_accepts_profile_id_at_exactly_64_chars(
    kontrol: server.ChromeKontrolServer,
) -> None:
    ws = FakeWebSocket()
    profile_id = "a" * 64
    ws.feed(json.dumps({"browser": "chrome", "profileId": profile_id}))
    result = await kontrol._receive_identify(as_ws_protocol(ws))
    assert result == server.ClientInfo(browser="chrome", websocket=as_ws_protocol(ws), profile_id=profile_id)
    assert ws.close_calls == []


async def test_receive_identify_rejects_non_string_email(kontrol: server.ChromeKontrolServer) -> None:
    ws = FakeWebSocket()
    ws.feed(json.dumps({"browser": "chrome", "email": 42}))
    result = await kontrol._receive_identify(as_ws_protocol(ws))
    assert result is None
    assert ws.close_calls == [(1008, "Invalid email field value")]


async def test_receive_identify_rejects_empty_string_email(kontrol: server.ChromeKontrolServer) -> None:
    ws = FakeWebSocket()
    ws.feed(json.dumps({"browser": "chrome", "email": ""}))
    result = await kontrol._receive_identify(as_ws_protocol(ws))
    assert result is None
    assert ws.close_calls == [(1008, "Invalid email field value")]


async def test_receive_identify_rejects_non_ascii_email(kontrol: server.ChromeKontrolServer) -> None:
    ws = FakeWebSocket()
    ws.feed(json.dumps({"browser": "chrome", "email": "ユーザー@example.com"}))
    result = await kontrol._receive_identify(as_ws_protocol(ws))
    assert result is None
    assert ws.close_calls == [(1008, "Invalid email field value")]


async def test_receive_identify_rejects_email_over_254_chars(kontrol: server.ChromeKontrolServer) -> None:
    ws = FakeWebSocket()
    too_long_email = ("a" * 250) + "@example.com"  # well over 254 chars
    ws.feed(json.dumps({"browser": "chrome", "email": too_long_email}))
    result = await kontrol._receive_identify(as_ws_protocol(ws))
    assert result is None
    assert ws.close_calls == [(1008, "Invalid email field value")]


async def test_receive_identify_accepts_email_at_exactly_254_chars(
    kontrol: server.ChromeKontrolServer,
) -> None:
    ws = FakeWebSocket()
    email = ("a" * 242) + "@example.com"  # exactly 254 chars
    assert len(email) == 254
    ws.feed(json.dumps({"browser": "chrome", "email": email}))
    result = await kontrol._receive_identify(as_ws_protocol(ws))
    assert result == server.ClientInfo(browser="chrome", websocket=as_ws_protocol(ws), email=email)
    assert ws.close_calls == []


async def test_receive_identify_rejects_non_string_label(kontrol: server.ChromeKontrolServer) -> None:
    ws = FakeWebSocket()
    ws.feed(json.dumps({"browser": "chrome", "label": 42}))
    result = await kontrol._receive_identify(as_ws_protocol(ws))
    assert result is None
    assert ws.close_calls == [(1008, "Invalid label field value")]


async def test_receive_identify_rejects_empty_string_label(kontrol: server.ChromeKontrolServer) -> None:
    ws = FakeWebSocket()
    ws.feed(json.dumps({"browser": "chrome", "label": ""}))
    result = await kontrol._receive_identify(as_ws_protocol(ws))
    assert result is None
    assert ws.close_calls == [(1008, "Invalid label field value")]


async def test_receive_identify_accepts_non_ascii_label(kontrol: server.ChromeKontrolServer) -> None:
    """Unlike profileId/email, label must allow non-ASCII (Japanese labels are the whole point)."""
    ws = FakeWebSocket()
    ws.feed(json.dumps({"browser": "chrome", "label": "メイン"}))
    result = await kontrol._receive_identify(as_ws_protocol(ws))
    assert result == server.ClientInfo(browser="chrome", websocket=as_ws_protocol(ws), label="メイン")
    assert ws.close_calls == []


async def test_receive_identify_rejects_non_printable_label(kontrol: server.ChromeKontrolServer) -> None:
    ws = FakeWebSocket()
    ws.feed(json.dumps({"browser": "chrome", "label": "メイン\x07"}))
    result = await kontrol._receive_identify(as_ws_protocol(ws))
    assert result is None
    assert ws.close_calls == [(1008, "Invalid label field value")]


async def test_receive_identify_rejects_label_containing_line_separator(
    kontrol: server.ChromeKontrolServer,
) -> None:
    """U+2028 LINE SEPARATOR is in Unicode category Zl, which str.isprintable() already
    treats as non-printable — defense in depth against the log/JSON-line-injection class of
    issue ISSUES.md P2-7 documents for _sanitise_for_log (a separate, still-open defect on a
    different code path; this test only confirms this new input boundary is not vulnerable
    to the same category of value).
    """
    ws = FakeWebSocket()
    ws.feed(json.dumps({"browser": "chrome", "label": "before after"}))
    result = await kontrol._receive_identify(as_ws_protocol(ws))
    assert result is None
    assert ws.close_calls == [(1008, "Invalid label field value")]


async def test_receive_identify_rejects_label_over_64_chars(kontrol: server.ChromeKontrolServer) -> None:
    ws = FakeWebSocket()
    ws.feed(json.dumps({"browser": "chrome", "label": "あ" * 65}))
    result = await kontrol._receive_identify(as_ws_protocol(ws))
    assert result is None
    assert ws.close_calls == [(1008, "Invalid label field value")]


async def test_receive_identify_accepts_label_at_exactly_64_chars(
    kontrol: server.ChromeKontrolServer,
) -> None:
    ws = FakeWebSocket()
    label = "あ" * 64
    ws.feed(json.dumps({"browser": "chrome", "label": label}))
    result = await kontrol._receive_identify(as_ws_protocol(ws))
    assert result == server.ClientInfo(browser="chrome", websocket=as_ws_protocol(ws), label=label)
    assert ws.close_calls == []


# ---------------------------------------------------------------------------
# Security-Audit.md L-3 (Phase 2b): whitespace-only profileId/email/label rejected,
# intentional surrounding whitespace preserved.
# ---------------------------------------------------------------------------


async def test_receive_identify_rejects_whitespace_only_label(kontrol: server.ChromeKontrolServer) -> None:
    ws = FakeWebSocket()
    ws.feed(json.dumps({"browser": "chrome", "label": "   "}))
    result = await kontrol._receive_identify(as_ws_protocol(ws))
    assert result is None
    assert ws.close_calls == [(1008, "Invalid label field value")]


async def test_receive_identify_rejects_whitespace_only_profile_id(kontrol: server.ChromeKontrolServer) -> None:
    ws = FakeWebSocket()
    ws.feed(json.dumps({"browser": "chrome", "profileId": "   "}))
    result = await kontrol._receive_identify(as_ws_protocol(ws))
    assert result is None
    assert ws.close_calls == [(1008, "Invalid profileId field value")]


async def test_receive_identify_rejects_whitespace_only_email(kontrol: server.ChromeKontrolServer) -> None:
    ws = FakeWebSocket()
    ws.feed(json.dumps({"browser": "chrome", "email": "   "}))
    result = await kontrol._receive_identify(as_ws_protocol(ws))
    assert result is None
    assert ws.close_calls == [(1008, "Invalid email field value")]


async def test_receive_identify_preserves_intentional_surrounding_whitespace_in_label(
    kontrol: server.ChromeKontrolServer,
) -> None:
    """L-3's fix must only *reject* whitespace-only values; it must not strip() a value
    that has real content plus intentional surrounding whitespace.
    """
    ws = FakeWebSocket()
    label = " メイン "
    ws.feed(json.dumps({"browser": "chrome", "label": label}))
    result = await kontrol._receive_identify(as_ws_protocol(ws))
    assert result is not None
    assert result.label == label
    assert ws.close_calls == []


# ---------------------------------------------------------------------------
# Security-Audit.md L-4 (Phase 2b): label is NFC-normalized on receipt.
# ---------------------------------------------------------------------------


async def test_receive_identify_normalises_label_to_nfc(kontrol: server.ChromeKontrolServer) -> None:
    """An NFD (decomposed) label must be stored as its NFC-normalized form, so that a
    later alias resolution comparing labels never has to worry about NFC/NFD mismatches
    (spec section 3-4).
    """
    nfd_label = unicodedata.normalize("NFD", "が")  # decomposed: か + combining U+3099
    assert nfd_label != unicodedata.normalize("NFC", nfd_label)  # sanity: they really differ

    ws = FakeWebSocket()
    ws.feed(json.dumps({"browser": "chrome", "label": nfd_label}))
    result = await kontrol._receive_identify(as_ws_protocol(ws))

    assert result is not None
    assert result.label == unicodedata.normalize("NFC", nfd_label)
    assert ws.close_calls == []


# ---------------------------------------------------------------------------
# email format validation (Phase 2b): "@" exactly once, non-empty local/domain parts.
# ---------------------------------------------------------------------------


async def test_receive_identify_rejects_email_without_at_sign(kontrol: server.ChromeKontrolServer) -> None:
    ws = FakeWebSocket()
    ws.feed(json.dumps({"browser": "chrome", "email": "userexample.com"}))
    result = await kontrol._receive_identify(as_ws_protocol(ws))
    assert result is None
    assert ws.close_calls == [(1008, "Invalid email field value")]


async def test_receive_identify_rejects_email_with_two_at_signs(kontrol: server.ChromeKontrolServer) -> None:
    ws = FakeWebSocket()
    ws.feed(json.dumps({"browser": "chrome", "email": "user@sub@example.com"}))
    result = await kontrol._receive_identify(as_ws_protocol(ws))
    assert result is None
    assert ws.close_calls == [(1008, "Invalid email field value")]


async def test_receive_identify_rejects_email_with_empty_local_part(kontrol: server.ChromeKontrolServer) -> None:
    ws = FakeWebSocket()
    ws.feed(json.dumps({"browser": "chrome", "email": "@example.com"}))
    result = await kontrol._receive_identify(as_ws_protocol(ws))
    assert result is None
    assert ws.close_calls == [(1008, "Invalid email field value")]


async def test_receive_identify_rejects_email_with_empty_domain_part(kontrol: server.ChromeKontrolServer) -> None:
    ws = FakeWebSocket()
    ws.feed(json.dumps({"browser": "chrome", "email": "user@"}))
    result = await kontrol._receive_identify(as_ws_protocol(ws))
    assert result is None
    assert ws.close_calls == [(1008, "Invalid email field value")]


async def test_receive_identify_accepts_well_formed_email(kontrol: server.ChromeKontrolServer) -> None:
    ws = FakeWebSocket()
    ws.feed(json.dumps({"browser": "chrome", "email": "work@example.com"}))
    result = await kontrol._receive_identify(as_ws_protocol(ws))
    assert result == server.ClientInfo(browser="chrome", websocket=as_ws_protocol(ws), email="work@example.com")
    assert ws.close_calls == []


# ---------------------------------------------------------------------------
# ISSUES.md P1-5 (Phase F7): optional "focusTs" field.
#
# Unlike profileId/email/label, an invalid focusTs must NOT reject the whole
# identify — it is silently dropped (ClientInfo.focus_ts stays None) while
# every other valid field still goes through, matching the existing
# "carry on with whatever validated" policy for the optional fields.
# ---------------------------------------------------------------------------


async def test_receive_identify_accepts_valid_focus_ts(kontrol: server.ChromeKontrolServer) -> None:
    ws = FakeWebSocket()
    ws.feed(json.dumps({"browser": "chrome", "focusTs": 1234567890}))
    result = await kontrol._receive_identify(as_ws_protocol(ws))
    assert result == server.ClientInfo(browser="chrome", websocket=as_ws_protocol(ws), focus_ts=1234567890)
    assert ws.close_calls == []


async def test_receive_identify_truncates_float_focus_ts_to_int(kontrol: server.ChromeKontrolServer) -> None:
    ws = FakeWebSocket()
    ws.feed(json.dumps({"browser": "chrome", "focusTs": 1234567890.9}))
    result = await kontrol._receive_identify(as_ws_protocol(ws))
    assert result is not None
    assert result.focus_ts == 1234567890


async def test_receive_identify_absent_focus_ts_defaults_to_none(kontrol: server.ChromeKontrolServer) -> None:
    ws = FakeWebSocket()
    ws.feed(json.dumps({"browser": "chrome"}))
    result = await kontrol._receive_identify(as_ws_protocol(ws))
    assert result is not None
    assert result.focus_ts is None


@pytest.mark.parametrize(
    "bad_focus_ts",
    [-1, 0, "1234567890", None, [1234567890], True, False, float("inf"), float("-inf"), float("nan")],
    ids=[
        "negative",
        "zero",
        "string",
        "null",
        "list",
        "true",
        "false",
        "infinity",
        "negative-infinity",
        "nan",
    ],
)
async def test_receive_identify_invalid_focus_ts_does_not_reject_connection(
    kontrol: server.ChromeKontrolServer, bad_focus_ts: object
) -> None:
    """An invalid focusTs is ignored (identify still succeeds with focus_ts=None), unlike
    profileId/email/label whose invalid values close the connection outright.

    Security-Audit.md M-1: `float('inf')` (from a JSON `1e999` literal) previously passed
    `_is_valid_positive_timestamp()` because `inf > 0` is True, and would then blow up
    `int(inf)` with OverflowError during identify. `-inf` and `nan` were already rejected
    by the pre-existing `value > 0` check, but are included here too so a future change to
    that condition cannot silently regress them.
    """
    ws = FakeWebSocket()
    ws.feed(json.dumps({"browser": "chrome", "focusTs": bad_focus_ts}))
    result = await kontrol._receive_identify(as_ws_protocol(ws))
    assert result is not None
    assert result.focus_ts is None
    assert ws.close_calls == []


async def test_receive_identify_accepts_focus_ts_alongside_other_optional_fields(
    kontrol: server.ChromeKontrolServer,
) -> None:
    ws = FakeWebSocket()
    ws.feed(
        json.dumps(
            {
                "type": "identify",
                "browser": "chrome",
                "profileId": "a3f2c1d8",
                "email": "user@example.com",
                "label": "メイン",
                "focusTs": 555,
            }
        )
    )
    result = await kontrol._receive_identify(as_ws_protocol(ws))
    assert result == server.ClientInfo(
        browser="chrome",
        websocket=as_ws_protocol(ws),
        profile_id="a3f2c1d8",
        email="user@example.com",
        label="メイン",
        focus_ts=555,
    )
    assert ws.close_calls == []
