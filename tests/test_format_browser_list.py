"""
Location   : ChromeKontrol/tests/test_format_browser_list.py
Purpose    : Unit tests for server._format_browser_list().
Why        : Phase F2 review M-1 found two user-facing messages that hardcoded
             "Chrome or Edge" even though ALLOWED_BROWSERS had grown to include
             "firefox". The fix routes both messages through this helper so
             they're derived from ALLOWED_BROWSERS instead of being spelled out
             by hand. These tests pin down the helper's formatting rules in
             isolation (0/1/2/3+ items, custom conjunction) and, separately,
             confirm the two concrete call patterns used at the actual call
             sites (server.py's timeout message and startup-log curl hint)
             always include every member of ALLOWED_BROWSERS. That second
             group is what keeps this suite from going stale the next time a
             browser is added: it asserts membership against the live
             ALLOWED_BROWSERS set rather than a copy-pasted expected string,
             so a future "add safari" change requires no test edits here.
Related    : server.py (_format_browser_list, ALLOWED_BROWSERS,
             ChromeKontrolServer._resolve_client, run_serve_mode)
"""

from __future__ import annotations

import server


def test_format_browser_list_empty_returns_empty_string() -> None:
    assert server._format_browser_list([]) == ""


def test_format_browser_list_single_item_returned_unchanged() -> None:
    assert server._format_browser_list(["chrome"]) == "chrome"


def test_format_browser_list_two_items_joined_with_default_conjunction() -> None:
    assert server._format_browser_list(["chrome", "edge"]) == "chrome or edge"


def test_format_browser_list_three_items_uses_oxford_comma() -> None:
    assert server._format_browser_list(["chrome", "edge", "firefox"]) == "chrome, edge, or firefox"


def test_format_browser_list_four_items_extends_the_comma_list() -> None:
    assert server._format_browser_list(["a", "b", "c", "d"]) == "a, b, c, or d"


def test_format_browser_list_accepts_a_generator_not_just_a_list() -> None:
    """The parameter is typed Iterable[str]; a one-shot generator must work too."""
    result = server._format_browser_list(name.upper() for name in ["chrome", "edge"])
    assert result == "CHROME or EDGE"


def test_format_browser_list_custom_conjunction() -> None:
    assert server._format_browser_list(["chrome", "edge"], conjunction="and") == "chrome and edge"


def test_format_browser_list_preserves_caller_supplied_order() -> None:
    """The helper does not sort; ordering is the caller's responsibility

    (server.py always passes sorted(ALLOWED_BROWSERS) so output is stable
    across runs, since frozenset iteration order is not guaranteed).
    """
    assert server._format_browser_list(["zebra", "alpha"]) == "zebra or alpha"


# ---------------------------------------------------------------------------
# Phase F2 M-1: the two concrete usages in server.py must always surface every
# member of ALLOWED_BROWSERS, so that adding a browser there can never leave
# either message out of date again.
# ---------------------------------------------------------------------------


def test_format_browser_list_with_capitalized_allowed_browsers_contains_every_browser() -> None:
    """Mirrors the _resolve_client() timeout-message call site."""
    result = server._format_browser_list(name.capitalize() for name in sorted(server.ALLOWED_BROWSERS))
    for browser in server.ALLOWED_BROWSERS:
        assert browser.capitalize() in result


def test_format_browser_list_with_quoted_browser_field_contains_every_browser() -> None:
    """Mirrors the run_serve_mode() startup-log curl-hint call site.

    run_serve_mode() itself is excluded from coverage (process entry point
    that binds real sockets — see pyproject.toml's exclude_also), so this is
    the only place the exact call pattern used there gets exercised.
    """
    result = server._format_browser_list(f'"browser":"{name}"' for name in sorted(server.ALLOWED_BROWSERS))
    for browser in server.ALLOWED_BROWSERS:
        assert f'"browser":"{browser}"' in result


def test_format_browser_list_output_is_stable_across_repeated_calls() -> None:
    """Guards the sorted() requirement: unsorted frozenset iteration would make
    this message change from run to run, which is exactly what M-1 warned
    against ("otherwise logs/messages would vary between runs and tests
    couldn't be written").
    """
    first = server._format_browser_list(sorted(server.ALLOWED_BROWSERS))
    second = server._format_browser_list(sorted(server.ALLOWED_BROWSERS))
    assert first == second
