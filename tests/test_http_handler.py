"""
Location   : ChromeKontrol/tests/test_http_handler.py
Purpose    : Unit tests for _handle_http_request() (the serve-mode HTTP API).
Why        : This function is the only network-facing surface that parses
             untrusted bytes directly off a socket (headers, Content-Length,
             JSON body) before any of it reaches _validate_command. Every
             documented status code (SPEC.md "HTTP サーバー") corresponds to
             a distinct rejection branch that must be verified in isolation,
             since conflating any two of them (e.g. returning 400 where 411
             is documented) would be a silent contract break for callers.

             Timeout paths (408) are exercised via fake reader objects whose
             read()/readexactly() raise asyncio.TimeoutError immediately,
             rather than waiting out the real 10-second deadlines hardcoded
             in server.py. asyncio.wait_for() propagates an exception raised
             by the wrapped awaitable exactly as if no timeout occurred, so
             this reaches the identical `except asyncio.TimeoutError:` branch
             deterministically and near-instantly (Coding.md "実時間の待機
             を避ける" guidance).
Related    : server.py
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

import server

# Fixed test-double token: not a real secret, used only to compare against
# itself within a single test process. flake8-bandit/ruff S105 is silenced
# for the whole tests/ package via pyproject.toml's per-file-ignores.
AUTH_TOKEN = "unit-test-fixed-token-0123456789abcdef"


class _HeaderTimeoutReader:
    """Fake reader simulating a client that stalls before sending headers.

    Only `read()` is implemented (the only method _handle_http_request calls
    while accumulating headers). Raising asyncio.TimeoutError directly from
    within the awaited coroutine makes asyncio.wait_for() surface that same
    exception immediately, without needing the real 10-second deadline to
    elapse.
    """

    async def read(self, n: int) -> bytes:
        raise asyncio.TimeoutError


class _BodyTimeoutReader(asyncio.StreamReader):
    """A real StreamReader for the header phase, but readexactly() stalls forever.

    Subclassing lets header parsing behave exactly like production (reading
    from a real buffered StreamReader) while isolating the timeout
    simulation to the body-read call specifically.
    """

    async def readexactly(self, n: int) -> bytes:
        raise asyncio.TimeoutError


def make_reader(data: bytes) -> asyncio.StreamReader:
    """A real StreamReader pre-loaded with `data` and immediately EOF'd.

    feed_data() + feed_eof() before any read() call means .read()/.readexactly()
    never actually suspend waiting for more bytes, keeping these tests fast.
    """
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    return reader


def make_writer() -> MagicMock:
    writer = MagicMock()
    writer.get_extra_info.return_value = ("127.0.0.1", 54321)
    writer.write = MagicMock()
    writer.drain = AsyncMock()
    writer.close = MagicMock()
    writer.wait_closed = AsyncMock()
    return writer


def build_http_request(headers: dict[str, str], body: bytes = b"", method: str = "POST") -> bytes:
    """Assemble a raw HTTP/1.1 request from an ordered header dict and body.

    Omitting a key from `headers` simulates that header being entirely
    absent from the wire (as opposed to present-but-empty), matching how
    real HTTP clients behave.
    """
    lines = [f"{method} / HTTP/1.1"]
    lines.extend(f"{name}: {value}" for name, value in headers.items())
    head = "\r\n".join(lines) + "\r\n\r\n"
    return head.encode("latin-1") + body


def default_headers(content_length: int, *, content_type: str = "application/json") -> dict[str, str]:
    return {
        "Host": "127.0.0.1:9766",
        "Content-Type": content_type,
        "Content-Length": str(content_length),
        server.HTTP_AUTH_HEADER_NAME: AUTH_TOKEN,
    }


def parse_response(writer: MagicMock) -> tuple[int, dict[str, str], Any]:
    """Extract (status_code, header_map, json_body) from the bytes passed to writer.write()."""
    assert writer.write.called, "handler never wrote a response"
    raw: bytes = writer.write.call_args[0][0]
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    status_code = int(lines[0].split(" ", 2)[1])
    header_map: dict[str, str] = {}
    for line in lines[1:]:
        name, sep, value = line.partition(":")
        if sep:
            header_map[name.strip()] = value.strip()
    payload = json.loads(body.decode("utf-8")) if body else None
    return status_code, header_map, payload


@pytest.fixture
def kontrol() -> server.ChromeKontrolServer:
    instance = server.ChromeKontrolServer()
    # Replaced per-test with an AsyncMock where the test needs to assert on
    # dispatch; left as the real (unused) implementation otherwise so tests
    # that expect *no* dispatch fail loudly if that assumption is ever wrong.
    return instance


async def test_handle_http_request_returns_200_and_forwards_to_send_command(
    kontrol: server.ChromeKontrolServer,
) -> None:
    kontrol.send_command = AsyncMock(return_value={"result": "ok", "data": "<html></html>"})  # type: ignore[method-assign]
    body = json.dumps({"cmd": "get_dom", "browser": "edge"}).encode("utf-8")
    reader = make_reader(build_http_request(default_headers(len(body)), body))
    writer = make_writer()

    await server._handle_http_request(reader, writer, kontrol, AUTH_TOKEN)

    status, headers, payload = parse_response(writer)
    assert status == 200
    assert payload == {"result": "ok", "data": "<html></html>"}
    kontrol.send_command.assert_awaited_once_with({"cmd": "get_dom", "browser": "edge"}, browser="edge", target=None)


async def test_handle_http_request_defaults_target_browser_to_none_when_absent(
    kontrol: server.ChromeKontrolServer,
) -> None:
    kontrol.send_command = AsyncMock(return_value={"result": "ok"})  # type: ignore[method-assign]
    body = json.dumps({"cmd": "list_tabs"}).encode("utf-8")
    reader = make_reader(build_http_request(default_headers(len(body)), body))
    writer = make_writer()

    await server._handle_http_request(reader, writer, kontrol, AUTH_TOKEN)

    kontrol.send_command.assert_awaited_once_with({"cmd": "list_tabs"}, browser=None, target=None)


async def test_handle_http_request_forwards_target_field_to_send_command(
    kontrol: server.ChromeKontrolServer,
) -> None:
    """ISSUES.md P0-1 (Phase 2b): the "target" field must reach send_command() alongside
    (in place of) "browser", exactly like the existing browser-forwarding test above.
    """
    kontrol.send_command = AsyncMock(return_value={"result": "ok"})  # type: ignore[method-assign]
    body = json.dumps({"cmd": "list_tabs", "target": "仕事"}).encode("utf-8")
    reader = make_reader(build_http_request(default_headers(len(body)), body))
    writer = make_writer()

    await server._handle_http_request(reader, writer, kontrol, AUTH_TOKEN)

    kontrol.send_command.assert_awaited_once_with({"cmd": "list_tabs", "target": "仕事"}, browser=None, target="仕事")


async def test_handle_http_request_rejects_non_post_method(
    kontrol: server.ChromeKontrolServer,
) -> None:
    reader = make_reader(b"GET / HTTP/1.1\r\n\r\n")
    writer = make_writer()

    await server._handle_http_request(reader, writer, kontrol, AUTH_TOKEN)

    status, _, payload = parse_response(writer)
    assert status == 405
    assert payload == {"result": "error", "message": "Method Not Allowed"}


async def test_handle_http_request_rejects_missing_token(
    kontrol: server.ChromeKontrolServer,
) -> None:
    headers = default_headers(2)
    del headers[server.HTTP_AUTH_HEADER_NAME]
    reader = make_reader(build_http_request(headers, b"{}"))
    writer = make_writer()

    await server._handle_http_request(reader, writer, kontrol, AUTH_TOKEN)

    status, _, payload = parse_response(writer)
    assert status == 401
    assert payload == {"result": "error", "message": "Unauthorized"}


async def test_handle_http_request_rejects_mismatched_token(
    kontrol: server.ChromeKontrolServer,
) -> None:
    headers = default_headers(2)
    headers[server.HTTP_AUTH_HEADER_NAME] = "completely-different-token"
    reader = make_reader(build_http_request(headers, b"{}"))
    writer = make_writer()

    await server._handle_http_request(reader, writer, kontrol, AUTH_TOKEN)

    status, _, payload = parse_response(writer)
    assert status == 401
    assert payload == {"result": "error", "message": "Unauthorized"}


async def test_handle_http_request_missing_and_mismatched_token_share_identical_message(
    kontrol: server.ChromeKontrolServer,
) -> None:
    """Both 401 variants must return the exact same body to prevent header-presence enumeration."""
    headers_missing = default_headers(2)
    del headers_missing[server.HTTP_AUTH_HEADER_NAME]
    reader1 = make_reader(build_http_request(headers_missing, b"{}"))
    writer1 = make_writer()
    await server._handle_http_request(reader1, writer1, kontrol, AUTH_TOKEN)
    _, _, payload1 = parse_response(writer1)

    headers_wrong = default_headers(2)
    headers_wrong[server.HTTP_AUTH_HEADER_NAME] = "wrong"
    reader2 = make_reader(build_http_request(headers_wrong, b"{}"))
    writer2 = make_writer()
    await server._handle_http_request(reader2, writer2, kontrol, AUTH_TOKEN)
    _, _, payload2 = parse_response(writer2)

    assert payload1 == payload2


@pytest.mark.parametrize("content_type", ["text/plain", "application/xml", ""])
async def test_handle_http_request_rejects_wrong_content_type(
    kontrol: server.ChromeKontrolServer, content_type: str
) -> None:
    headers = default_headers(2, content_type=content_type)
    if content_type == "":
        del headers["Content-Type"]
    reader = make_reader(build_http_request(headers, b"{}"))
    writer = make_writer()

    await server._handle_http_request(reader, writer, kontrol, AUTH_TOKEN)

    status, _, payload = parse_response(writer)
    assert status == 415
    assert payload == {"result": "error", "message": "Unsupported Media Type"}


async def test_handle_http_request_accepts_content_type_with_charset_parameter(
    kontrol: server.ChromeKontrolServer,
) -> None:
    """Only the media-type portion is compared; `; charset=utf-8` must not cause a 415."""
    kontrol.send_command = AsyncMock(return_value={"result": "ok"})  # type: ignore[method-assign]
    body = json.dumps({"cmd": "list_tabs"}).encode("utf-8")
    headers = default_headers(len(body), content_type="application/json; charset=utf-8")
    reader = make_reader(build_http_request(headers, body))
    writer = make_writer()

    await server._handle_http_request(reader, writer, kontrol, AUTH_TOKEN)

    status, _, _ = parse_response(writer)
    assert status == 200


async def test_handle_http_request_accepts_content_type_case_insensitively(
    kontrol: server.ChromeKontrolServer,
) -> None:
    kontrol.send_command = AsyncMock(return_value={"result": "ok"})  # type: ignore[method-assign]
    body = json.dumps({"cmd": "list_tabs"}).encode("utf-8")
    headers = default_headers(len(body), content_type="Application/JSON")
    reader = make_reader(build_http_request(headers, body))
    writer = make_writer()

    await server._handle_http_request(reader, writer, kontrol, AUTH_TOKEN)

    status, _, _ = parse_response(writer)
    assert status == 200


async def test_handle_http_request_rejects_missing_content_length(
    kontrol: server.ChromeKontrolServer,
) -> None:
    headers = default_headers(2)
    del headers["Content-Length"]
    reader = make_reader(build_http_request(headers, b"{}"))
    writer = make_writer()

    await server._handle_http_request(reader, writer, kontrol, AUTH_TOKEN)

    status, _, payload = parse_response(writer)
    assert status == 411
    assert payload == {"result": "error", "message": "Length Required"}


async def test_handle_http_request_rejects_negative_content_length(
    kontrol: server.ChromeKontrolServer,
) -> None:
    headers = default_headers(2)
    headers["Content-Length"] = "-5"
    reader = make_reader(build_http_request(headers, b"{}"))
    writer = make_writer()

    await server._handle_http_request(reader, writer, kontrol, AUTH_TOKEN)

    status, _, payload = parse_response(writer)
    assert status == 400
    assert payload == {"result": "error", "message": "Bad Request: negative Content-Length"}


async def test_handle_http_request_rejects_non_integer_content_length(
    kontrol: server.ChromeKontrolServer,
) -> None:
    headers = default_headers(2)
    headers["Content-Length"] = "not-a-number"
    reader = make_reader(build_http_request(headers, b"{}"))
    writer = make_writer()

    await server._handle_http_request(reader, writer, kontrol, AUTH_TOKEN)

    status, _, payload = parse_response(writer)
    assert status == 400
    assert payload == {"result": "error", "message": "Bad Request: invalid Content-Length"}


async def test_handle_http_request_rejects_content_length_over_max_message_bytes(
    kontrol: server.ChromeKontrolServer,
) -> None:
    """The declared Content-Length alone triggers 413; no oversized body is actually sent."""
    headers = default_headers(server.MAX_MESSAGE_BYTES + 1)
    reader = make_reader(build_http_request(headers, b""))  # body deliberately not sent
    writer = make_writer()

    await server._handle_http_request(reader, writer, kontrol, AUTH_TOKEN)

    status, _, payload = parse_response(writer)
    assert status == 413
    assert payload == {"result": "error", "message": "Request Entity Too Large"}


async def test_handle_http_request_rejects_incomplete_body(
    kontrol: server.ChromeKontrolServer,
) -> None:
    """Content-Length promises more bytes than actually arrive before EOF."""
    declared_body = b'{"cmd": "get_dom"}'
    headers = default_headers(len(declared_body) + 10)  # promise 10 bytes that never come
    reader = make_reader(build_http_request(headers, declared_body))
    writer = make_writer()

    await server._handle_http_request(reader, writer, kontrol, AUTH_TOKEN)

    status, _, payload = parse_response(writer)
    assert status == 400
    assert payload == {"result": "error", "message": "Bad Request: incomplete body"}


async def test_handle_http_request_rejects_invalid_json_body(
    kontrol: server.ChromeKontrolServer,
) -> None:
    body = b"not json at all"
    headers = default_headers(len(body))
    reader = make_reader(build_http_request(headers, body))
    writer = make_writer()

    await server._handle_http_request(reader, writer, kontrol, AUTH_TOKEN)

    status, _, payload = parse_response(writer)
    assert status == 400
    assert payload == {"result": "error", "message": "Bad Request: invalid JSON body"}


async def test_handle_http_request_rejects_command_failing_validation(
    kontrol: server.ChromeKontrolServer,
) -> None:
    kontrol.send_command = AsyncMock()  # type: ignore[method-assign]
    body = json.dumps({"cmd": "not_a_real_command"}).encode("utf-8")
    headers = default_headers(len(body))
    reader = make_reader(build_http_request(headers, body))
    writer = make_writer()

    await server._handle_http_request(reader, writer, kontrol, AUTH_TOKEN)

    status, _, payload = parse_response(writer)
    assert status == 400
    assert payload == {
        "result": "error",
        "message": "Unknown or missing command: not_a_real_command",
    }
    kontrol.send_command.assert_not_awaited()


async def test_handle_http_request_rejects_headers_exceeding_8kib(
    kontrol: server.ChromeKontrolServer,
) -> None:
    # Deliberately no CRLFCRLF terminator anywhere in this payload: the
    # handler must trip the 8 KiB header-size guard before ever finding one.
    oversized_headers = b"X-Filler: " + b"A" * 9000 + b"\r\n"
    reader = make_reader(b"POST / HTTP/1.1\r\n" + oversized_headers)
    writer = make_writer()

    await server._handle_http_request(reader, writer, kontrol, AUTH_TOKEN)

    status, _, payload = parse_response(writer)
    assert status == 431
    assert payload == {"result": "error", "message": "Request Header Fields Too Large"}


async def test_handle_http_request_returns_408_on_header_read_timeout(
    kontrol: server.ChromeKontrolServer,
) -> None:
    reader = _HeaderTimeoutReader()
    writer = make_writer()

    await server._handle_http_request(reader, writer, kontrol, AUTH_TOKEN)  # type: ignore[arg-type]

    status, _, payload = parse_response(writer)
    assert status == 408
    assert payload == {"result": "error", "message": "Request Timeout"}


async def test_handle_http_request_returns_408_when_header_deadline_expires_naturally(
    kontrol: server.ChromeKontrolServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercises the real deadline-expiry check (`remaining_time <= 0`) inside the header
    accumulation loop, as opposed to the fake-reader-raises-immediately technique used by
    test_handle_http_request_returns_408_on_header_read_timeout above. The running loop's
    time() is patched to jump forward by more than the 10-second header deadline between
    the deadline being set and the first remaining-time check, so the real comparison in
    server.py fires deterministically without an actual 10-second wait.
    """
    loop = asyncio.get_running_loop()
    real_time = loop.time
    call_count = 0

    def fake_time() -> float:
        nonlocal call_count
        call_count += 1
        # The 1st call establishes header_deadline (= now + 10.0); every call
        # after jumps far enough forward that "remaining_time <= 0" is true.
        return real_time() if call_count == 1 else real_time() + 999.0

    monkeypatch.setattr(loop, "time", fake_time)

    reader = asyncio.StreamReader()  # never fed anything; read() is never actually reached
    writer = make_writer()

    await server._handle_http_request(reader, writer, kontrol, AUTH_TOKEN)

    status, _, payload = parse_response(writer)
    assert status == 408
    assert payload == {"result": "error", "message": "Request Timeout"}


