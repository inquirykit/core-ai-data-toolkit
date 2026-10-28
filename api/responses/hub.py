"""/hub: search, repos, downloads and what is on disk."""

from typing import List, Literal, Optional


from .common import Out


class HubSearchResult(Out):
    id: str
    pipeline_tag: Optional[str]
    library_name: Optional[str]
    downloads: int
    likes: int
    # ISO 8601
    last_modified: Optional[str]
    # Needs a Hugging Face token and accepted terms
    gated: bool
    tags: List[str]
    on_disk: bool


class HubSearch(Out):
    query: str
    task: Optional[str]
    results: List[HubSearchResult]


class HubFile(Out):
    path: str
    size: Optional[int]


class HubRepo(Out):
    """What a download would fetch."""
    id: str
    sha: Optional[str]
    pipeline_tag: Optional[str]
    gated: bool
    license: Optional[str]
    files: List[HubFile]
    total_bytes: int
    weight_bytes: int
    on_disk: bool


class HubDownload(Out):
    id: str
    repo_id: str
    status: Literal["queued", "running", "done", "failed", "cancelled"]
    # Null until the server knows how much there is
    total_bytes: Optional[int]
    done_bytes: int
    started: Optional[float]
    finished: Optional[float]
    error: Optional[str]


class HubDownloads(Out):
    downloads: List[HubDownload]


class HubLocalRepo(Out):
    id: str
    size_bytes: int
    weight_bytes: int
    files: int
    last_accessed: Optional[float]
    last_modified: Optional[float]
    loaded: bool
    loaded_tasks: List[str]
    # The tasks whose catalog names this model
    tasks: List[str]


class HubLocal(Out):
    repos: List[HubLocalRepo]
    total_bytes: int
