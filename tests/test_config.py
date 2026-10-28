"""config.py: one table of settings, read from the environment."""

import re
from pathlib import Path

import pytest

from config import SETTINGS, Config, Setting, flag, items

ROOT = Path(__file__).resolve().parent.parent


def test_env_example_mentions_every_setting():
    text = (ROOT / "env.example").read_text()
    assert [name for name in SETTINGS if not re.search(rf"\b{name}\b", text)] == []


@pytest.mark.parametrize("raw, parse, default, expected", [
    (None, int, 3, 3),
    ("", int, 3, 3),          # an empty number is unset (compose passes "${X:-}")
    ("7", int, 3, 7),
    ("", items, "a,b", []),   # an empty list is empty: "off"
    (None, items, "a, b", ["a", "b"]),
    ("", flag, True, False),
    ("yes", flag, False, True),
])
def test_values_are_parsed_and_defaulted(monkeypatch, raw, parse, default, expected):
    spec = Setting(parse, default, name="TK_TEST_SETTING")
    if raw is None:
        monkeypatch.delenv("TK_TEST_SETTING", raising=False)
    else:
        monkeypatch.setenv("TK_TEST_SETTING", raw)
    assert spec.read() == expected


def test_apply_reads_the_named_settings_again(monkeypatch):
    monkeypatch.setattr(Config, "MAX_MODELS", Config.MAX_MODELS)
    monkeypatch.setattr(Config, "ALLOWED_MODELS", Config.ALLOWED_MODELS)
    monkeypatch.setattr("config._FROM_FILE", set())
    monkeypatch.delenv("MAX_MODELS", raising=False)
    monkeypatch.delenv("ALLOWED_MODELS", raising=False)
    Config.apply({"MAX_MODELS": "9", "ALLOWED_MODELS": "a/b, c/d"})
    assert Config.MAX_MODELS == 9 and Config.ALLOWED_MODELS == ["a/b", "c/d"]


def test_described_hides_secrets(monkeypatch):
    monkeypatch.setattr(Config, "API_KEY", "s3cret")
    monkeypatch.setattr(Config, "JUDGE_API_KEY", None)
    described = Config.described()
    assert described["API_KEY"] == "set" and described["JUDGE_API_KEY"] == "not set"
    assert set(described) == set(SETTINGS) and "s3cret" not in str(described)
    assert isinstance(described["IMAGE_DIR"], str)


@pytest.mark.parametrize("raw, parse, minimum, wrong", [
    ("abc", int, None, "TK_TEST_SETTING='abc' is not a valid value"),
    ("0", int, 1, "TK_TEST_SETTING='0' is below the least allowed, 1"),
    ("-0.5", float, 0, "below the least allowed, 0"),
])
def test_a_bad_value_names_its_variable(monkeypatch, raw, parse, minimum, wrong):
    monkeypatch.setenv("TK_TEST_SETTING", raw)
    with pytest.raises(ValueError, match=wrong):
        Setting(parse, 3, minimum=minimum, name="TK_TEST_SETTING").read()


def test_a_bad_secret_is_not_shown(monkeypatch):
    monkeypatch.setenv("TK_TEST_SETTING", "shh")
    with pytest.raises(ValueError, match=r"TK_TEST_SETTING=\(hidden\)"):
        Setting(int, 3, secret=True, name="TK_TEST_SETTING").read()


def test_secret_names_are_the_keys():
    assert {"API_KEY", "ADMIN_API_KEY", "WEBHOOK_SECRET", "JUDGE_API_KEY", "SETUP_CODE"} <= set(Config.secret_names())


def test_more_than_one_worker_needs_no_database(monkeypatch):
    monkeypatch.setattr(Config, "WORKERS", 2)
    monkeypatch.setattr(Config, "ARCADEDB_PATH", "data/arcadedb")
    assert any("WORKERS > 1" in reason for reason in Config.fatal())
    monkeypatch.setattr(Config, "ARCADEDB_PATH", "")
    assert Config.fatal() == []