async def test_handle_http_request_returns_408_on_body_read_timeout(
    kontrol: server.ChromeKontrolServer,
) -> None:
    declared_body = b'{"cmd": "get_dom"}'
    # Declare more bytes than are actually fed, forcing the "read remaining
    # bytes" branch to execute (remaining > 0) so readexactly() is reached.
    headers = default_headers(len(declared_body) + 5)
    request = build_http_request(headers, declared_body)

    reader = _BodyTimeoutReader()
    reader.feed_data(request)
    reader.feed_eof()
    writer = make_writer()

    await server._handle_http_request(reader, writer, kontrol, AUTH_TOKEN)

    status, _, payload = parse_response(writer)
    assert status == 408
    assert payload == {"result": "error", "message": "Request Timeout"}


async def test_handle_http_request_stops_header_read_on_eof_before_terminator(
    kontrol: server.ChromeKontrolServer,
) -> None:
    """If the client closes the connection mid-headers (EOF with no \\r\\n\\r\\n ever seen),
    the accumulation loop must stop via the "empty chunk" branch rather than looping forever,
    and fall through to whatever the (incomplete) header parse produces downstream — here,
    a POST request with no Content-Length header at all, hence 411.
    """
    request = (
        f"POST / HTTP/1.1\r\n"
        f"Content-Type: application/json\r\n"
        f"{server.HTTP_AUTH_HEADER_NAME}: {AUTH_TOKEN}\r\n"
        f"X-Partial: unterminated"  # deliberately missing the closing \r\n\r\n
    ).encode("utf-8")
    reader = make_reader(request)
    writer = make_writer()

    await server._handle_http_request(reader, writer, kontrol, AUTH_TOKEN)

    status, _, payload = parse_response(writer)
    assert status == 411
    assert payload == {"result": "error", "message": "Length Required"}


