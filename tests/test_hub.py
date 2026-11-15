from fastapi.testclient import TestClient

from api import app as app_module
from models import hub
from models.families.generic.transformers_model import TransformersModel
from tests.fakes import FakeModel, swapped

COMMIT = "0123456789abcdef0123456789abcdef01234567"


def test_the_commit_a_revision_names_is_read_from_the_cache(monkeypatch, tmp_path):
    # A folder of its own: the test database may sit in tmp_path too.
    repo = tmp_path / "repo"
    (repo / "refs").mkdir(parents=True)
    (repo / "refs" / "main").write_text(COMMIT + "\n")
    monkeypatch.setattr(hub, "repo_dir", lambda repo_id: repo)
    assert hub.commit_of("org/model") == COMMIT
    # A commit is itself; a branch the cache has not got, a local folder and a bad ref are none.
    assert hub.commit_of("org/model", "f" * 40) == "f" * 40
    assert hub.commit_of("org/model", "dev") is None
    assert hub.commit_of(str(repo)) is None
    (repo / "refs" / "main").write_text("not a commit")
    assert hub.commit_of("org/model") is None


def test_a_loaded_model_says_the_commit_its_weights_came_from(monkeypatch):
    monkeypatch.setattr(hub, "commit_of", lambda repo_id, revision=None: COMMIT if repo_id == "org/pinned" else None)
    with swapped(TransformersModel, FakeModel), TestClient(app_module.app) as client:
        r = client.post("/inference", json={"model_id": "org/pinned", "task": "text-classification",
                                            "input": {"text": "hi"}})
        assert r.status_code == 200, r.text
        assert r.json()["revision"] == COMMIT
        details = client.get("/models/status").json()["model_details"]
        assert details["org/pinned:text-classification"]["revision"] == COMMIT
        assert client.delete("/models/org/pinned/text-classification").status_code == 200
