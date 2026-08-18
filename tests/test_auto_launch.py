"""
Location   : ChromeKontrol/tests/test_auto_launch.py
Purpose    : Unit tests for ChromeKontrolServer._auto_launch_response() and the
             auto-launch branch of _resolve_client() (ISSUES.md P0-1, Phase 2c).
Why        : This is the single most security-sensitive feature in the project -- the
             first code path where the server spawns an external process. Every gating
             condition (autoLaunch enabled, a "profiles" entry exists, no client is
             currently connected, no active cooldown, the executable resolves) must be
             enforced in order, each must produce a distinguishable error message, and
             the actual subprocess.Popen() call must be verified argument-by-argument
             (shell=False, an argv list, all three standard streams redirected to
             DEVNULL) since this is exactly the boundary Security-Audit.md flags for
             "特に2cのプロファイル自動起動は重点的な検証が必要". No test in this file
             spawns a real browser process; subprocess.Popen is always mocked.
Related    : server.py, ISSUES.md P0-1, Security-Audit.md
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from unittest.mock import MagicMock

import pytest

import server
from tests.conftest import FakeWebSocket, as_ws_protocol


@pytest.fixture
def kontrol_with_launch() -> server.ChromeKontrolServer:
    """A server with auto-launch enabled and a single profile registered, mirroring the
    task brief's example config (target "仕事" is not used here; tests exercise the
    resolved-string form directly via target="chrome:work@example.com" for clarity,
    since alias resolution itself is already covered by test_resolve_target.py).
    """
    return server.ChromeKontrolServer(
        auto_launch=True,
        profiles={"chrome:work@example.com": "Profile 1"},
    )


def _patch_popen(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Replace subprocess.Popen with a MagicMock that never actually spawns a process."""
    mock_popen = MagicMock(return_value=MagicMock())
    monkeypatch.setattr(subprocess, "Popen", mock_popen)
    return mock_popen


def _patch_executable(monkeypatch: pytest.MonkeyPatch, path: str = "/usr/bin/google-chrome") -> None:
    monkeypatch.setattr(server, "_resolve_browser_executable", lambda browser: path)


# ---------------------------------------------------------------------------
# Gating condition 1: autoLaunch disabled
# ---------------------------------------------------------------------------


async def test_auto_launch_response_disabled_returns_distinct_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kontrol = server.ChromeKontrolServer(auto_launch=False, profiles={"chrome:work@example.com": "Profile 1"})
    mock_popen = _patch_popen(monkeypatch)

    base_message = "Target 'chrome:work@example.com' is not connected. Connected: (none)."
    result = await kontrol._auto_launch_response("chrome:work@example.com", base_message)

    assert isinstance(result, dict)
    assert result["result"] == "error"
    assert "not connected" in result["message"]
    assert "Auto-launch is disabled" in result["message"]
    assert "autoLaunch" in result["message"]
    mock_popen.assert_not_called()


# ---------------------------------------------------------------------------
# Gating condition 3: no matching "profiles" entry
# ---------------------------------------------------------------------------


async def test_auto_launch_response_no_profiles_entry_returns_distinct_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kontrol = server.ChromeKontrolServer(auto_launch=True, profiles={})
    mock_popen = _patch_popen(monkeypatch)

    result = await kontrol._auto_launch_response("chrome:nobody@example.com", "not-connected-base-message")

    assert isinstance(result, dict)
    assert result["result"] == "error"
    assert "not-connected-base-message" in result["message"]
    assert '"profiles"' in result["message"]
    mock_popen.assert_not_called()