async def test_handle_http_request_ignores_header_line_without_a_colon(
    kontrol: server.ChromeKontrolServer,
) -> None:
    """A stray header-section line with no ':' (malformed, or a mangled continuation
    line) is silently skipped rather than raising or being treated as a header.
    """
    kontrol.send_command = AsyncMock(return_value={"result": "ok"})  # type: ignore[method-assign]
    body = json.dumps({"cmd": "list_tabs"}).encode("utf-8")
    request = (
        b"POST / HTTP/1.1\r\n"
        b"GARBAGE LINE WITH NO COLON\r\n"
        + "\r\n".join(f"{k}: {v}" for k, v in default_headers(len(body)).items()).encode("utf-8")
        + b"\r\n\r\n"
        + body
    )
    reader = make_reader(request)
    writer = make_writer()

    await server._handle_http_request(reader, writer, kontrol, AUTH_TOKEN)

    status, _, _ = parse_response(writer)
    assert status == 200


async def test_handle_http_request_reads_body_delivered_in_a_separate_chunk_from_headers(
    kontrol: server.ChromeKontrolServer,
) -> None:
    """Exercises the "remaining > 0" follow-up readexactly() success path: headers arrive
    (and are fully consumed) before any body bytes are available yet, matching how a real
    TCP stream can deliver headers and body in separate segments. A background task feeds
    the body shortly after, so the follow-up read has real (but effectively instant) data
    to receive rather than actually waiting.
    """
    kontrol.send_command = AsyncMock(return_value={"result": "ok"})  # type: ignore[method-assign]
    body = json.dumps({"cmd": "list_tabs"}).encode("utf-8")
    headers = default_headers(len(body))
    header_bytes = ("POST / HTTP/1.1\r\n" + "\r\n".join(f"{k}: {v}" for k, v in headers.items()) + "\r\n\r\n").encode(
        "utf-8"
    )

    reader = asyncio.StreamReader()
    reader.feed_data(header_bytes)  # body deliberately withheld for now

    async def deliver_body_shortly() -> None:
        await asyncio.sleep(0)
        reader.feed_data(body)
        reader.feed_eof()

    feeder = asyncio.create_task(deliver_body_shortly())
    writer = make_writer()

    await server._handle_http_request(reader, writer, kontrol, AUTH_TOKEN)
    await feeder

    status, _, payload = parse_response(writer)
    assert status == 200
    assert payload == {"result": "ok"}


