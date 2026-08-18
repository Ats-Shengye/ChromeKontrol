"""
Location   : ChromeKontrol/tests/test_sanitise_for_log.py
Purpose    : Unit tests for server._sanitise_for_log().
Why        : This function is the sole log-injection defense in the codebase
             (server.py:119-141). Every code path that logs attacker-influenced
             data routes through it, so its Unicode-category filtering must be
             pinned down precisely: which categories are stripped, and that
             plain values pass through unchanged. See
             test_sanitise_for_log_strips_tab_despite_apparent_special_casing
             below for a discrepancy this suite surfaced between the code's
             apparent intent (preserve tab) and its actual behavior (tab is
             stripped, same as any other Cc character).
Related    : server.py
"""

from __future__ import annotations

import server


def test_sanitise_for_log_plain_ascii_passes_through_unchanged() -> None:
    """A string with no control/format characters is returned unmodified."""
    assert server._sanitise_for_log("chrome") == "chrome"


def test_sanitise_for_log_removes_newline_and_carriage_return() -> None:
    """Cc (control) characters, incl. the classic log-injection newline, are stripped."""
    assert server._sanitise_for_log("line1\nline2\rline3") == "line1line2line3"


def test_sanitise_for_log_strips_tab_despite_apparent_special_casing() -> None:
    """Documents actual (surprising) current behavior: tab is stripped, not kept.

    The implementation's `(ch >= ' ' or ch == '\\t')` clause reads as if it
    were carving out an exception for tab, but tab's Unicode general
    category is 'Cc' (control) — the same category the second `and` clause
    filters out. Since both clauses are ANDed together, the `ch == '\\t'`
    branch can never actually preserve a tab in practice: nothing else in
    the alphabet is both "< ' ' and not in Cf/Cc/Cs", so this special case
    is effectively dead code. This is a latent inconsistency between the
    code's apparent intent and its actual behavior, distinct from the three
    defects ISSUES.md tracks (P1-1/P1-3/P1-4) — flagged to the team
    separately rather than xfail'd here, since Phase 0's job is to freeze
    *current* behavior, and no fix direction has been decided for it yet.
    """
    assert server._sanitise_for_log("a\tb") == "ab"


def test_sanitise_for_log_removes_escape_character() -> None:
    """ESC (U+001B), used for terminal escape sequence injection, is a Cc control char."""
    assert server._sanitise_for_log("before\x1b[31mafter") == "before[31mafter"


def test_sanitise_for_log_removes_right_to_left_override() -> None:
    """U+202E RIGHT-TO-LEFT OVERRIDE (category Cf) can visually reorder log text.

    Uses the \\uXXXX escape (rather than embedding the raw character) so the
    source file itself stays plainly readable and does not carry an
    invisible bidi-override control character in version control.
    """
    assert server._sanitise_for_log("safe\u202eevil") == "safeevil"


def test_sanitise_for_log_removes_byte_order_mark() -> None:
    """U+FEFF ZERO WIDTH NO-BREAK SPACE / BOM (category Cf) is stripped."""
    assert server._sanitise_for_log("safe\ufeffvalue") == "safevalue"


def test_sanitise_for_log_removes_line_separator() -> None:
    """U+2028 LINE SEPARATOR (category 'Zl') is stripped (ISSUES.md P2-7 / Security-Audit.md M-1).

    ECMAScript and RFC 7159 treat U+2028 as a line terminator, so letting it
    pass through allowed forging fake log entries in JSON-transport log
    pipelines (CWE-117). Previously this test pinned the *buggy* passthrough
    behavior as a guard against silent regression; now that the category
    filter includes 'Zl'/'Zp', it pins the fixed (stripping) behavior instead.
    """
    assert server._sanitise_for_log("before\u2028after") == "beforeafter"


def test_sanitise_for_log_removes_paragraph_separator() -> None:
    """U+2029 PARAGRAPH SEPARATOR (category 'Zp') is stripped for the same reason as U+2028."""
    assert server._sanitise_for_log("before\u2029after") == "beforeafter"


def test_sanitise_for_log_removes_lone_surrogate() -> None:
    """Cs (surrogate) code points, which raise UnicodeEncodeError if logged raw, are stripped."""
    lone_surrogate = "before\ud800after"
    result = server._sanitise_for_log(lone_surrogate)
    assert result == "beforeafter"
    # Sanity check: the sanitised output must be safely encodable, unlike the input.
    result.encode("utf-8")


def test_sanitise_for_log_converts_non_string_input_via_str() -> None:
    """Non-string inputs (e.g. None, int) are stringified before filtering."""
    assert server._sanitise_for_log(None) == "None"
    assert server._sanitise_for_log(42) == "42"


def test_sanitise_for_log_converts_exception_via_str() -> None:
    """Exceptions (a common argument at call sites like `_sanitise_for_log(exc)`) stringify."""
    assert server._sanitise_for_log(ValueError("bad value")) == "bad value"


def test_sanitise_for_log_handles_empty_string() -> None:
    """Empty input returns empty output (no crash on the boundary case)."""
    assert server._sanitise_for_log("") == ""