async def test_auto_launch_response_profiles_entry_without_recognised_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A profiles entry whose key is not "browser:identifier" form (misconfiguration) must
    not attempt to launch -- there's no way to know which executable to run.
    """
    kontrol = server.ChromeKontrolServer(auto_launch=True, profiles={"nobrowserprefix": "Default"})
    mock_popen = _patch_popen(monkeypatch)

    result = await kontrol._auto_launch_response("nobrowserprefix", "not-connected-base-message")

    assert isinstance(result, dict)
    assert result["result"] == "error"
    assert "recognised browser" in result["message"] or "browser" in result["message"].lower()
    mock_popen.assert_not_called()


# ---------------------------------------------------------------------------
# Gating condition 5: cooldown
# ---------------------------------------------------------------------------


async def test_auto_launch_response_cooldown_blocks_relaunch(
    kontrol_with_launch: server.ChromeKontrolServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_executable(monkeypatch)
    mock_popen = _patch_popen(monkeypatch)

    loop = asyncio.get_running_loop()
    kontrol_with_launch._launch_attempts["chrome:work@example.com"] = loop.time()

    result = await kontrol_with_launch._auto_launch_response("chrome:work@example.com", "base-message")

    assert isinstance(result, dict)
    assert result["result"] == "error"
    assert "cooldown" in result["message"].lower()
    mock_popen.assert_not_called()


async def test_auto_launch_response_cooldown_elapsed_allows_relaunch(
    kontrol_with_launch: server.ChromeKontrolServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_executable(monkeypatch)
    mock_popen = _patch_popen(monkeypatch)
    monkeypatch.setattr(server, "AUTO_LAUNCH_WAIT_TIMEOUT", 0.05)

    loop = asyncio.get_running_loop()
    kontrol_with_launch._launch_attempts["chrome:work@example.com"] = (
        loop.time() - server.AUTO_LAUNCH_COOLDOWN_SECONDS - 1
    )

    result = await kontrol_with_launch._auto_launch_response("chrome:work@example.com", "base-message")

    mock_popen.assert_called_once()
    assert isinstance(result, dict)
    assert "connected within" in result["message"]  # timed out waiting, but launch WAS attempted


# ---------------------------------------------------------------------------
# Gating condition 6: executable not found
# ---------------------------------------------------------------------------


async def test_auto_launch_response_executable_not_found(
    kontrol_with_launch: server.ChromeKontrolServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)
    mock_popen = _patch_popen(monkeypatch)

    result = await kontrol_with_launch._auto_launch_response("chrome:work@example.com", "base-message")

    assert isinstance(result, dict)
    assert result["result"] == "error"
    assert "chrome" in result["message"]
    assert "google-chrome" in result["message"]
    mock_popen.assert_not_called()
    # Cooldown must not be recorded for a condition that isn't even a launch attempt.
    assert "chrome:work@example.com" not in kontrol_with_launch._launch_attempts


# ---------------------------------------------------------------------------
# Gating condition 6b (Phase F2 regression): a browser present in
# ALLOWED_BROWSERS but absent from BROWSER_EXECUTABLE_CANDIDATES (firefox,
# by design -- see BROWSER_EXECUTABLE_CANDIDATES's definition-site comment).
#
# Before the Phase F2 fix, `BROWSER_EXECUTABLE_CANDIDATES[browser]` was
# indexed directly to build the "not found on PATH" message, which raised a
# bare KeyError for any browser without a candidates entry. That branch was
# unreachable while ALLOWED_BROWSERS == BROWSER_EXECUTABLE_CANDIDATES.keys(),
# but became reachable the moment 'firefox' joined ALLOWED_BROWSERS without
# also joining BROWSER_EXECUTABLE_CANDIDATES. This section pins the fix.
# ---------------------------------------------------------------------------


async def test_auto_launch_response_unsupported_browser_does_not_raise_keyerror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kontrol = server.ChromeKontrolServer(auto_launch=True, profiles={"firefox:work@example.com": "default"})
    mock_popen = _patch_popen(monkeypatch)

    # No try/except here is deliberate: a regression back to the direct dict
    # index would raise KeyError, and an uncaught exception fails the test.
    result = await kontrol._auto_launch_response("firefox:work@example.com", "base-message")

    assert isinstance(result, dict)
    assert result["result"] == "error"
    mock_popen.assert_not_called()


async def test_auto_launch_response_unsupported_browser_message_says_not_supported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The message must say auto-launch is unsupported for this browser -- not "not found
    on PATH" (that phrasing is reserved for the executable-not-found case below, which has
    a different, actionable remedy: install the browser / fix PATH).
    """
    kontrol = server.ChromeKontrolServer(auto_launch=True, profiles={"firefox:work@example.com": "default"})
    _patch_popen(monkeypatch)

    result = await kontrol._auto_launch_response("firefox:work@example.com", "base-message")

    assert isinstance(result, dict)
    assert "not supported" in result["message"].lower()
    assert "not found" not in result["message"].lower()