async def test_handle_http_request_logs_and_returns_on_oserror_during_read(
    kontrol: server.ChromeKontrolServer,
) -> None:
    """A raw OSError (e.g. "Connection reset by peer") while reading headers is caught,
    logged, and the connection is cleaned up — no response is attempted on a socket that
    just proved it can no longer be read from.
    """

    class _ResetReader:
        async def read(self, n: int) -> bytes:
            raise OSError("Connection reset by peer")

    reader = _ResetReader()
    writer = make_writer()

    await server._handle_http_request(reader, writer, kontrol, AUTH_TOKEN)  # type: ignore[arg-type]

    writer.write.assert_not_called()
    writer.close.assert_called_once()


async def test_handle_http_request_suppresses_oserror_when_writing_timeout_response(
    kontrol: server.ChromeKontrolServer,
) -> None:
    """If the client is gone by the time a 408 is attempted (write() itself raises OSError),
    that secondary failure must not crash the handler either.
    """
    reader = _HeaderTimeoutReader()
    writer = make_writer()
    writer.write.side_effect = OSError("broken pipe")

    await server._handle_http_request(reader, writer, kontrol, AUTH_TOKEN)  # type: ignore[arg-type]  # must not raise

    writer.close.assert_called_once()


async def test_handle_http_request_closes_writer_in_all_cases(
    kontrol: server.ChromeKontrolServer,
) -> None:
    """The finally block must always close the connection, success or failure."""
    reader = make_reader(b"GET / HTTP/1.1\r\n\r\n")
    writer = make_writer()

    await server._handle_http_request(reader, writer, kontrol, AUTH_TOKEN)

    writer.close.assert_called_once()
    writer.wait_closed.assert_awaited_once()


async def test_handle_http_request_suppresses_oserror_on_writer_close(
    kontrol: server.ChromeKontrolServer,
) -> None:
    """A socket that is already gone by cleanup time (OSError on close) must not crash the handler."""
    reader = make_reader(b"GET / HTTP/1.1\r\n\r\n")
    writer = make_writer()
    writer.close.side_effect = OSError("socket already closed")

    await server._handle_http_request(reader, writer, kontrol, AUTH_TOKEN)  # must not raise
