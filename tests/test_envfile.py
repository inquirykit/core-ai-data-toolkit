import os
import stat

import envfile


def test_parse_handles_comments_quotes_and_export():
    values = envfile.parse(
        "# comment\n"
        "\n"
        "A=1\n"
        "export B = two\n"
        'C="quoted # not a comment"\n'
        "D=value # trailing comment\n"
        "E=\n"
        "not a setting\n"
    )
    assert values == {"A": "1", "B": "two", "C": "quoted # not a comment", "D": "value", "E": ""}


def test_load_fills_only_unset_or_empty_variables(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("TK_SET=from-file\nTK_EMPTY=from-file\nTK_NEW=from-file\n")
    monkeypatch.setenv("TK_SET", "from-env")
    monkeypatch.setenv("TK_EMPTY", "")
    monkeypatch.delenv("TK_NEW", raising=False)

    assert sorted(envfile.load(path)) == ["TK_EMPTY", "TK_NEW"]
    assert os.environ["TK_SET"] == "from-env"
    assert os.environ["TK_EMPTY"] == "from-file"
    monkeypatch.delenv("TK_NEW")


def test_load_ignores_a_missing_file(tmp_path):
    assert envfile.load(tmp_path / "missing.env") == []


def test_update_keeps_other_lines_and_replaces_every_copy(tmp_path):
    path = tmp_path / ".env"
    path.write_text("# my settings\nMAX_MODELS=2\nAPI_KEY=old\n# API_KEY=commented\nAPI_KEY=older\n")
    envfile.update(path, {"API_KEY": "new", "ADMIN_API_KEY": "admin"})
    assert path.read_text() == (
        "# my settings\nMAX_MODELS=2\nAPI_KEY=new\n# API_KEY=commented\nAPI_KEY=new\n\nADMIN_API_KEY=admin\n"
    )
    assert envfile.parse(path.read_text())["API_KEY"] == "new"


def test_update_creates_a_private_file(tmp_path):
    path = tmp_path / "config" / "settings.env"
    envfile.update(path, {"API_KEY": "secret"})
    assert path.read_text() == "API_KEY=secret\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [p.name for p in path.parent.iterdir()] == ["settings.env"]