async def test_auto_launch_response_unsupported_browser_does_not_record_cooldown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Popen is never reached for this browser, so there is no launch attempt to clock --
    mirrors the executable-not-found case's cooldown-exemption behavior.
    """
    kontrol = server.ChromeKontrolServer(auto_launch=True, profiles={"firefox:work@example.com": "default"})
    _patch_popen(monkeypatch)

    await kontrol._auto_launch_response("firefox:work@example.com", "base-message")

    assert "firefox:work@example.com" not in kontrol._launch_attempts


async def test_auto_launch_response_chrome_executable_not_found_still_uses_legacy_message(
    kontrol_with_launch: server.ChromeKontrolServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Contrast case explicitly requested by the Phase F2 spec: chrome DOES have
    BROWSER_EXECUTABLE_CANDIDATES entries, so a shutil.which() miss must still produce the
    original "candidates were not found on PATH" message, not the new "not supported"
    message introduced for firefox above.
    """
    monkeypatch.setattr(shutil, "which", lambda name: None)
    mock_popen = _patch_popen(monkeypatch)

    result = await kontrol_with_launch._auto_launch_response("chrome:work@example.com", "base-message")

    assert isinstance(result, dict)
    assert "not supported" not in result["message"].lower()
    assert "google-chrome" in result["message"]
    assert "none of the expected executables" in result["message"]
    assert "were found on PATH" in result["message"]
    mock_popen.assert_not_called()


# ---------------------------------------------------------------------------
# Launch failure (Popen raises OSError)
# ---------------------------------------------------------------------------


