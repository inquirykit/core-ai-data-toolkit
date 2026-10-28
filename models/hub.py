"""
The Hugging Face Hub for the admin dashboard: search it, look a repo up,
download its weights ahead of time, and see or delete what is on disk.

A download is a kind of task (jobs/tasks.py): MAX_HUB_DOWNLOADS run at once,
the rest wait, the most urgent first, then the oldest; one
queued or running when the service stops goes on when it is back. Each runs in its own
process (``snapshot_download`` in a child Python), so cancelling one is
killing that process; the Hub cache keeps the partial blobs and the next
download of the repo resumes them. Progress is the size of the repo's cache
folder against the repo's total file size. Everything here is
for the admin routes (api/hub_routes.py); models still download on their own
when first loaded, as before.
"""

import asyncio
import logging
import os
import stat
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import procs
from errors import Conflict, NotFound
from jobs.store import FINISHED, SUCCEEDED
from jobs.tasks import Kind, TaskFailed, TaskRunner

from . import tasks
from .memory import _WEIGHT_SUFFIXES

logger = logging.getLogger(__name__)

_DOWNLOAD_SCRIPT = "import sys\nfrom huggingface_hub import snapshot_download\nsnapshot_download(sys.argv[1])\n"


def offline() -> bool:
    return os.environ.get("HF_HUB_OFFLINE", "").lower() in ("1", "true", "yes")


def cache_dir() -> Path:
    from huggingface_hub import constants

    return Path(constants.HF_HUB_CACHE)


def repo_dir(repo_id: str) -> Path:
    from huggingface_hub.file_download import repo_folder_name

    return cache_dir() / repo_folder_name(repo_id=repo_id, repo_type="model")


def _is_weight(name: str) -> bool:
    return name.endswith(_WEIGHT_SUFFIXES)


def _dir_bytes(path: Path) -> int:
    """Bytes of the regular files under ``path`` (symlinks, the snapshots, not counted again)."""
    total = 0
    for root, _, files in os.walk(path):
        for name in files:
            try:
                entry = os.lstat(os.path.join(root, name))
            except OSError:
                continue
            if not stat.S_ISLNK(entry.st_mode):
                total += entry.st_size
    return total


# ------------------------------------------------------------------- search
def search(query: str, task: Optional[str] = None, limit: int = 30) -> List[Dict[str, Any]]:
    """Models on the Hub matching ``query`` (and the task's pipeline tag), most downloaded first."""
    from huggingface_hub import HfApi

    models = HfApi().list_models(
        search=query or None,
        pipeline_tag=tasks.hub_tags().get(task) if task else None,
        sort="downloads",
        limit=limit,
    )
    results = []
    for m in models:
        results.append({
            "id": m.id,
            "pipeline_tag": m.pipeline_tag,
            "library_name": getattr(m, "library_name", None),
            "downloads": m.downloads or 0,
            "likes": m.likes or 0,
            "last_modified": m.last_modified.isoformat() if m.last_modified else None,
            "gated": bool(m.gated),
            "tags": [t for t in (m.tags or []) if ":" not in t][:12],
        })
    return results


def repo_info(repo_id: str) -> Dict[str, Any]:
    """A repo's files with sizes, its total and weight bytes, license and gating. Raises the Hub's errors."""
    from huggingface_hub import HfApi

    info = HfApi().model_info(repo_id, files_metadata=True)
    files = [{"path": s.rfilename, "size": s.size} for s in (info.siblings or [])]
    license_tag = next((t[len("license:"):] for t in (info.tags or []) if t.startswith("license:")), None)
    return {
        "id": info.id,
        "sha": info.sha,
        "pipeline_tag": info.pipeline_tag,
        "gated": bool(info.gated),
        "license": license_tag,
        "files": files,
        "total_bytes": sum(f["size"] or 0 for f in files),
        "weight_bytes": sum(f["size"] or 0 for f in files if _is_weight(f["path"])),
    }


# ------------------------------------------------------------------ on disk
def local_repos() -> List[Dict[str, Any]]:
    """Model repos in the Hub cache: size, weight bytes, file count, last access and change."""
    from huggingface_hub import scan_cache_dir

    try:
        repos = scan_cache_dir(cache_dir()).repos
    except Exception:
        logger.debug("Could not scan the Hub cache", exc_info=True)
        return []
    out = []
    for repo in repos:
        if repo.repo_type != "model":
            continue
        # Revisions share blobs; count each once.
        weights = {f.blob_path: f.size_on_disk for rev in repo.revisions for f in rev.files if _is_weight(f.file_name)}
        out.append({
            "id": repo.repo_id,
            "size_bytes": repo.size_on_disk,
            "weight_bytes": sum(weights.values()),
            "files": repo.nb_files,
            "last_accessed": repo.last_accessed,
            "last_modified": repo.last_modified,
        })
    return sorted(out, key=lambda r: r["id"].lower())


def delete_repo(repo_id: str) -> int:
    """Delete every revision of the repo from the Hub cache; returns the bytes freed. NotFound if not there."""
    from huggingface_hub import scan_cache_dir

    info = scan_cache_dir(cache_dir())
    repo = next((r for r in info.repos if r.repo_type == "model" and r.repo_id == repo_id), None)
    if repo is None:
        raise NotFound("Not in the Hub cache")
    strategy = info.delete_revisions(*[rev.commit_hash for rev in repo.revisions])
    strategy.execute()
    return strategy.expected_freed_size


