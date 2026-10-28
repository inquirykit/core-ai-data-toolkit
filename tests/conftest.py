import os
import sys
import tempfile

# Make the project root importable and keep tests independent of local env.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DEBUG", "false")
os.environ["API_KEY"] = ""
os.environ["ALLOWED_MODELS"] = ""
os.environ["ADMIN_API_KEY"] = ""
# Each test run gets its own throwaway database and settings file, so a
# developer's .env never leaks into the tests.
_TMP = tempfile.mkdtemp(prefix="ai-toolkit-tests-")
os.environ["ENV_FILE"] = os.path.join(_TMP, "settings.env")
os.environ["SETUP_WIZARD"] = "true"
os.environ.pop("SETUP_CODE", None)
os.environ.setdefault("IMAGE_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "images"))
# No model servers from a developer's file, and no Hub lookups for size estimates.
os.environ["MODEL_SERVERS_FILE"] = os.path.join(_TMP, "model_servers.toml")
os.environ["MODEL_SERVERS_SAVED_FILE"] = os.path.join(_TMP, "model_servers.json")
os.environ["MODEL_SERVERS_ALLOW_COMMANDS"] = "false"
os.environ["HF_HUB_OFFLINE"] = "1"
# The app evicts an idle model at once, as the route tests expect; tests/test_scheduling.py sets keep and turn itself.
os.environ["MODEL_KEEP_S"] = "0"
os.environ["MODEL_TURN_S"] = "0"
# A throwaway ArcadeDB database for /collections.
os.environ["ARCADEDB_PATH"] = os.path.join(_TMP, "arcadedb")
# Throwaway job table, job files and inputs; no job environments.
os.environ["JOBS_DIR"] = os.path.join(_TMP, "jobs")
os.environ["JOB_DATA_DIR"] = os.path.join(_TMP, "datasets")
os.environ["JOB_ENVS_DIR"] = os.path.join(_TMP, "envs")
# Throwaway enricher files and result cache; no API keys from the developer's environment.
os.environ["ENRICHERS_DIR"] = os.path.join(_TMP, "enrichers")
os.environ["ENRICHER_TERMS"] = "open,attribution,non_commercial,paid"
# Throwaway evidence captures and signing key; no time stamping authority.
os.environ["EVIDENCE_DIR"] = os.path.join(_TMP, "evidence")
os.environ["EVIDENCE_KEY_FILE"] = os.path.join(_TMP, "evidence", "keys", "ed25519.pem")
os.environ["EVIDENCE_TSA_URL"] = ""


import pytest


@pytest.fixture
def built_pages(tmp_path, monkeypatch):
    """Stand-ins for the pages frontend/ builds, so tests need no Node build."""
    from api import pages

    for name, title in [("admin", "Admin"), ("setup", "Setup"), ("playground", "Playground")]:
        (tmp_path / f"{name}.html").write_text(f"<!doctype html><title>AI &amp; Data Toolkit · {title}</title><div id=root></div>")
    monkeypatch.setattr(pages, "STATIC_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def arcade(tmp_path):
    """A throwaway ArcadeDB database of its own, for stores built outside the app."""
    from vectorstore import Database

    database = Database(str(tmp_path / "arcadedb"))
    database.start_jvm()  # pytest runs tests on the main thread
    yield database
    database.close()
