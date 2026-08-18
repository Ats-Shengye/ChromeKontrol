"""
Location   : ChromeKontrol/tests/test_is_allowed_origin.py
Purpose    : Unit tests for server._is_allowed_origin().
Why        : This is the WebSocket handshake's sole defense against non-local
             Origins (SPEC.md "ネットワーク分離"). Every branch — missing
             Origin, the "null" Origin used by file:// pages, extension
             origins, the localhost allowlist, and case-insensitivity in
             both directions — must be pinned down individually, since a
             regression here directly reopens the CSRF/DNS-rebinding attack
             surface this function exists to close.
Related    : server.py
"""

from __future__ import annotations

import server


def test_is_allowed_origin_accepts_missing_origin_header() -> None:
    """No Origin header at all (e.g. wscat, other non-browser CLI clients) is allowed."""
    assert server._is_allowed_origin({}) is True


def test_is_allowed_origin_accepts_empty_string_origin() -> None:
    """An explicitly empty Origin value is treated the same as a missing header."""
    assert server._is_allowed_origin({"Origin": ""}) is True


def test_is_allowed_origin_rejects_null_origin() -> None:
    """The 'null' Origin sent by file:// pages and sandboxed iframes is explicitly denied."""
    assert server._is_allowed_origin({"Origin": "null"}) is False


def test_is_allowed_origin_rejects_null_origin_case_insensitively() -> None:
    """'null' rejection must not be bypassable via case variation."""
    assert server._is_allowed_origin({"Origin": "NULL"}) is False
    assert server._is_allowed_origin({"Origin": "Null"}) is False


def test_is_allowed_origin_accepts_chrome_extension_origin() -> None:
    assert server._is_allowed_origin({"Origin": "chrome-extension://eeheoiffbkbadfpaaobmcajgigdhmeeg"}) is True


def test_is_allowed_origin_accepts_chrome_extension_origin_case_insensitively() -> None:
    assert server._is_allowed_origin({"Origin": "CHROME-EXTENSION://ABCDEFG"}) is True


# ---------------------------------------------------------------------------
# Phase F2: 'moz-extension://' (Firefox extension origins).
# ---------------------------------------------------------------------------


def test_is_allowed_origin_accepts_moz_extension_origin() -> None:
    assert server._is_allowed_origin({"Origin": "moz-extension://12345678-1234-1234-1234-123456789abc"}) is True


def test_is_allowed_origin_accepts_moz_extension_origin_without_uuid() -> None:
    """Prefix match accepts a bare "moz-extension://" with nothing after it, mirroring the
    existing chrome-extension:// behavior: this function does not validate UUID shape, only
    the scheme prefix (Firefox's per-install UUID cannot be known in advance -- see
    _is_allowed_origin()'s comment for why prefix matching is the only option here).
    """
    assert server._is_allowed_origin({"Origin": "moz-extension://"}) is True


def test_is_allowed_origin_accepts_moz_extension_origin_case_insensitively() -> None:
    assert server._is_allowed_origin({"Origin": "MOZ-EXTENSION://12345678-1234-1234-1234-123456789abc"}) is True


def test_is_allowed_origin_rejects_moz_extension_substring_not_at_start() -> None:
    """Guards against a spoofed Origin embedding the moz-extension:// string mid-value;
    startswith() must anchor the match at the beginning, not just look for a substring.
    """
    assert server._is_allowed_origin({"Origin": "https://evil.example.com/moz-extension://"}) is False


def test_is_allowed_origin_rejects_moz_extension_without_scheme_separator() -> None:
    assert server._is_allowed_origin({"Origin": "moz-extension"}) is False


def test_is_allowed_origin_accepts_localhost_allowlist_entries() -> None:
    for origin in server.ALLOWED_ORIGINS:
        assert server._is_allowed_origin({"Origin": origin}) is True


def test_is_allowed_origin_accepts_allowlisted_origin_case_insensitively() -> None:
    assert server._is_allowed_origin({"Origin": "WS://LOCALHOST"}) is True
    assert server._is_allowed_origin({"Origin": "Ws://127.0.0.1"}) is True


def test_is_allowed_origin_rejects_arbitrary_external_origin() -> None:
    assert server._is_allowed_origin({"Origin": "http://evil.example.com"}) is False


def test_is_allowed_origin_rejects_similar_looking_non_local_origin() -> None:
    """Guards against substring-style bypasses (e.g. 'ws://127.0.0.1.evil.com')."""
    assert server._is_allowed_origin({"Origin": "ws://127.0.0.1.evil.com"}) is False


def test_is_allowed_origin_rejects_wildcard_bind_origin() -> None:
    """ws://0.0.0.0 is explicitly excluded from the allowlist per SPEC.md."""
    assert server._is_allowed_origin({"Origin": "ws://0.0.0.0"}) is False