# ---------------------------------------------------------------- downloads
# Seconds between SIGTERM and SIGKILL when a download is stopped.
STOP_GRACE = 5.0


class Downloads:
    """Downloads of Hub repos: the "download" kind of task, a child process each, ``max_running`` at a time."""

    def __init__(self, tasks: TaskRunner, max_running: int = 2, poll_s: float = 1.0):
        self.tasks = tasks
        self.poll_s = poll_s
        # Task id -> (bytes in the repo's cache folder, its total or None), from the download's start on.
        self._bytes: Dict[str, Tuple[int, Optional[int]]] = {}
        self._processes: Dict[str, asyncio.subprocess.Process] = {}
        # A download the service stopped under goes on from the files it has, once the service is back.
        tasks.add(Kind("download", run=self._run, stop=self._stop, concurrency=max_running, on_interrupt="resume"))

    def _public(self, task: Dict[str, Any]) -> Dict[str, Any]:
        """The task as GET /hub/downloads shows it ("done" for succeeded)."""
        result = task["result"] or {}
        done, total = self._bytes.get(task["id"], (result.get("done_bytes", 0), result.get("total_bytes")))
        return {"id": task["id"], "repo_id": task["ref"], "status": "done" if task["status"] == SUCCEEDED else task["status"],
                "total_bytes": total, "done_bytes": done, "started": task["started"], "finished": task["finished"],
                "error": task["error"]}

    async def list(self) -> List[Dict[str, Any]]:
        """Every download not yet dismissed, newest first."""
        return [self._public(t) for t in await self.tasks.store.list(limit=200, kind="download")]

    async def active_for(self, repo_id: str) -> Optional[Dict[str, Any]]:
        """The repo's download that is queued or running, if any."""
        task = await self.tasks.find("download", repo_id)
        return task if task is not None and task["status"] not in FINISHED else None

    async def start(self, repo_id: str, priority: str = "normal") -> Dict[str, Any]:
        """Queue a download of ``repo_id``; Conflict if one is already queued or running."""
        if await self.active_for(repo_id) is not None:
            raise Conflict(f"{repo_id} is already being downloaded")
        return self._public(await self.tasks.submit("download", ref=repo_id, priority=priority))

    async def cancel(self, download_id: str) -> None:
        """Stop a queued or running download, or dismiss a finished one. NotFound if unknown."""
        task = await self.tasks.store.get(download_id)
        if task is None or task["kind"] != "download":
            raise NotFound("No such download")
        if task["status"] in FINISHED:
            await self.tasks.store.delete(download_id)
            self._bytes.pop(download_id, None)
        else:
            await self.tasks.cancel(download_id)

    async def _spawn(self, repo_id: str) -> asyncio.subprocess.Process:
        # Without the service's keys and secrets (procs.hide); HF_TOKEN is none of them, so a gated repo still downloads.
        env = procs.child_env(HF_HUB_DISABLE_PROGRESS_BARS="1")
        env.pop("HF_HUB_OFFLINE", None)
        return await asyncio.create_subprocess_exec(
            sys.executable, "-c", _DOWNLOAD_SCRIPT, repo_id,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE, env=env, start_new_session=True,
        )

    async def _run(self, task: Dict[str, Any]) -> Dict[str, Any]:
        """One download, from the repo's size to its process's exit: the bytes it has, or TaskFailed with the error."""
        id, repo_id = task["id"], task["ref"]
        folder = repo_dir(repo_id)
        total = None
        try:
            total = (await asyncio.to_thread(repo_info, repo_id))["total_bytes"]
        except Exception as e:
            logger.info("No size for %s: %s", repo_id, e)
        self._bytes[id] = (await asyncio.to_thread(_dir_bytes, folder), total)
        process = procs.track(await self._spawn(repo_id))
        self._processes[id] = process
        try:
            if (await self.tasks.get(id))["cancel"]:  # cancelled before there was a process to stop: it ends as cancelled
                await procs.stop(process, STOP_GRACE)
            errors = asyncio.create_task(process.stderr.read())
            while True:
                try:
                    await asyncio.wait_for(process.wait(), self.poll_s)
                    break
                except asyncio.TimeoutError:
                    done = await asyncio.to_thread(_dir_bytes, folder)
                    self._bytes[id] = (done, total)
                    await self.tasks.store.progress(id, done / total if total else None, "downloading")
            stderr = (await errors).decode(errors="replace")[-2000:]
        except asyncio.CancelledError:
            # The service is stopping: the partial files stay for the next download.
            await asyncio.shield(procs.stop(process, STOP_GRACE))
            raise
        finally:
            self._processes.pop(id, None)
            procs.untrack(process)
        done = await asyncio.to_thread(_dir_bytes, folder)
        if process.returncode == 0:
            done = max(done, total or 0)
            self._bytes[id] = (done, total)
            return {"total_bytes": total, "done_bytes": done}
        self._bytes[id] = (done, total)
        # A cancel killed it: the task runner sees the cancel and marks it cancelled.
        last = [line for line in stderr.strip().splitlines() if line.strip()]
        raise TaskFailed((last[-1] if last else f"exit code {process.returncode}")[:500])

    async def _stop(self, task: Dict[str, Any]) -> None:
        process = self._processes.get(task["id"])
        if process is not None:
            await procs.stop(process, STOP_GRACE)
