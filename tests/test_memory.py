from models import memory


def write_repo(cache, repo_id, files):
    """A Hub cache entry as huggingface_hub lays it out: blobs, a snapshot of links, a ref."""
    repo = cache / f"models--{repo_id.replace('/', '--')}"
    snapshot = repo / "snapshots" / "abc123"
    snapshot.mkdir(parents=True)
    (repo / "blobs").mkdir()
    (repo / "refs").mkdir()
    (repo / "refs" / "main").write_text("abc123")
    for i, (name, size) in enumerate(files.items()):
        blob = repo / "blobs" / f"blob{i}"
        blob.write_bytes(b"x" * size)
        (snapshot / name).symlink_to(blob)


def test_cached_weights_counts_weight_files_only(tmp_path):
    write_repo(tmp_path, "org/model", {"config.json": 10, "model.safetensors": 1000})
    write_repo(tmp_path, "org/config-only", {"config.json": 10})
    weights = memory.cached_weights(tmp_path)
    assert set(weights) == {"org/model"}
    assert weights["org/model"] >= 1000
    assert memory.cached_weights(tmp_path / "missing") == {}


def test_local_weights_of_a_model_directory(tmp_path):
    (tmp_path / "model.safetensors").write_bytes(b"x" * 300)
    (tmp_path / "tokenizer.json").write_bytes(b"x" * 50)
    assert memory.local_weights(str(tmp_path)) == 300
    assert memory.local_weights("org/not-a-dir") == 0
