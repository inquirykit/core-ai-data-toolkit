"""
The Hugging Face Hub cache, as a model's load reads it: which commit of a repo
its weights are (``commit_of``), so a loaded model can say what answered
(models/base.py). Weights are downloaded by the load itself, on first use.
"""

import re
from pathlib import Path
from typing import Optional


def cache_dir() -> Path:
    from huggingface_hub import constants

    return Path(constants.HF_HUB_CACHE)


def repo_dir(repo_id: str) -> Path:
    from huggingface_hub.file_download import repo_folder_name

    return cache_dir() / repo_folder_name(repo_id=repo_id, repo_type="model")


def commit_of(repo_id: str, revision: Optional[str] = None) -> Optional[str]:
    """The commit of ``repo_id`` that ``revision`` (a branch or tag, the default branch when None) names in the Hub
    cache: the one a load from the cache reads. A commit given is itself; None for a local path, a repo not in the
    cache, or anything that cannot be read."""
    if revision and re.fullmatch(r"[0-9a-f]{40}", revision):
        return revision
    try:
        if Path(repo_id).exists():
            return None
        ref = repo_dir(repo_id) / "refs" / (revision or "main")
        commit = ref.read_text().strip() if ref.is_file() else ""
    except Exception:  # a name that is no repo's, a cache that cannot be read
        return None
    return commit if re.fullmatch(r"[0-9a-f]{40}", commit) else None
