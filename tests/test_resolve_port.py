"""
Location   : ChromeKontrol/tests/test_resolve_port.py
Purpose    : Unit tests for server._resolve_port().
Why        : Port selection sits ahead of the security-relevant bind step
             (BIND_HOST-only binding); a parsing bug here could silently
             fall back to an unexpected port. Priority order (CLI > env >
             default), out-of-range handling, and malformed-value handling
             all need explicit coverage per SPEC.md's documented precedence.
Related    : server.py
"""

from __future__ import annotations

import pytest

import server

ENV_VAR = "CHROME_KONTROL_TEST_PORT"
CLI_FLAG = "--port"
DEFAULT = 9765


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure the test environment variable never leaks between tests."""
    monkeypatch.delenv(ENV_VAR, raising=False)


def test_resolve_port_uses_default_when_nothing_else_set() -> None:
    assert server._resolve_port(ENV_VAR, CLI_FLAG, DEFAULT, []) == DEFAULT


def test_resolve_port_uses_env_var_when_no_cli_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_VAR, "8000")
    assert server._resolve_port(ENV_VAR, CLI_FLAG, DEFAULT, []) == 8000


def test_resolve_port_cli_flag_overrides_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_VAR, "8000")
    args = [CLI_FLAG, "9000"]
    assert server._resolve_port(ENV_VAR, CLI_FLAG, DEFAULT, args) == 9000


def test_resolve_port_cli_flag_overrides_default_when_env_unset() -> None:
    args = [CLI_FLAG, "9001"]
    assert server._resolve_port(ENV_VAR, CLI_FLAG, DEFAULT, args) == 9001


def test_resolve_port_falls_back_to_default_when_env_out_of_range_low(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ENV_VAR, "0")
    assert server._resolve_port(ENV_VAR, CLI_FLAG, DEFAULT, []) == DEFAULT


def test_resolve_port_falls_back_to_default_when_env_out_of_range_high(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ENV_VAR, "65536")
    assert server._resolve_port(ENV_VAR, CLI_FLAG, DEFAULT, []) == DEFAULT


def test_resolve_port_falls_back_to_default_when_env_not_an_integer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ENV_VAR, "not-a-number")
    assert server._resolve_port(ENV_VAR, CLI_FLAG, DEFAULT, []) == DEFAULT


def test_resolve_port_keeps_prior_value_when_cli_flag_out_of_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An out-of-range CLI value is rejected, but the already-resolved value (from env) sticks."""
    monkeypatch.setenv(ENV_VAR, "8000")
    args = [CLI_FLAG, "70000"]
    assert server._resolve_port(ENV_VAR, CLI_FLAG, DEFAULT, args) == 8000


def test_resolve_port_keeps_default_when_cli_flag_out_of_range_and_env_unset() -> None:
    args = [CLI_FLAG, "70000"]
    assert server._resolve_port(ENV_VAR, CLI_FLAG, DEFAULT, args) == DEFAULT


def test_resolve_port_keeps_prior_value_when_cli_flag_not_an_integer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ENV_VAR, "8000")
    args = [CLI_FLAG, "abc"]
    assert server._resolve_port(ENV_VAR, CLI_FLAG, DEFAULT, args) == 8000


def test_resolve_port_keeps_prior_value_when_cli_flag_has_no_following_value() -> None:
    """--port as the last argument (no value after it) is silently ignored, not an error."""
    args = [CLI_FLAG]
    assert server._resolve_port(ENV_VAR, CLI_FLAG, DEFAULT, args) == DEFAULT


def test_resolve_port_ignores_unrelated_cli_flags() -> None:
    args = ["--serve", "--http-port", "9766"]
    assert server._resolve_port(ENV_VAR, CLI_FLAG, DEFAULT, args) == DEFAULT


@pytest.mark.parametrize("boundary_value", [1, 65535])
def test_resolve_port_accepts_range_boundary_values_via_cli(boundary_value: int) -> None:
    args = [CLI_FLAG, str(boundary_value)]
    assert server._resolve_port(ENV_VAR, CLI_FLAG, DEFAULT, args) == boundary_value
