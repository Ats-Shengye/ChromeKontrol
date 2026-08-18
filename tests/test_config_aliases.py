"""
Location   : ChromeKontrol/tests/test_config_aliases.py
Purpose    : Unit tests for server._resolve_config_file_path(), server._is_valid_alias_entry(),
             and server._load_aliases() (ISSUES.md P0-1, Phase 2b).
Why        : _load_aliases() is the sole ingestion point for the "aliases" mapping that
             later feeds ChromeKontrolServer._resolve_by_target(). Every "soft failure"
             mode (missing file, unreadable file, invalid JSON, non-object top-level,
             non-object "aliases", malformed individual entries) must degrade to "no
             aliases, server still works" rather than crash the server at startup, per
             the spec's explicit table of behaviors. Every test uses tmp_path so the
             real ~/.config/chromekontrol/config.json is never touched, mirroring
             test_token_persistence.py's established pattern for the sibling token file.
Related    : server.py, ISSUES.md P0-1
"""

from __future__ import annotations

import json
import logging
import unicodedata
from pathlib import Path

import pytest

import server

# ---------------------------------------------------------------------------
# _resolve_config_file_path()
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_config_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure CHROME_KONTROL_CONFIG_FILE never leaks between tests."""
    monkeypatch.delenv(server.CONFIG_FILE_ENV_VAR, raising=False)


def test_resolve_config_file_path_uses_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    custom = tmp_path / "custom-dir" / "config.json"
    monkeypatch.setenv(server.CONFIG_FILE_ENV_VAR, str(custom))
    assert server._resolve_config_file_path() == custom


def test_resolve_config_file_path_ignores_blank_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """A whitespace-only override is treated as unset, matching _resolve_token_file_path's convention."""
    monkeypatch.setenv(server.CONFIG_FILE_ENV_VAR, "   ")
    assert server._resolve_config_file_path() == server.DEFAULT_CONFIG_FILE


def test_resolve_config_file_path_defaults_when_unset() -> None:
    assert server._resolve_config_file_path() == server.DEFAULT_CONFIG_FILE


# ---------------------------------------------------------------------------
# _is_valid_alias_entry()
# ---------------------------------------------------------------------------


def test_is_valid_alias_entry_accepts_normal_string() -> None:
    assert server._is_valid_alias_entry("仕事") is True


def test_is_valid_alias_entry_rejects_non_string() -> None:
    assert server._is_valid_alias_entry(42) is False
    assert server._is_valid_alias_entry(None) is False
    assert server._is_valid_alias_entry(["a"]) is False


def test_is_valid_alias_entry_rejects_empty_string() -> None:
    assert server._is_valid_alias_entry("") is False


def test_is_valid_alias_entry_accepts_at_exactly_max_length() -> None:
    assert server._is_valid_alias_entry("a" * server.MAX_ALIAS_ENTRY_LENGTH) is True


def test_is_valid_alias_entry_rejects_over_max_length() -> None:
    assert server._is_valid_alias_entry("a" * (server.MAX_ALIAS_ENTRY_LENGTH + 1)) is False


# ---------------------------------------------------------------------------
# _load_aliases()
# ---------------------------------------------------------------------------


def test_load_aliases_returns_empty_dict_when_file_missing(tmp_path: Path) -> None:
    missing = tmp_path / "config.json"
    assert server._load_aliases(missing) == {}


def test_load_aliases_missing_file_logs_nothing(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Spec: a missing config file is the normal/expected case and must not log anything."""
    missing = tmp_path / "config.json"
    with caplog.at_level(logging.WARNING):
        server._load_aliases(missing)
    assert caplog.records == []


def test_load_aliases_does_not_create_missing_directory(tmp_path: Path) -> None:
    """Spec: unlike the token file, the config directory must never be created by reading it."""
    missing_dir_config = tmp_path / "does-not-exist" / "config.json"
    server._load_aliases(missing_dir_config)
    assert not (tmp_path / "does-not-exist").exists()


def test_load_aliases_handles_read_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A file that passes is_file() but fails to read (e.g. a permission error, or a
    race where it is removed between the check and the read) must degrade to "no
    aliases" rather than crash server startup.
    """
    config_file = tmp_path / "config.json"
    config_file.write_text("{}", encoding="utf-8")

    def _raise_oserror(self: Path, encoding: str | None = None, errors: str | None = None) -> str:
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "read_text", _raise_oserror)

    with caplog.at_level(logging.WARNING):
        result = server._load_aliases(config_file)
    assert result == {}
    assert any("Failed to read config file" in rec.getMessage() for rec in caplog.records)


