"""
Request metrics and model events for the /admin dashboard, in the ArcadeDB
database.

Only metadata is recorded here (model, task, outcome, duration); a request's
input and output are its trace in evals/store.py, linked by trace_id and kept
for fewer days. Recording never blocks: rows go onto a queue that one
background thread writes in batches (vectorstore.BatchWriter).
"""

import itertools
import logging
import math
import os
import threading
import time
from typing import Any, Dict, List, Optional, Sequence

from vectorstore import BatchWriter, Database, DocumentStore, on_pool

logger = logging.getLogger(__name__)

REQUEST = "tk_request"
EVENT = "tk_event"
SCHEMA: Dict[str, Dict[str, Any]] = {
    REQUEST: {
        "props": {
            "id": "LONG", "ts": "DOUBLE", "model_id": "STRING", "task": "STRING",
            "status": "STRING",            # ok | client_error | error
            "duration_ms": "DOUBLE",
            "cold": "BOOLEAN",             # the model was loaded for this request
            "error": "STRING",             # exception class name
            "trace_id": "STRING",          # the request's trace (evals/), when tracing is on
            "prompt_tokens": "INTEGER",    # token counts of generation requests, when the model reports them
            "completion_tokens": "INTEGER",
        },
        "indexes": [("id", True), ("ts", False)],
    },
    EVENT: {
        "props": {
            "id": "LONG", "ts": "DOUBLE",
            "kind": "STRING",              # load | load_failed | unload | evict | limits
            "model_key": "STRING", "duration_ms": "DOUBLE", "detail": "STRING", "pid": "INTEGER",
        },
        "indexes": [("id", True), ("ts", False)],
    },
}


def percentile(sorted_values: Sequence[float], q: float) -> Optional[float]:
    """Nearest-rank percentile of an ascending list (None when empty)."""
    if not sorted_values:
        return None
    rank = max(1, math.ceil(len(sorted_values) * q / 100))
    return sorted_values[rank - 1]


def _column(row: Any, name: str) -> Any:
    """``row[name]``, or None for rows without that column."""
    try:
        return row[name]
    except (KeyError, IndexError):
        return None


