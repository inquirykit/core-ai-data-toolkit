"""Each fact is documented in one place (AGENTS.md, Working here), and the places that are not generated hold what they
should: a route's docstring is its documentation in /docs, a router has its row in STRUCTURE.md's API table, a folder
its line in STRUCTURE.md's map. Settings are env.example's (tests/test_config.py)."""

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
STRUCTURE = (ROOT / "STRUCTURE.md").read_text()
# Source files, by the folders that hold them; tests and writing are not mapped.
SOURCE = (".py", ".ts", ".tsx", ".sh")
UNMAPPED = ("tests/", "docs/", "research_notes/", "reports/")


def _section(heading: str) -> str:
    start = STRUCTURE.index(f"## {heading}\n")
    end = STRUCTURE.find("\n## ", start + 1)
    return STRUCTURE[start:end if end != -1 else None]


def test_every_route_says_what_it_does():
    """/docs is the routes' reference: a route without a docstring is one nobody documented."""
    from api import app as app_module

    schema = app_module.app.openapi()
    missing = [
        f"{method.upper()} {path}"
        for path, item in schema["paths"].items()
        for method, operation in item.items()
        if method in ("get", "post", "put", "patch", "delete") and not (operation.get("description") or "").strip()
    ]
    assert missing == [], "give these routes a docstring (it is what /docs shows):\n" + "\n".join(missing)


def test_every_router_has_its_row_in_the_api_table():
    table = _section("API")
    routers = sorted(p.name for p in (ROOT / "api").glob("*_routes.py"))
    assert [name for name in routers if f"`{name}`" not in table] == [], "add the router to STRUCTURE.md's API table"


def test_every_folder_of_source_files_has_its_line_in_the_map():
    try:
        files = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.split()
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    folders = {
        str(Path(f).parent) for f in files
        if f.endswith(SOURCE) and not f.startswith(UNMAPPED) and str(Path(f).parent) != "."
    }
    mapped = _section("Directory Structure")
    missing = sorted(folder for folder in folders if not re.search(rf"(^|[\s/,]){re.escape(Path(folder).name)}/", mapped, re.M))
    assert missing == [], "give these folders a line in STRUCTURE.md's map:\n" + "\n".join(missing)