def test_load_aliases_rejects_invalid_json(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    config_file = tmp_path / "config.json"
    config_file.write_text("{not valid json", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        result = server._load_aliases(config_file)
    assert result == {}
    assert any("invalid JSON" in rec.getMessage() for rec in caplog.records)


def test_load_aliases_rejects_non_object_top_level(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps(["not", "an", "object"]), encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        result = server._load_aliases(config_file)
    assert result == {}
    assert any("must be a JSON object" in rec.getMessage() for rec in caplog.records)


def test_load_aliases_rejects_non_dict_aliases_value(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"aliases": ["not", "a", "dict"]}), encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        result = server._load_aliases(config_file)
    assert result == {}
    assert any('"aliases" must be a JSON object' in rec.getMessage() for rec in caplog.records)


def test_load_aliases_returns_empty_dict_when_aliases_key_absent(tmp_path: Path) -> None:
    """A config file with unrelated top-level keys but no "aliases" is valid; just no aliases."""
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"autoLaunch": False}), encoding="utf-8")
    assert server._load_aliases(config_file) == {}


def test_load_aliases_ignores_unknown_top_level_keys(tmp_path: Path) -> None:
    """Spec: unknown top-level keys (e.g. future "autoLaunch"/"profiles") are ignored,
    preserving forward compatibility; "aliases" is still loaded normally alongside them.
    """
    config_file = tmp_path / "config.json"
    config_file.write_text(
        json.dumps({"aliases": {"メイン": "chrome:main@example.com"}, "autoLaunch": True, "profiles": []}),
        encoding="utf-8",
    )
    assert server._load_aliases(config_file) == {"メイン": "chrome:main@example.com"}


def test_load_aliases_loads_valid_entries(tmp_path: Path) -> None:
    config_file = tmp_path / "config.json"
    config_file.write_text(
        json.dumps(
            {
                "aliases": {
                    "仕事": "chrome:work@example.com",
                    "サブ": "chrome:work@example.com",
                    "メイン": "chrome:main@example.com",
                    "Edge": "edge:*",
                }
            }
        ),
        encoding="utf-8",
    )
    result = server._load_aliases(config_file)
    assert result == {
        "仕事": "chrome:work@example.com",
        "サブ": "chrome:work@example.com",
        "メイン": "chrome:main@example.com",
        "Edge": "edge:*",
    }


def test_load_aliases_skips_malformed_entry_but_keeps_others(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """A single malformed entry (non-string value here) is dropped with a warning; the rest
    of the file still loads (spec section 1: "そのエントリのみ無視").
    """
    config_file = tmp_path / "config.json"
    config_file.write_text(
        json.dumps({"aliases": {"良い": "chrome:good@example.com", "悪い": 12345}}),
        encoding="utf-8",
    )
    with caplog.at_level(logging.WARNING):
        result = server._load_aliases(config_file)
    assert result == {"良い": "chrome:good@example.com"}
    assert any("malformed alias entry" in rec.getMessage() for rec in caplog.records)


def test_load_aliases_skips_entry_with_empty_string_value(tmp_path: Path) -> None:
    config_file = tmp_path / "config.json"
    config_file.write_text(
        json.dumps({"aliases": {"良い": "chrome:good@example.com", "空": ""}}),
        encoding="utf-8",
    )
    assert server._load_aliases(config_file) == {"良い": "chrome:good@example.com"}


def test_load_aliases_skips_entry_with_value_over_max_length(tmp_path: Path) -> None:
    config_file = tmp_path / "config.json"
    too_long = "a" * (server.MAX_ALIAS_ENTRY_LENGTH + 1)
    config_file.write_text(
        json.dumps({"aliases": {"良い": "chrome:good@example.com", "長すぎ": too_long}}),
        encoding="utf-8",
    )
    assert server._load_aliases(config_file) == {"良い": "chrome:good@example.com"}


def test_load_aliases_normalises_keys_and_values_to_nfc(tmp_path: Path) -> None:
    """Spec section 3-4: both alias keys and values are NFC-normalized on load."""
    nfd_key = unicodedata.normalize("NFD", "が")  # decomposed: か + combining U+3099
    nfd_value = "chrome:" + unicodedata.normalize("NFD", "が")
    assert nfd_key != unicodedata.normalize("NFC", nfd_key)  # sanity: NFD/NFC really differ here

    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"aliases": {nfd_key: nfd_value}}), encoding="utf-8")

    result = server._load_aliases(config_file)
    assert result == {unicodedata.normalize("NFC", nfd_key): unicodedata.normalize("NFC", nfd_value)}


