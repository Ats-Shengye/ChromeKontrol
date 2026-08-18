"""
Location   : ChromeKontrol/tests/test_read_stdin_command.py
Purpose    : Unit tests for read_stdin_command() (the one-shot-mode CLI reader).
Why        : This function's return-value contract feeds directly into
             _validate_command in run_server(); it must distinguish "no
             command" (None) from a parsed value cleanly across every input
             shape (empty, whitespace-only, malformed JSON, valid JSON, and
             an OS-level read failure).
Related    : server.py
"""

from __future__ import annotations

import sys
from typing import Any

import pytest

import server


class _FakeStdin:
    """A minimal stand-in for sys.stdin exposing only the readline() method
    read_stdin_command() actually calls (via loop.run_in_executor)."""

    def __init__(self, line: str | None = None, error: Exception | None = None) -> None:
        self._line = line
        self._error = error

    def readline(self) -> str:
        if self._error is not None:
            raise self._error
        return self._line if self._line is not None else ""


async def test_read_stdin_command_returns_none_on_empty_input(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdin", _FakeStdin(line=""))
    result = await server.read_stdin_command()
    assert result is None


async def test_read_stdin_command_returns_none_on_whitespace_only_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "stdin", _FakeStdin(line="   \n"))
    result = await server.read_stdin_command()
    assert result is None


async def test_read_stdin_command_returns_none_on_invalid_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdin", _FakeStdin(line="not json\n"))
    result = await server.read_stdin_command()
    assert result is None


async def test_read_stdin_command_parses_valid_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdin", _FakeStdin(line='{"cmd": "get_dom"}\n'))
    result: Any = await server.read_stdin_command()
    assert result == {"cmd": "get_dom"}


async def test_read_stdin_command_strips_surrounding_whitespace_before_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "stdin", _FakeStdin(line='   {"cmd": "list_tabs"}   \n'))
    result: Any = await server.read_stdin_command()
    assert result == {"cmd": "list_tabs"}


async def test_read_stdin_command_returns_none_on_os_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdin", _FakeStdin(error=OSError("stdin closed")))
    result = await server.read_stdin_command()
    assert result is None
