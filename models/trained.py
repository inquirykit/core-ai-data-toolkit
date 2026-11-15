"""
Models a job here trained, served by the job's ID: "autogluon:<job id>", an AutoGluon predictor
(models/families/autogluon/), and "trained:<job id>", a fine-tuned text classifier (models/families/trained/).

Such a model is its job's folder, JOBS_DIR/<job id>. The worker writes the folder's result.json only when the job
succeeded (jobs/worker.py), so a failed job's leftovers are never served; the result names the model ID and its task,
and params.json what it was trained with (its ``name``, if it was given one). Deleting the job deletes the model.

The model lists ask for every such model on each call (``listed``), once per task and family, so what each folder
holds is kept, keyed by the folder's modification time (a job writing its result.json changes it), and the folders
are looked over once a second at most, or at once when a job's folder is made or deleted. A folder changed in the last
few seconds is read again each time, since some file systems keep that time to a few milliseconds only.
"""

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.config import Config

_JOB_ID = re.compile(r"^[0-9a-f]{8,32}$")
# A folder changed this recently is not kept: a result.json written in the same tick would go unseen.
_SETTLED_NS = 2_000_000_000


@dataclass(frozen=True)
class Trained:
    """A model a job here trained: its ID, task, folder, and the job's result and params."""
    model_id: str
    task: str
    workdir: Path
    result: Dict[str, Any] = field(default_factory=dict)
    params: Dict[str, Any] = field(default_factory=dict)

    @property
    def name(self) -> str:
        """The name it was given at training, else what it was trained on."""
        given = self.params.get("name")
        if isinstance(given, str) and given:
            return given
        source = self.params.get("train_file")
        return f"Trained on {Path(source).name}" if isinstance(source, str) else ""


_kept: Dict[str, Tuple[int, Optional[Trained]]] = {}
# The last look over the folders: when, at which JOBS_DIR and its modification time, and what it found.
_SCAN_EVERY_S = 1.0
_scanned: Tuple[float, str, int, List[Tuple[int, Trained]]] = (0.0, "", 0, [])
_lock = threading.Lock()


def _read(workdir: Path) -> Optional[Trained]:
    try:
        result = json.loads((workdir / "result.json").read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(result, dict) or not isinstance(result.get("model_id"), str) or not result.get("task"):
        return None
    try:
        params = json.loads((workdir / "params.json").read_text())
    except (OSError, ValueError):
        params = {}
    return Trained(result["model_id"], result["task"], workdir, result, params if isinstance(params, dict) else {})


def _at(workdir: Path, mtime_ns: int) -> Optional[Trained]:
    key = str(workdir)
    with _lock:
        kept = _kept.get(key)
    if kept is not None and kept[0] == mtime_ns:
        return kept[1]
    found = _read(workdir)
    if time.time_ns() - mtime_ns > _SETTLED_NS:
        with _lock:
            _kept[key] = (mtime_ns, found)
    return found


def find(model_id: str, prefix: str) -> Optional[Trained]:
    """The model ``model_id`` names, ``prefix`` + a job ID, if that job trained it here; else None."""
    if not model_id.startswith(prefix) or not _JOB_ID.match(model_id[len(prefix):]):
        return None
    workdir = Config.JOBS_DIR / model_id[len(prefix):]
    try:
        mtime = workdir.stat().st_mtime_ns
    except OSError:
        return None
    found = _at(workdir, mtime)
    return found if found is not None and found.model_id == model_id else None


def listed(prefix: str, task: str) -> List[Trained]:
    """The models jobs here trained for ``task`` whose IDs start with ``prefix``, the newest first."""
    return [t for _, t in _every() if t.task == task and t.model_id.startswith(prefix)]


def _every() -> List[Tuple[int, Trained]]:
    """Every model jobs here trained, with its folder's modification time, the newest first."""
    global _scanned
    root = str(Config.JOBS_DIR)
    try:
        root_mtime = os.stat(root).st_mtime_ns
    except OSError:
        return []
    with _lock:
        at, where, mtime, found = _scanned
    if where == root and mtime == root_mtime and time.monotonic() - at < _SCAN_EVERY_S:
        return found
    found = _scan(root)
    with _lock:
        _scanned = (time.monotonic(), root, root_mtime, found)
    return found


def _scan(root: str) -> List[Tuple[int, Trained]]:
    folders = []
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                if _JOB_ID.match(entry.name):
                    try:
                        if entry.is_dir():
                            folders.append((entry.path, entry.stat().st_mtime_ns))
                    except OSError:  # deleted as we looked
                        pass
    except OSError:
        return []
    found = [(mtime, _at(Path(path), mtime)) for path, mtime in folders]
    with _lock:  # forget deleted jobs
        present = {path for path, _ in folders}
        for key in [k for k in _kept if k not in present]:
            del _kept[key]
    return sorted(((mtime, t) for mtime, t in found if t is not None), key=lambda m: m[0], reverse=True)