def test_load_aliases_env_override_is_read(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Integration-style check: _resolve_config_file_path() + _load_aliases() together
    honor CHROME_KONTROL_CONFIG_FILE end-to-end.
    """
    custom = tmp_path / "custom.json"
    custom.write_text(json.dumps({"aliases": {"テスト": "chrome:*"}}), encoding="utf-8")
    monkeypatch.setenv(server.CONFIG_FILE_ENV_VAR, str(custom))

    result = server._load_aliases(server._resolve_config_file_path())
    assert result == {"テスト": "chrome:*"}


# ---------------------------------------------------------------------------
# _load_aliases() エントリ数上限 (Security-Audit.md M-5)
# ---------------------------------------------------------------------------


def _make_aliases_payload(count: int) -> dict[str, str]:
    """count件のユニークな有効エイリアスエントリを生成するテストヘルパー。

    キー・値ともに_is_valid_alias_entry()の条件（文字列・1〜MAX_ALIAS_ENTRY_LENGTH
    文字）を満たすASCII文字列にする。実在しない架空のユーザー名とRFC 2606
    予約ドメイン(example.com)を使い、実在の個人情報を含めない。
    """
    return {f"alias{i:05d}": f"chrome:user{i:05d}@example.com" for i in range(count)}


def test_load_aliases_loads_all_entries_at_exactly_max_count(tmp_path: Path) -> None:
    """Spec: exactly MAX_ALIAS_COUNT valid entries are all loaded (boundary; not truncated)."""
    config_file = tmp_path / "config.json"
    payload = _make_aliases_payload(server.MAX_ALIAS_COUNT)
    config_file.write_text(json.dumps({"aliases": payload}), encoding="utf-8")

    result = server._load_aliases(config_file)
    assert result == payload
    assert len(result) == server.MAX_ALIAS_COUNT


def test_load_aliases_at_exactly_max_count_logs_nothing(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Reaching exactly the limit (not exceeding it) must not trigger the truncation warning."""
    config_file = tmp_path / "config.json"
    payload = _make_aliases_payload(server.MAX_ALIAS_COUNT)
    config_file.write_text(json.dumps({"aliases": payload}), encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        server._load_aliases(config_file)
    assert caplog.records == []


def test_load_aliases_truncates_entries_over_max_count(tmp_path: Path) -> None:
    """Spec: MAX_ALIAS_COUNT + 1 valid entries -> only the first MAX_ALIAS_COUNT (insertion
    order, which json.loads()/dict preserve) are loaded; the rest are ignored.
    """
    config_file = tmp_path / "config.json"
    payload = _make_aliases_payload(server.MAX_ALIAS_COUNT + 1)
    config_file.write_text(json.dumps({"aliases": payload}), encoding="utf-8")

    result = server._load_aliases(config_file)
    expected = dict(list(payload.items())[: server.MAX_ALIAS_COUNT])
    assert result == expected
    assert len(result) == server.MAX_ALIAS_COUNT


def test_load_aliases_truncation_logs_warning_exactly_once(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Spec: the truncation warning fires exactly once (a single `break`), not once per
    entry beyond the limit, even when dozens of entries are dropped.
    """
    config_file = tmp_path / "config.json"
    payload = _make_aliases_payload(server.MAX_ALIAS_COUNT + 50)
    config_file.write_text(json.dumps({"aliases": payload}), encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        result = server._load_aliases(config_file)

    assert len(result) == server.MAX_ALIAS_COUNT
    truncation_records = [rec for rec in caplog.records if "has more than" in rec.getMessage()]
    assert len(truncation_records) == 1
    assert str(server.MAX_ALIAS_COUNT) in truncation_records[0].getMessage()
