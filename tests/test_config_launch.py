"""
Location   : ChromeKontrol/tests/test_config_launch.py
Purpose    : Unit tests for server._read_config_object(), server._load_auto_launch(),
             server._is_valid_profile_directory_name(), server._load_profiles(), and
             server._resolve_browser_executable() (ISSUES.md P0-1, Phase 2c).
Why        : These are the ingestion points for the "autoLaunch" flag and the "profiles"
             mapping that ChromeKontrolServer._auto_launch_response() later consumes to
             decide whether/how to spawn a browser process. Every "soft failure" mode
             (missing file, invalid JSON, non-boolean autoLaunch, non-dict profiles,
             malformed individual directory names) must degrade to "auto-launch disabled
             / entry ignored" rather than crash the server at startup — this is the
             single most security-sensitive ingestion path in the project because its
             output eventually reaches a subprocess.Popen() argument list. Getting any
             validation wrong here risks either disabling a feature the user configured
             correctly, or letting a malformed value reach the launch path.
Related    : server.py, ISSUES.md P0-1
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

import pytest

import server

# ---------------------------------------------------------------------------
# _read_config_object()
# ---------------------------------------------------------------------------


def test_read_config_object_returns_empty_dict_when_file_missing(tmp_path: Path) -> None:
    missing = tmp_path / "config.json"
    assert server._read_config_object(missing) == {}


def test_read_config_object_missing_file_logs_nothing(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    missing = tmp_path / "config.json"
    with caplog.at_level(logging.WARNING):
        server._read_config_object(missing)
    assert caplog.records == []


def test_read_config_object_handles_read_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    config_file = tmp_path / "config.json"
    config_file.write_text("{}", encoding="utf-8")

    def _raise_oserror(self: Path, encoding: str | None = None, errors: str | None = None) -> str:
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "read_text", _raise_oserror)

    with caplog.at_level(logging.WARNING):
        result = server._read_config_object(config_file)
    assert result == {}
    assert any("Failed to read config file" in rec.getMessage() for rec in caplog.records)


def test_read_config_object_rejects_invalid_json(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    config_file = tmp_path / "config.json"
    config_file.write_text("{not valid json", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        result = server._read_config_object(config_file)
    assert result == {}
    assert any("invalid JSON" in rec.getMessage() for rec in caplog.records)


def test_read_config_object_rejects_non_object_top_level(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        result = server._read_config_object(config_file)
    assert result == {}
    assert any("must be a JSON object" in rec.getMessage() for rec in caplog.records)


def test_read_config_object_returns_full_top_level_dict(tmp_path: Path) -> None:
    config_file = tmp_path / "config.json"
    config_file.write_text(
        json.dumps({"aliases": {"a": "chrome:*"}, "autoLaunch": True, "profiles": {"chrome:*": "Default"}}),
        encoding="utf-8",
    )
    result = server._read_config_object(config_file)
    assert result == {"aliases": {"a": "chrome:*"}, "autoLaunch": True, "profiles": {"chrome:*": "Default"}}


# ---------------------------------------------------------------------------
# _load_auto_launch()
# ---------------------------------------------------------------------------


def test_load_auto_launch_returns_false_when_file_missing(tmp_path: Path) -> None:
    assert server._load_auto_launch(tmp_path / "config.json") is False


def test_load_auto_launch_returns_false_when_key_absent(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"aliases": {}}), encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        result = server._load_auto_launch(config_file)
    assert result is False
    assert caplog.records == []


def test_load_auto_launch_returns_true(tmp_path: Path) -> None:
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"autoLaunch": True}), encoding="utf-8")
    assert server._load_auto_launch(config_file) is True


def test_load_auto_launch_returns_false_for_explicit_false(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"autoLaunch": False}), encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        result = server._load_auto_launch(config_file)
    assert result is False
    assert caplog.records == []


@pytest.mark.parametrize("bad_value", ["true", 1, 0, 1.0, None, [], {}])
def test_load_auto_launch_treats_non_boolean_as_disabled_with_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, bad_value: object
) -> None:
    """Spec: any non-boolean value (string "true", numbers, etc.) is treated as disabled,
    and logs a warning distinct from the "key absent" (silent) case — except JSON `null`,
    which decodes to Python None and is therefore indistinguishable from "key absent"
    (see test_load_auto_launch_returns_false_when_key_absent); it is included here only
    to document that decision, not because it should warn.
    """
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"autoLaunch": bad_value}), encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        result = server._load_auto_launch(config_file)
    assert result is False
    if bad_value is None:
        assert caplog.records == []
    else:
        assert any("must be a boolean" in rec.getMessage() for rec in caplog.records)


# ---------------------------------------------------------------------------
# _is_valid_profile_directory_name()
# ---------------------------------------------------------------------------


def test_is_valid_profile_directory_name_accepts_default() -> None:
    assert server._is_valid_profile_directory_name("Default") is True


def test_is_valid_profile_directory_name_accepts_name_with_space() -> None:
    assert server._is_valid_profile_directory_name("Profile 1") is True


def test_is_valid_profile_directory_name_accepts_hyphen_and_underscore_in_middle() -> None:
    assert server._is_valid_profile_directory_name("Profile-1_test") is True


def test_is_valid_profile_directory_name_rejects_non_string() -> None:
    assert server._is_valid_profile_directory_name(42) is False
    assert server._is_valid_profile_directory_name(None) is False
    assert server._is_valid_profile_directory_name(["Default"]) is False


def test_is_valid_profile_directory_name_rejects_empty_string() -> None:
    assert server._is_valid_profile_directory_name("") is False


def test_is_valid_profile_directory_name_accepts_at_exactly_max_length() -> None:
    assert server._is_valid_profile_directory_name("a" * server.MAX_PROFILE_DIR_LENGTH) is True


def test_is_valid_profile_directory_name_rejects_over_max_length() -> None:
    assert server._is_valid_profile_directory_name("a" * (server.MAX_PROFILE_DIR_LENGTH + 1)) is False


def test_is_valid_profile_directory_name_rejects_disallowed_characters() -> None:
    assert server._is_valid_profile_directory_name("Profile;1") is False
    assert server._is_valid_profile_directory_name("Profile$1") is False
    assert server._is_valid_profile_directory_name("プロファイル") is False


def test_is_valid_profile_directory_name_rejects_leading_hyphen() -> None:
    assert server._is_valid_profile_directory_name("-rf") is False


def test_is_valid_profile_directory_name_rejects_forward_slash() -> None:
    assert server._is_valid_profile_directory_name("a/b") is False


def test_is_valid_profile_directory_name_rejects_backslash() -> None:
    assert server._is_valid_profile_directory_name("a\\b") is False


def test_is_valid_profile_directory_name_rejects_single_dot() -> None:
    """ "." itself would already be rejected by the character-set check (no '.' allowed),
    but the dedicated check exists as defense-in-depth per the spec; verify directly.
    """
    assert server._is_valid_profile_directory_name(".") is False


def test_is_valid_profile_directory_name_rejects_double_dot() -> None:
    assert server._is_valid_profile_directory_name("..") is False


# ---------------------------------------------------------------------------
# _load_profiles()
# ---------------------------------------------------------------------------


def test_load_profiles_returns_empty_dict_when_file_missing(tmp_path: Path) -> None:
    assert server._load_profiles(tmp_path / "config.json") == {}


def test_load_profiles_returns_empty_dict_when_key_absent(tmp_path: Path) -> None:
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"aliases": {}}), encoding="utf-8")
    assert server._load_profiles(config_file) == {}


def test_load_profiles_rejects_non_dict_value(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"profiles": ["not", "a", "dict"]}), encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        result = server._load_profiles(config_file)
    assert result == {}
    assert any('"profiles" must be a JSON object' in rec.getMessage() for rec in caplog.records)


def test_load_profiles_loads_valid_entries(tmp_path: Path) -> None:
    config_file = tmp_path / "config.json"
    config_file.write_text(
        json.dumps(
            {
                "profiles": {
                    "chrome:work@example.com": "Profile 1",
                    "chrome:main@example.com": "Default",
                    "edge:*": "Default",
                }
            }
        ),
        encoding="utf-8",
    )
    result = server._load_profiles(config_file)
    assert result == {
        "chrome:work@example.com": "Profile 1",
        "chrome:main@example.com": "Default",
        "edge:*": "Default",
    }


def test_load_profiles_skips_entry_with_invalid_directory_name_but_keeps_others(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    config_file = tmp_path / "config.json"
    config_file.write_text(
        json.dumps({"profiles": {"chrome:good@example.com": "Default", "chrome:bad@example.com": "../etc"}}),
        encoding="utf-8",
    )
    with caplog.at_level(logging.WARNING):
        result = server._load_profiles(config_file)
    assert result == {"chrome:good@example.com": "Default"}
    assert any("invalid profile directory name" in rec.getMessage() for rec in caplog.records)


def test_load_profiles_skips_entry_with_non_string_directory_value(tmp_path: Path) -> None:
    config_file = tmp_path / "config.json"
    config_file.write_text(
        json.dumps({"profiles": {"chrome:good@example.com": "Default", "chrome:bad@example.com": 12345}}),
        encoding="utf-8",
    )
    result = server._load_profiles(config_file)
    assert result == {"chrome:good@example.com": "Default"}


def test_load_profiles_skips_entry_with_key_over_max_target_length(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    too_long_key = "chrome:" + "a" * server.MAX_TARGET_LENGTH
    config_file = tmp_path / "config.json"
    config_file.write_text(
        json.dumps({"profiles": {"chrome:good@example.com": "Default", too_long_key: "Default"}}),
        encoding="utf-8",
    )
    with caplog.at_level(logging.WARNING):
        result = server._load_profiles(config_file)
    assert result == {"chrome:good@example.com": "Default"}
    assert any("malformed profiles entry" in rec.getMessage() for rec in caplog.records)


def test_load_profiles_normalises_keys_to_nfc(tmp_path: Path) -> None:
    import unicodedata

    nfd_key = "chrome:" + unicodedata.normalize("NFD", "が")
    assert nfd_key != unicodedata.normalize("NFC", nfd_key)

    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"profiles": {nfd_key: "Default"}}), encoding="utf-8")

    result = server._load_profiles(config_file)
    assert result == {unicodedata.normalize("NFC", nfd_key): "Default"}


def _make_profiles_payload(count: int) -> dict[str, str]:
    """count件のユニークな有効profilesエントリを生成するテストヘルパー。

    実在しない架空のユーザー名とRFC 2606予約ドメイン(example.com)を使い、
    実在の個人情報を含めない。
    """
    return {f"chrome:user{i:05d}@example.com": "Default" for i in range(count)}


def test_load_profiles_loads_all_entries_at_exactly_max_count(tmp_path: Path) -> None:
    config_file = tmp_path / "config.json"
    payload = _make_profiles_payload(server.MAX_ALIAS_COUNT)
    config_file.write_text(json.dumps({"profiles": payload}), encoding="utf-8")

    result = server._load_profiles(config_file)
    assert result == payload
    assert len(result) == server.MAX_ALIAS_COUNT


def test_load_profiles_truncates_entries_over_max_count(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    config_file = tmp_path / "config.json"
    payload = _make_profiles_payload(server.MAX_ALIAS_COUNT + 1)
    config_file.write_text(json.dumps({"profiles": payload}), encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        result = server._load_profiles(config_file)

    expected = dict(list(payload.items())[: server.MAX_ALIAS_COUNT])
    assert result == expected
    assert len(result) == server.MAX_ALIAS_COUNT
    assert any("has more than" in rec.getMessage() for rec in caplog.records)


# ---------------------------------------------------------------------------
# _resolve_browser_executable()
# ---------------------------------------------------------------------------


def test_resolve_browser_executable_finds_first_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_which(name: str) -> str | None:
        return "/usr/bin/google-chrome" if name == "google-chrome" else None

    monkeypatch.setattr(shutil, "which", _fake_which)
    assert server._resolve_browser_executable("chrome") == "/usr/bin/google-chrome"


def test_resolve_browser_executable_falls_back_to_second_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_which(name: str) -> str | None:
        return "/usr/bin/google-chrome-stable" if name == "google-chrome-stable" else None

    monkeypatch.setattr(shutil, "which", _fake_which)
    assert server._resolve_browser_executable("chrome") == "/usr/bin/google-chrome-stable"


def test_resolve_browser_executable_falls_back_to_third_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_which(name: str) -> str | None:
        return "/usr/bin/chromium" if name == "chromium" else None

    monkeypatch.setattr(shutil, "which", _fake_which)
    assert server._resolve_browser_executable("chrome") == "/usr/bin/chromium"


def test_resolve_browser_executable_returns_none_when_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert server._resolve_browser_executable("chrome") is None


def test_resolve_browser_executable_edge_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_which(name: str) -> str | None:
        return "/usr/bin/microsoft-edge" if name == "microsoft-edge" else None

    monkeypatch.setattr(shutil, "which", _fake_which)
    assert server._resolve_browser_executable("edge") == "/usr/bin/microsoft-edge"
