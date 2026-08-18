"""
Location   : ChromeKontrol/tests/test_ignore_sigchld.py
Purpose    : Unit tests for server._ignore_sigchld_for_auto_launched_children().
Why        : run_serve_mode() calls this helper once at startup to prevent zombie
             processes from accumulating when subprocess.Popen()-launched browser
             processes exit without anyone calling wait() on them (Security-Audit.md
             M-6, Phase 2c). Because run_serve_mode() itself is excluded from coverage
             (it binds real sockets and runs an unbounded loop), the SIGCHLD-ignoring
             behavior was extracted into its own function specifically so it could be
             exercised in isolation here.

             We never actually flip the test process's own SIGCHLD handler: doing so
             would risk interfering with pytest's own subprocess management (e.g.
             pytest-xdist workers, coverage subprocess hooks) for the remainder of the
             test session. Instead, signal.signal itself is monkeypatched, so the real
             handler is left untouched and pytest's monkeypatch fixture restores the
             original attribute automatically at teardown.
Related    : server.py (_ignore_sigchld_for_auto_launched_children, run_serve_mode)
"""

from __future__ import annotations

import signal
from unittest.mock import MagicMock

import pytest

import server


@pytest.mark.skipif(not hasattr(signal, "SIGCHLD"), reason="signal.SIGCHLD is POSIX-only")
def test_ignore_sigchld_sets_sig_ign_on_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    """On a platform that defines signal.SIGCHLD (Linux, per this project's assumption),
    the helper must install SIG_IGN so the kernel auto-reaps launched browser children."""
    mock_signal = MagicMock()
    # server.py does `import signal` at module scope, so patching the shared `signal`
    # module object (not a `server.signal` attribute) is what server._ignore_sigchld_*
    # actually observes, matching this suite's existing convention for patching
    # subprocess.Popen (see tests/test_auto_launch.py).
    monkeypatch.setattr(signal, "signal", mock_signal)

    server._ignore_sigchld_for_auto_launched_children()

    mock_signal.assert_called_once_with(signal.SIGCHLD, signal.SIG_IGN)


def test_ignore_sigchld_is_noop_when_sigchld_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """On a platform without signal.SIGCHLD (e.g. Windows), the helper must return
    without touching signal.signal, rather than raising AttributeError."""
    mock_signal = MagicMock()
    monkeypatch.setattr(signal, "signal", mock_signal)
    monkeypatch.delattr(signal, "SIGCHLD", raising=False)

    server._ignore_sigchld_for_auto_launched_children()

    mock_signal.assert_not_called()
