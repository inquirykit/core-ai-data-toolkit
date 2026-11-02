"""The version is written in several files (AGENTS.md, Working here); they all name pyproject.toml's, or this fails."""

import json
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def npm_form(version: str) -> str:
    """The version as npm writes it (semver): 0.2alpha22 is 0.2.0-alpha.22."""
    match = re.fullmatch(r"(\d+)\.(\d+)(?:\.(\d+))?(?:(alpha|beta|rc)(\d+))?", version)
    assert match, f"pyproject.toml's version {version!r} is not one this test can write in npm's form"
    major, minor, micro, pre, number = match.groups()
    return f"{major}.{minor}.{micro or 0}" + (f"-{pre}.{number}" if pre else "")


def test_every_file_names_the_same_version():
    from api.app import APP_VERSION

    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    package = json.loads((ROOT / "frontend" / "package.json").read_text())
    lock = json.loads((ROOT / "frontend" / "package-lock.json").read_text())
    readme = (ROOT / "README.md").read_text()
    found = {
        "APP_VERSION in api/app.py": (APP_VERSION, version),
        "frontend/package.json": (package["version"], npm_form(version)),
        "frontend/package-lock.json": (lock["version"], npm_form(version)),
        "frontend/package-lock.json, its root package": (lock["packages"][""]["version"], npm_form(version)),
        "frontend/openapi.json (npm run gen:api)":
            (json.loads((ROOT / "frontend" / "openapi.json").read_text())["info"]["version"], version),
        "README.md's badge text": (f"version: {version}]" in readme, True),
        "README.md's badge link": (f"/badge/version-{version}-" in readme, True),
    }
    wrong = {where: was for where, (was, wanted) in found.items() if was != wanted}
    assert wrong == {}, f"not pyproject.toml's version {version}: {wrong}"


def test_the_npm_form_of_a_version():
    assert npm_form("0.2alpha22") == "0.2.0-alpha.22"
    assert npm_form("1.4.2rc1") == "1.4.2-rc.1"
    assert npm_form("1.0") == "1.0.0"
