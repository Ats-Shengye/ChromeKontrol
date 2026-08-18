"""
Location   : ChromeKontrol/tests/test_token_persistence.py
Purpose    : Unit tests for server._resolve_token_file_path(), server._determine_auth_token(),
             and server._persist_token_to_file() (ISSUES.md P0-2, Phase 1).
Why        : Phase 1 replaced the previous token-sharing mechanism (grepping a log file that
             never existed under the server's real runtime) with a fixed, permission-locked
             file that mcp_bridge.mjs re-reads on every request. That file's permissions are
             the whole security property this change buys: a 0600 file readable only by the
             owning user, written atomically so a reader never observes a half-written token,
             and never containing the token in any log line. Every test in this file uses
             tmp_path so the real ~/.config/chromekontrol/ directory is never touched, per the
             task brief's explicit requirement.
Related    : server.py, mcp_bridge.mjs, ISSUES.md (P0-2)
"""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path

import pytest

import server

# ---------------------------------------------------------------------------
# _resolve_token_file_path()
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_token_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure CHROME_KONTROL_TOKEN / CHROME_KONTROL_TOKEN_FILE never leak between tests."""
    monkeypatch.delenv("CHROME_KONTROL_TOKEN", raising=False)
    monkeypatch.delenv(server.TOKEN_FILE_ENV_VAR, raising=False)


def test_resolve_token_file_path_uses_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """CHROME_KONTROL_TOKEN_FILE, when set to a non-blank value, takes precedence."""
    custom = tmp_path / "custom-dir" / "token"
    monkeypatch.setenv(server.TOKEN_FILE_ENV_VAR, str(custom))
    assert server._resolve_token_file_path() == custom


def test_resolve_token_file_path_ignores_blank_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """A whitespace-only override is treated as unset, matching _determine_auth_token's .strip() convention."""
    monkeypatch.setenv(server.TOKEN_FILE_ENV_VAR, "   ")
    assert server._resolve_token_file_path() == server.DEFAULT_TOKEN_FILE


def test_resolve_token_file_path_defaults_when_unset() -> None:
    assert server._resolve_token_file_path() == server.DEFAULT_TOKEN_FILE


# ---------------------------------------------------------------------------
# _determine_auth_token()
# ---------------------------------------------------------------------------


def test_determine_auth_token_uses_env_value(monkeypatch: pytest.MonkeyPatch) -> None:
    fixed = "a" * 40
    monkeypatch.setenv("CHROME_KONTROL_TOKEN", fixed)
    token, env_used = server._determine_auth_token()
    assert token == fixed
    assert env_used is True


def test_determine_auth_token_generates_when_unset() -> None:
    token, env_used = server._determine_auth_token()
    assert env_used is False
    assert len(token) > 0


def test_determine_auth_token_generates_a_fresh_value_each_call() -> None:
    """secrets.token_urlsafe(32) must not be memoised across calls (each server start rotates it)."""
    token1, _ = server._determine_auth_token()
    token2, _ = server._determine_auth_token()
    assert token1 != token2


