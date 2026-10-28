"""
Write the API's OpenAPI schema to a file, for the frontend's generated types.

    python scripts/export_openapi.py frontend/openapi.json

Settings come from their defaults, not from .env, so the schema (which holds
limits such as MAX_INPUT_CHARS) is the same on every machine.
tests/test_openapi_snapshot.py fails when the committed file is out of date.
"""

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def schema() -> dict:
    sys.path.insert(0, str(ROOT))
    from api.app import app

    return app.openapi()


def render(spec: dict) -> str:
    return json.dumps(spec, indent=2, sort_keys=True) + "\n"


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    # Defaults only: no .env, no setup wizard, no database (so no JVM), no enrichers.
    os.environ.update(ENV_FILE=os.devnull, SETUP_WIZARD="false", ARCADEDB_PATH="", ENRICHERS_DIR="")
    Path(sys.argv[1]).write_text(render(schema()))


if __name__ == "__main__":
    main()
