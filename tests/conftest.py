import base64
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
os.environ["MODEL_SERVERS_ALLOW_COMMANDS"] = "false"
os.environ["HF_HUB_OFFLINE"] = "1"
# The app evicts an idle model at once, as the route tests expect; tests/test_scheduling.py sets keep and turn itself.
os.environ["MODEL_KEEP_S"] = "0"
os.environ["MODEL_TURN_S"] = "0"
# A throwaway DATA_DIR for everything the service keeps: the database, job and bucket files, model servers saved
# from /admin, evidence captures and their key, the saved keys.
os.environ["DATA_DIR"] = os.path.join(_TMP, "data")
# No job environments, no time stamping authority, every enricher's terms.
os.environ["JOB_ENVS_DIR"] = os.path.join(_TMP, "envs")
os.environ["EVIDENCE_TSA_URL"] = ""
os.environ["ENRICHER_TERMS"] = "open,attribution,non_commercial,paid"
# The saved keys are opened by this master key: nothing reaches the developer's credential store.
os.environ["SECRETS_MASTER_KEY"] = base64.urlsafe_b64encode(b"test-master-key-only-for-tests!!").decode()
os.environ.pop("SECRETS_MASTER_KEY_FILE", None)
os.environ["PYTHON_KEYRING_BACKEND"] = "keyring.backends.null.Keyring"


import pytest


@pytest.fixture(autouse=True)
def fresh_fake_models():
    """FakeModel keeps its instances, log and delays on the class: every test starts without any. Imported here, not
    at the top, so the settings above are in place before anything reads them."""
    from tests.fakes import FakeModel

    FakeModel.reset()


@pytest.fixture
def built_pages(tmp_path, monkeypatch):
    """Stand-ins for the pages frontend/ builds, so tests need no Node build."""
    from api import pages

    for name, title in [("admin", "Admin"), ("setup", "Setup")]:
        (tmp_path / f"{name}.html").write_text(f"<!doctype html><title>AI &amp; Data Toolkit · {title}</title><div id=root></div>")
    monkeypatch.setattr(pages, "STATIC_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def own_db(tmp_path):
    """A throwaway database of its own, for stores built outside the app."""
    from db import Database

    database = Database(str(tmp_path / "toolkit.duckdb"))
    yield database
    database.close()