def test_determine_auth_token_warns_on_short_env_token(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("CHROME_KONTROL_TOKEN", "short")
    with caplog.at_level(logging.WARNING):
        token, env_used = server._determine_auth_token()
    assert token == "short"
    assert env_used is True
    assert any("短すぎます" in rec.getMessage() for rec in caplog.records)


def test_determine_auth_token_ignores_blank_env_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """A whitespace-only env value (M-7's .strip() guard) falls back to a generated token."""
    monkeypatch.setenv("CHROME_KONTROL_TOKEN", "   ")
    token, env_used = server._determine_auth_token()
    assert env_used is False
    assert len(token) > 0


# ---------------------------------------------------------------------------
# _persist_token_to_file(): success paths
# ---------------------------------------------------------------------------


def test_persist_token_creates_parent_directory(tmp_path: Path) -> None:
    token_file = tmp_path / "chromekontrol" / "token"
    assert not token_file.parent.exists()
    server._persist_token_to_file("some-token-value", token_file)
    assert token_file.parent.is_dir()


def test_persist_token_parent_directory_permissions_are_0700(tmp_path: Path) -> None:
    token_file = tmp_path / "chromekontrol" / "token"
    server._persist_token_to_file("some-token-value", token_file)
    mode = stat.S_IMODE(token_file.parent.stat().st_mode)
    assert mode == 0o700


def test_persist_token_file_permissions_are_0600(tmp_path: Path) -> None:
    token_file = tmp_path / "chromekontrol" / "token"
    server._persist_token_to_file("some-token-value", token_file)
    mode = stat.S_IMODE(token_file.stat().st_mode)
    assert mode == 0o600


def test_persist_token_file_permissions_unaffected_by_permissive_umask(tmp_path: Path) -> None:
    """A permissive umask (0o000, i.e. no bits masked out) must not widen the file beyond 0600.

    os.open()/tempfile.mkstemp()'s mode argument is masked by the process umask; this test
    proves _persist_token_to_file's explicit os.chmod() calls override that, per the task
    brief's requirement that permissions hold "even with umask changed".
    """
    token_file = tmp_path / "chromekontrol" / "token"
    old_umask = os.umask(0o000)
    try:
        server._persist_token_to_file("some-token-value", token_file)
    finally:
        os.umask(old_umask)
    mode = stat.S_IMODE(token_file.stat().st_mode)
    assert mode == 0o600


def test_persist_token_directory_permissions_unaffected_by_permissive_umask(tmp_path: Path) -> None:
    token_file = tmp_path / "chromekontrol" / "token"
    old_umask = os.umask(0o000)
    try:
        server._persist_token_to_file("some-token-value", token_file)
    finally:
        os.umask(old_umask)
    mode = stat.S_IMODE(token_file.parent.stat().st_mode)
    assert mode == 0o700


def test_persist_token_content_matches(tmp_path: Path) -> None:
    token_file = tmp_path / "chromekontrol" / "token"
    server._persist_token_to_file("expected-token-abc123", token_file)
    assert token_file.read_text(encoding="utf-8") == "expected-token-abc123"


def test_persist_token_overwrites_existing_file_on_rotation(tmp_path: Path) -> None:
    """A second call (simulating a server restart) must replace, not append to, the old token."""
    token_file = tmp_path / "chromekontrol" / "token"
    server._persist_token_to_file("old-token", token_file)
    server._persist_token_to_file("new-token", token_file)
    assert token_file.read_text(encoding="utf-8") == "new-token"


def test_persist_token_leaves_no_leftover_temp_file_on_success(tmp_path: Path) -> None:
    token_file = tmp_path / "chromekontrol" / "token"
    server._persist_token_to_file("some-token-value", token_file)
    leftover = list(token_file.parent.glob(f".{token_file.name}.*.tmp"))
    assert leftover == []


def test_persist_token_uses_env_var_token_value(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The value _determine_auth_token() resolves from CHROME_KONTROL_TOKEN ends up written verbatim.

    Exercises the two functions together, mirroring how run_serve_mode() chains them.
    """
    fixed_token = "b" * 40
    monkeypatch.setenv("CHROME_KONTROL_TOKEN", fixed_token)
    token, env_used = server._determine_auth_token()
    assert env_used is True

    token_file = tmp_path / "chromekontrol" / "token"
    server._persist_token_to_file(token, token_file)
    assert token_file.read_text(encoding="utf-8") == fixed_token


def test_persist_token_file_path_override_is_honoured(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """CHROME_KONTROL_TOKEN_FILE resolved via _resolve_token_file_path() is where the token lands."""
    custom_path = tmp_path / "custom-location" / "token"
    monkeypatch.setenv(server.TOKEN_FILE_ENV_VAR, str(custom_path))
    resolved = server._resolve_token_file_path()
    server._persist_token_to_file("token-value", resolved)
    assert custom_path.read_text(encoding="utf-8") == "token-value"


def test_persist_token_success_does_not_log_token_value(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    token_file = tmp_path / "chromekontrol" / "token"
    secret = "must-never-appear-in-logs-success-path-1a2b3c"

    with caplog.at_level(logging.DEBUG):
        server._persist_token_to_file(secret, token_file)

    for record in caplog.records:
        assert secret not in record.getMessage()


# ---------------------------------------------------------------------------
# _persist_token_to_file(): failure paths (must never raise; must log a warning
# without the token value)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.name != "posix", reason="permission-bit test requires POSIX chmod semantics")
@pytest.mark.skipif(
    hasattr(os, "getuid") and os.getuid() == 0,
    reason="root bypasses permission checks; this test requires a non-root user",
)
def test_persist_token_write_failure_does_not_raise(tmp_path: Path) -> None:
    """A parent directory the process cannot write into must not propagate as an exception.

    This encodes the task brief's requirement that server startup continues even if token
    persistence fails (the environment-variable sharing path still works in that case).
    """
    readonly_root = tmp_path / "readonly"
    readonly_root.mkdir(mode=0o500)
    token_file = readonly_root / "sub" / "token"

    server._persist_token_to_file("token-value", token_file)  # must not raise

    assert not token_file.exists()


@pytest.mark.skipif(os.name != "posix", reason="permission-bit test requires POSIX chmod semantics")
@pytest.mark.skipif(
    hasattr(os, "getuid") and os.getuid() == 0,
    reason="root bypasses permission checks; this test requires a non-root user",
)
def test_persist_token_write_failure_logs_warning(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    readonly_root = tmp_path / "readonly"
    readonly_root.mkdir(mode=0o500)
    token_file = readonly_root / "sub" / "token"

    with caplog.at_level(logging.WARNING):
        server._persist_token_to_file("token-value", token_file)

    assert any("Failed to write token file" in rec.getMessage() for rec in caplog.records)


@pytest.mark.skipif(os.name != "posix", reason="permission-bit test requires POSIX chmod semantics")
@pytest.mark.skipif(
    hasattr(os, "getuid") and os.getuid() == 0,
    reason="root bypasses permission checks; this test requires a non-root user",
)
def test_persist_token_failure_does_not_log_token_value(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    readonly_root = tmp_path / "readonly"
    readonly_root.mkdir(mode=0o500)
    token_file = readonly_root / "sub" / "token"
    secret = "must-never-appear-in-logs-failure-path-9f8e7d"

    with caplog.at_level(logging.DEBUG):
        server._persist_token_to_file(secret, token_file)

    for record in caplog.records:
        assert secret not in record.getMessage()


def test_persist_token_rename_failure_cleans_up_temp_file_and_does_not_raise(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A failure at the final os.replace() step (e.g. cross-device edge cases) still cleans up.

    Simulated by monkeypatching os.replace, since provoking a *real* rename failure
    deterministically (short of cross-filesystem tricks unavailable under tmp_path) is not
    practical. This covers the second cleanup branch (after a successful write, before a
    successful rename), distinct from the mkdir-failure tests above (which never reach
    tempfile.mkstemp() at all).
    """
    token_file = tmp_path / "chromekontrol" / "token"

    def _boom(_src: object, _dst: object) -> None:
        raise OSError("simulated rename failure")

    monkeypatch.setattr(os, "replace", _boom)

    with caplog.at_level(logging.WARNING):
        server._persist_token_to_file("token-value", token_file)  # must not raise

    assert not token_file.exists()
    leftover = list(token_file.parent.glob(f".{token_file.name}.*.tmp"))
    assert leftover == []


def test_persist_token_write_failure_cleans_up_temp_file_and_does_not_raise(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A failure while writing the temp file's contents (the `except BaseException` handler
    guarding os.chmod()/os.fdopen(), distinct from the os.replace()-failure test above) also
    cleans up the orphaned temp file and does not propagate.

    Simulated by monkeypatching os.fdopen to fail after closing the fd itself (mirroring what
    a real fdopen failure would leave behind: an already-open fd that must still be closed).
    """
    token_file = tmp_path / "chromekontrol" / "token"

    def _boom_fdopen(fd: int, mode: str = "r", encoding: str | None = None) -> None:
        os.close(fd)
        raise OSError("simulated fdopen failure")

    monkeypatch.setattr(os, "fdopen", _boom_fdopen)

    with caplog.at_level(logging.WARNING):
        server._persist_token_to_file("token-value", token_file)  # must not raise

    assert not token_file.exists()
    leftover = list(token_file.parent.glob(f".{token_file.name}.*.tmp"))
    assert leftover == []


def test_persist_token_rename_failure_survives_cleanup_unlink_also_failing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A double failure (rename fails, then cleanup of the orphaned temp file also fails)
    must still not raise.

    Covers the innermost `except OSError: pass` guard around the cleanup unlink call inside
    the outer `except OSError` handler -- the last line of defense that keeps a cleanup
    failure from masking the original error or crashing server startup.
    """
    token_file = tmp_path / "chromekontrol" / "token"

    def _boom_replace(_src: object, _dst: object) -> None:
        raise OSError("simulated rename failure")

    def _boom_unlink(self: Path, missing_ok: bool = False) -> None:
        raise OSError("simulated unlink failure")

    monkeypatch.setattr(os, "replace", _boom_replace)
    monkeypatch.setattr(Path, "unlink", _boom_unlink)

    with caplog.at_level(logging.WARNING):
        server._persist_token_to_file("token-value", token_file)  # must not raise
