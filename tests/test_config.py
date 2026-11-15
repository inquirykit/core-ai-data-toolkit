"""core/config.py: one table of settings, read from the environment."""

import re
from pathlib import Path

import pytest

from core.config import SETTINGS, Config, Setting, flag, items, memory

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
    (None, memory, "1GB", "1GB"),
    (" 1.5GiB ", memory, "1GB", "1.5GiB"),
    ("50%", memory, "1GB", "50%"),
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
    monkeypatch.setattr("core.config._FROM_FILE", set())
    monkeypatch.delenv("MAX_MODELS", raising=False)
    monkeypatch.delenv("ALLOWED_MODELS", raising=False)
    Config.apply({"MAX_MODELS": "9", "ALLOWED_MODELS": "a/b, c/d"})
    assert Config.MAX_MODELS == 9 and Config.ALLOWED_MODELS == ["a/b", "c/d"]


def test_described_hides_secrets(monkeypatch):
    monkeypatch.setattr(Config, "API_KEY", "s3cret")
    monkeypatch.setattr(Config, "WEBHOOK_SECRET", None)
    described = Config.described()
    assert described["API_KEY"] == "set" and described["WEBHOOK_SECRET"] == "not set"
    assert set(described) == set(SETTINGS) and "s3cret" not in str(described)
    assert isinstance(described["IMAGE_DIR"], str)


@pytest.mark.parametrize("raw, parse, minimum, wrong", [
    ("abc", int, None, "TK_TEST_SETTING='abc' is not a valid value"),
    ("0", int, 1, "TK_TEST_SETTING='0' is below the least allowed, 1"),
    ("-0.5", float, 0, "below the least allowed, 0"),
    ("1000", memory, None, "TK_TEST_SETTING='1000' is not a valid value: give an amount such as 1GB"),
    ("abc", memory, None, "not a valid value"),
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
    assert all(SETTINGS[name].secret for name in ("API_KEY", "ADMIN_API_KEY", "WEBHOOK_SECRET",
                                                   "SETUP_CODE", "SECRETS_MASTER_KEY"))



def test_data_dir_holds_everything_the_service_keeps():
    """One folder to back up or move: every place the service writes is in DATA_DIR, and so are the images unless
    IMAGE_DIR says otherwise."""
    from core.config import LAYOUT

    for name in LAYOUT:
        assert getattr(Config, name).is_relative_to(Config.DATA_DIR), name
    assert Path(SETTINGS["IMAGE_DIR"].default()) == Config.DATA_DIR / "images"


def test_an_empty_folder_setting_is_refused(monkeypatch):
    """Empty is no folder: refused at startup, rather than the working directory."""
    for name in ("DATA_DIR", "IMAGE_DIR"):
        monkeypatch.setenv(name, " ")
        with pytest.raises(ValueError, match=f"{name}=' ' is not a valid value: give a folder"):
            SETTINGS[name].read()


def test_the_images_folder_in_data_dir_is_followed_where_it_links(tmp_path, monkeypatch):
    """DATA_DIR/images may be a link to a folder elsewhere: IMAGE_DIR is where it leads, so images in it are inside."""
    (tmp_path / "photos").mkdir()
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "images").symlink_to(tmp_path / "photos")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("IMAGE_DIR")
    assert SETTINGS["IMAGE_DIR"].read() == (tmp_path / "photos").resolve()