def summarize(rows: Sequence[Any], since: float, until: float, buckets: int = 60) -> Dict[str, Any]:
    """Totals, per-model stats and a timeline from request rows.

    Each row has ts, model_id, task, status, duration_ms and cold, and may
    have prompt_tokens and completion_tokens.
    """
    bucket_s = max((until - since) / buckets, 1.0)
    timeline = [
        {"start": since + i * bucket_s, "ok": 0, "client_error": 0, "error": 0, "_durations": []}
        for i in range(buckets)
    ]
    per_model: Dict[str, Dict[str, Any]] = {}
    durations: List[float] = []
    counts = {"ok": 0, "client_error": 0, "error": 0}
    tokens = {"prompt_tokens": 0, "completion_tokens": 0}

    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
        durations.append(row["duration_ms"])
        row_tokens = {name: _column(row, name) or 0 for name in tokens}
        for name, n in row_tokens.items():
            tokens[name] += n

        key = f"{row['model_id']}:{row['task']}"
        model = per_model.setdefault(key, {
            "model_key": key, "model_id": row["model_id"], "task": row["task"],
            "requests": 0, "client_errors": 0, "errors": 0, "cold_starts": 0,
            "prompt_tokens": 0, "completion_tokens": 0,
            "last_request": None, "_durations": [],
        })
        model["requests"] += 1
        for name, n in row_tokens.items():
            model[name] += n
        model["client_errors"] += row["status"] == "client_error"
        model["errors"] += row["status"] == "error"
        model["cold_starts"] += bool(row["cold"])
        model["last_request"] = max(model["last_request"] or 0, row["ts"])
        model["_durations"].append(row["duration_ms"])

        index = min(int((row["ts"] - since) // bucket_s), buckets - 1)
        if index >= 0:
            timeline[index][row["status"]] += 1
            timeline[index]["_durations"].append(row["duration_ms"])

    def latency(values: List[float]) -> Dict[str, Optional[float]]:
        values.sort()
        return {"p50_ms": percentile(values, 50), "p95_ms": percentile(values, 95)}

    for model in per_model.values():
        model.update(latency(model.pop("_durations")))
    for point in timeline:
        point.update(latency(point.pop("_durations")))

    total = sum(counts.values())
    return {
        "since": since,
        "until": until,
        "bucket_s": bucket_s,
        "totals": {
            "requests": total,
            "ok": counts["ok"],
            "client_errors": counts["client_error"],
            "errors": counts["error"],
            "error_rate": counts["error"] / total if total else 0.0,
            "requests_per_min": total / ((until - since) / 60),
            **tokens,
            **latency(durations),
        },
        "models": sorted(per_model.values(), key=lambda m: m["requests"], reverse=True),
        "timeline": timeline,
    }


class MonitoringStore(DocumentStore):
    """Records request metrics and model events; see the module docstring."""

    SCHEMA = SCHEMA

    def __init__(self, database: Database, retention_days: float = 7.0, max_queue: int = 10_000):
        super().__init__(database)
        self.retention_s = retention_days * 86400
        self.writer = BatchWriter(self._write, "monitoring-writer", max_queue, batch_size=500)
        # Ids increase in the order rows are recorded (the dashboard pages events by id).
        self._ids = itertools.count(time.time_ns() // 1000)
        self._id_lock = threading.Lock()

    @property
    def dropped(self) -> int:
        return self.writer.dropped

    def _next_id(self) -> int:
        with self._id_lock:
            return next(self._ids)

    # --------------------------------------------------------------- writing
    def record_request(
        self, model_id: str, task: str, status: str, duration_ms: float, cold: bool, error: Optional[str] = None,
        trace_id: Optional[str] = None, prompt_tokens: Optional[int] = None, completion_tokens: Optional[int] = None,
    ) -> None:
        self.writer.put((REQUEST, {
            "id": self._next_id(), "ts": time.time(), "model_id": model_id, "task": task, "status": status,
            "duration_ms": float(duration_ms), "cold": bool(cold), "error": error, "trace_id": trace_id,
            "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
        }))

    def record_event(
        self, kind: str, model_key: str, duration_ms: Optional[float] = None, detail: Optional[str] = None
    ) -> None:
        self.writer.put((EVENT, {
            "id": self._next_id(), "ts": time.time(), "kind": kind, "model_key": model_key,
            "duration_ms": None if duration_ms is None else float(duration_ms), "detail": detail, "pid": os.getpid(),
        }))

    def _write(self, rows: List[tuple]) -> None:
        db = self._db()
        self._commit(db, lambda: [self._insert(db, type_name, doc) for type_name, doc in rows])

    @on_pool
    def prune(self) -> int:
        """Delete rows older than the retention (api/app.py runs this hourly); how many went."""
        cutoff = time.time() - self.retention_s
        return sum(self._delete_older(type_name, "ts", cutoff) for type_name in (REQUEST, EVENT))

    def flush(self) -> None:
        """Block until everything recorded so far is written."""
        self.writer.flush()

    def close(self) -> None:
        """Write what is queued and stop the writer. Recording again restarts it."""
        self.writer.close(timeout=5)

    # --------------------------------------------------------------- reading
    @on_pool
    def summary(self, since: float, until: float, buckets: int = 60) -> Dict[str, Any]:
        rows = self._select(f"SELECT ts, model_id, task, status, duration_ms, cold, prompt_tokens, "
                                      f"completion_tokens FROM {REQUEST} WHERE ts >= :since AND ts <= :until",
                          {"since": since, "until": until})
        return summarize(rows, since, until, buckets)

    @on_pool
    def events(
        self,
        after_id: int = 0,
        limit: int = 100,
        since: Optional[float] = None,
        until: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """Newest first; with ``after_id``, only events newer than that one; ``since``/``until`` bound ``ts``."""
        where, params = _bounds(["id > :after_id"], {"after_id": after_id}, since, until)  # (:after is a reserved word)
        return self._select(f"SELECT FROM {EVENT} WHERE {where} ORDER BY id DESC LIMIT {int(limit)}", params)

    @on_pool
    def recent_requests(
        self,
        limit: int = 50,
        since: Optional[float] = None,
        until: Optional[float] = None,
        status: Optional[str] = None,
        model: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Newest first. ``status`` matches exactly; ``model`` is a case-insensitive substring of the model ID or task."""
        where, params = _bounds(["id IS NOT NULL"], {}, since, until)
        if status:
            where += " AND status = :status"
            params["status"] = status
        if model:
            where += " AND (model_id ILIKE :model OR task ILIKE :model)"
            params["model"] = f"%{model}%"
        return self._select(f"SELECT FROM {REQUEST} WHERE {where} ORDER BY id DESC LIMIT {int(limit)}", params)


def _bounds(clauses: List[str], params: Dict[str, Any], since: Optional[float], until: Optional[float]):
    """The clauses joined with the ``ts`` bounds that are given."""
    if since is not None:
        clauses.append("ts >= :since")
        params["since"] = since
    if until is not None:
        clauses.append("ts <= :until")
        params["until"] = until
    return " AND ".join(clauses), params
