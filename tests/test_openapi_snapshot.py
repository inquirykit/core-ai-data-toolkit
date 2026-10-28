from pathlib import Path

from api import app as app_module
from scripts.export_openapi import render

SNAPSHOT = Path(__file__).resolve().parent.parent / "frontend" / "openapi.json"


def test_frontend_types_match_the_api():
    """The frontend's generated types come from this file; regenerate it when the API changes."""
    assert SNAPSHOT.read_text() == render(app_module.app.openapi()), (
        "frontend/openapi.json is out of date: run `npm run gen:api` in frontend/"
    )
