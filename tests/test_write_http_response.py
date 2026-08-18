"""
Location   : ChromeKontrol/tests/test_write_http_response.py
Purpose    : Unit tests for _write_http_response() and _write_http_error().
Why        : Every HTTP response the server ever sends is assembled by this
             one function. Its header set is a deliberate security control
             (SPEC.md "HTTP サーバー": Cache-Control: no-store,
             X-Content-Type-Options: nosniff) rather than incidental
             formatting, so the exact header set and values are worth
             pinning down directly, independent of any particular request
             path that happens to trigger a given status code.
Related    : server.py
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import server


def make_writer() -> MagicMock:
    writer = MagicMock()
    writer.write = MagicMock()
    writer.drain = AsyncMock()
    return writer


def parse_written_response(writer: MagicMock) -> tuple[str, dict[str, str], bytes]:
    raw: bytes = writer.write.call_args[0][0]
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    status_line = lines[0]
    header_map: dict[str, str] = {}
    for line in lines[1:]:
        name, sep, value = line.partition(":")
        if sep:
            header_map[name.strip()] = value.strip()
    return status_line, header_map, body


async def test_write_http_response_formats_status_line_and_known_reason() -> None:
    writer = make_writer()
    await server._write_http_response(writer, 200, b'{"result":"ok"}')
    status_line, _, body = parse_written_response(writer)
    assert status_line == "HTTP/1.1 200 OK"
    assert body == b'{"result":"ok"}'


async def test_write_http_response_falls_back_to_unknown_reason_for_unmapped_status() -> None:
    """A status code with no entry in the reason-phrase table still produces a well-formed line."""
    writer = make_writer()
    await server._write_http_response(writer, 404, b"{}")
    status_line, _, _ = parse_written_response(writer)
    assert status_line == "HTTP/1.1 404 Unknown"


async def test_write_http_response_sets_required_security_headers() -> None:
    writer = make_writer()
    body = b'{"result":"ok"}'
    await server._write_http_response(writer, 200, body)
    _, headers, _ = parse_written_response(writer)
    assert headers["Content-Type"] == "application/json"
    assert headers["Content-Length"] == str(len(body))
    assert headers["Connection"] == "close"
    assert headers["Cache-Control"] == "no-store"
    assert headers["X-Content-Type-Options"] == "nosniff"


async def test_write_http_response_content_length_matches_actual_body_bytes() -> None:
    """Content-Length must reflect the encoded byte length, not a Python string length."""
    writer = make_writer()
    # A body containing multi-byte UTF-8 characters to ensure byte-length (not
    # code-point-length) is what gets reported.
    body = json.dumps({"message": "エラー"}).encode("utf-8")
    await server._write_http_response(writer, 400, body)
    _, headers, written_body = parse_written_response(writer)
    assert headers["Content-Length"] == str(len(body))
    assert written_body == body


async def test_write_http_response_awaits_drain() -> None:
    writer = make_writer()
    await server._write_http_response(writer, 200, b"{}")
    writer.drain.assert_awaited_once()


async def test_write_http_response_swallows_drain_timeout() -> None:
    """A slow/stalled client (drain() never completing) must not raise out of this function.

    The fake drain() raises asyncio.TimeoutError directly from within the
    awaited coroutine, which asyncio.wait_for() propagates immediately
    (see test_http_handler.py's module docstring for why this technique
    reaches the same except-clause as a real 5-second stall, without
    the real wait).
    """
    writer = make_writer()
    writer.drain = AsyncMock(side_effect=asyncio.TimeoutError)
    await server._write_http_response(writer, 200, b"{}")  # must not raise


async def test_write_http_error_produces_result_error_json_body() -> None:
    writer = make_writer()
    await server._write_http_error(writer, 400, "Bad Request: invalid JSON body")
    status_line, headers, body = parse_written_response(writer)
    assert status_line == "HTTP/1.1 400 Bad Request"
    assert headers["Content-Type"] == "application/json"
    assert json.loads(body) == {"result": "error", "message": "Bad Request: invalid JSON body"}