async def test_auto_launch_response_popen_oserror_returns_distinct_message(
    kontrol_with_launch: server.ChromeKontrolServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_executable(monkeypatch)
    mock_popen = MagicMock(side_effect=OSError("no such file"))
    monkeypatch.setattr(subprocess, "Popen", mock_popen)

    result = await kontrol_with_launch._auto_launch_response("chrome:work@example.com", "base-message")

    assert isinstance(result, dict)
    assert result["result"] == "error"
    assert "could not be started" in result["message"]
    assert "no such file" not in result["message"]  # exception details must not leak
    mock_popen.assert_called_once()
    # "launching" flag must be cleared even on failure (spec section 7).
    assert "chrome:work@example.com" not in kontrol_with_launch._launching
    # Cooldown IS recorded even on failure (spec: regardless of success/failure).
    assert "chrome:work@example.com" in kontrol_with_launch._launch_attempts


# ---------------------------------------------------------------------------
# Timeout waiting for connection after a successful launch
# ---------------------------------------------------------------------------


async def test_auto_launch_response_times_out_waiting_for_connection(
    kontrol_with_launch: server.ChromeKontrolServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_executable(monkeypatch)
    mock_popen = _patch_popen(monkeypatch)
    monkeypatch.setattr(server, "AUTO_LAUNCH_WAIT_TIMEOUT", 0.05)

    result = await kontrol_with_launch._auto_launch_response("chrome:work@example.com", "base-message")

    assert isinstance(result, dict)
    assert result["result"] == "error"
    assert "connected within" in result["message"]
    mock_popen.assert_called_once()
    assert "chrome:work@example.com" not in kontrol_with_launch._launching


# ---------------------------------------------------------------------------
# Success path
# ---------------------------------------------------------------------------


async def test_auto_launch_response_succeeds_when_client_connects(
    kontrol_with_launch: server.ChromeKontrolServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_executable(monkeypatch)
    mock_popen = _patch_popen(monkeypatch)
    monkeypatch.setattr(server, "AUTO_LAUNCH_WAIT_TIMEOUT", 2.0)

    task = asyncio.create_task(kontrol_with_launch._auto_launch_response("chrome:work@example.com", "base-message"))

    async def connect_after_delay() -> FakeWebSocket:
        await asyncio.sleep(0.1)
        ws = FakeWebSocket()
        kontrol_with_launch._clients["chrome:profile-a"] = server.ClientInfo(
            browser="chrome",
            websocket=as_ws_protocol(ws),
            profile_id="profile-a",
            email="work@example.com",
        )
        return ws

    connector = asyncio.create_task(connect_after_delay())
    ws = await connector
    result = await asyncio.wait_for(task, timeout=2.0)

    assert result is as_ws_protocol(ws)
    mock_popen.assert_called_once()
    assert "chrome:work@example.com" not in kontrol_with_launch._launching


# ---------------------------------------------------------------------------
# Popen() call argument verification
# ---------------------------------------------------------------------------


async def test_auto_launch_popen_called_with_correct_executable_and_args(
    kontrol_with_launch: server.ChromeKontrolServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_executable(monkeypatch, path="/usr/bin/google-chrome")
    mock_popen = _patch_popen(monkeypatch)
    monkeypatch.setattr(server, "AUTO_LAUNCH_WAIT_TIMEOUT", 0.05)

    await kontrol_with_launch._auto_launch_response("chrome:work@example.com", "base-message")

    mock_popen.assert_called_once()
    call_args, call_kwargs = mock_popen.call_args
    assert call_args[0] == ["/usr/bin/google-chrome", "--profile-directory=Profile 1"]
    assert call_kwargs["shell"] is False
    assert call_kwargs["stdin"] is subprocess.DEVNULL
    assert call_kwargs["stdout"] is subprocess.DEVNULL
    assert call_kwargs["stderr"] is subprocess.DEVNULL


async def test_auto_launch_popen_profile_directory_with_space_is_single_argv_element(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The profile directory name containing a literal space (e.g. "Profile 1") must be
    passed as one argv element -- proof that shell=False + array args keep it intact
    rather than letting a shell split it into two arguments.
    """
    kontrol = server.ChromeKontrolServer(auto_launch=True, profiles={"chrome:*": "Profile 1"})
    _patch_executable(monkeypatch)
    mock_popen = _patch_popen(monkeypatch)
    monkeypatch.setattr(server, "AUTO_LAUNCH_WAIT_TIMEOUT", 0.05)

    await kontrol._auto_launch_response("chrome:*", "base-message")

    call_args, _ = mock_popen.call_args
    argv = call_args[0]
    assert len(argv) == 2
    assert argv[1] == "--profile-directory=Profile 1"


# ---------------------------------------------------------------------------
# Duplicate-launch prevention (via send_command()'s serialization)
# ---------------------------------------------------------------------------


async def test_repeated_requests_for_same_target_only_launch_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two consecutive send_command() calls for the same target must only call Popen
    once: the first call attempts the launch and (in this test) times out waiting for a
    connection; the second call, arriving after the first releases _command_lock, must
    see the still-active cooldown and skip launching entirely.
    """
    kontrol = server.ChromeKontrolServer(auto_launch=True, profiles={"chrome:work@example.com": "Profile 1"})
    _patch_executable(monkeypatch)
    mock_popen = _patch_popen(monkeypatch)
    monkeypatch.setattr(server, "AUTO_LAUNCH_WAIT_TIMEOUT", 0.05)

    result_a = await kontrol.send_command({"cmd": "list_tabs"}, target="chrome:work@example.com", timeout=1.0)
    result_b = await kontrol.send_command({"cmd": "list_tabs"}, target="chrome:work@example.com", timeout=1.0)

    assert result_a["result"] == "error"
    assert result_b["result"] == "error"
    assert "cooldown" in result_b["message"].lower()
    mock_popen.assert_called_once()


# ---------------------------------------------------------------------------
# _resolve_client()'s auto-launch dispatch
# ---------------------------------------------------------------------------


async def test_resolve_client_does_not_auto_launch_when_ambiguous(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two connected clients matching a wildcard target is an ambiguity error, not a
    "not connected" state -- auto-launch must not be attempted (it wouldn't resolve the
    ambiguity anyway), and the original ambiguity error must propagate unchanged.
    """
    kontrol = server.ChromeKontrolServer(auto_launch=True, profiles={"chrome:*": "Default"})
    mock_popen = _patch_popen(monkeypatch)

    ws_a = FakeWebSocket()
    ws_b = FakeWebSocket()
    kontrol._clients["chrome:a"] = server.ClientInfo(browser="chrome", websocket=as_ws_protocol(ws_a), profile_id="a")
    kontrol._clients["chrome:b"] = server.ClientInfo(browser="chrome", websocket=as_ws_protocol(ws_b), profile_id="b")

    result = await kontrol._resolve_client(None, timeout=1.0, target="chrome:*")

    assert isinstance(result, dict)
    assert result["result"] == "error"
    assert "Multiple clients matched" in result["message"]
    mock_popen.assert_not_called()


async def test_resolve_client_auto_launches_and_returns_websocket_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kontrol = server.ChromeKontrolServer(auto_launch=True, profiles={"chrome:work@example.com": "Profile 1"})
    _patch_executable(monkeypatch)
    mock_popen = _patch_popen(monkeypatch)
    monkeypatch.setattr(server, "AUTO_LAUNCH_WAIT_TIMEOUT", 2.0)

    task = asyncio.create_task(kontrol._resolve_client(None, timeout=1.0, target="chrome:work@example.com"))

    async def connect_after_delay() -> FakeWebSocket:
        await asyncio.sleep(0.1)
        ws = FakeWebSocket()
        kontrol._clients["chrome:a"] = server.ClientInfo(
            browser="chrome", websocket=as_ws_protocol(ws), profile_id="a", email="work@example.com"
        )
        return ws

    ws = await asyncio.create_task(connect_after_delay())
    result = await asyncio.wait_for(task, timeout=2.0)

    assert result is as_ws_protocol(ws)
    mock_popen.assert_called_once()


async def test_send_command_full_flow_auto_launches_via_http_style_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Integration-style check across send_command() -> _resolve_client() ->
    _auto_launch_response(), confirming the whole chain wires target through correctly
    and that a successful auto-launch lets the command actually reach the extension.
    """
    kontrol = server.ChromeKontrolServer(auto_launch=True, profiles={"chrome:work@example.com": "Profile 1"})
    _patch_executable(monkeypatch)
    mock_popen = _patch_popen(monkeypatch)
    monkeypatch.setattr(server, "AUTO_LAUNCH_WAIT_TIMEOUT", 2.0)

    task = asyncio.create_task(
        kontrol.send_command({"cmd": "list_tabs"}, target="chrome:work@example.com", timeout=2.0)
    )

    await asyncio.sleep(0.1)
    ws = FakeWebSocket()
    kontrol._clients["chrome:a"] = server.ClientInfo(
        browser="chrome", websocket=as_ws_protocol(ws), profile_id="a", email="work@example.com"
    )

    for _ in range(10):
        await asyncio.sleep(0)
    assert ws.sent_messages == [json.dumps({"cmd": "list_tabs"})]

    await kontrol._handle_message(json.dumps({"result": "ok", "data": []}), "chrome:a")
    result = await task

    assert result == {"result": "ok", "data": []}
    mock_popen.assert_called_once()
