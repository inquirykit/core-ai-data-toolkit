"""
Request metrics and model events for the /admin dashboard, in the ArcadeDB
database.

Only metadata is recorded here (model, task, outcome, duration); a request's
input and output are its trace in evals/store.py, linked by trace_id and kept
for fewer days. Recording never blocks: rows go onto a queue that one
background thread writes in batches (db.BatchWriter).

Each batch also adds its requests to an hourly summary (tk_request_hour: per
hour, model and task, the counts, tokens and a latency histogram), in the same
transaction, so it is always current. A range of at most RAW_SPAN_S within the
request history is summarized from its requests, at any resolution; a longer
or older one from the hourly summary, in whole hours, which is kept for
HOURS_KEPT_DAYS. A day's requests are thousands of rows, its hours a few dozen.
"""

import json
import logging
import math
import os
import time
from collections import Counter
from typing import Any, Dict, Iterable, List, Optional, Tuple

from db import Database, QueuedStore, Where, on_pool
from db.engine import content

logger = logging.getLogger(__name__)

REQUEST = "tk_request"
HOUR = "tk_request_hour"
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
    HOUR: {
        "props": {
            "hour": "LONG",                # Unix time the hour starts at
            "model_id": "STRING", "task": "STRING",
            "ok": "INTEGER", "client_error": "INTEGER", "error": "INTEGER",  # its requests, by status
            "cold_starts": "INTEGER", "prompt_tokens": "LONG", "completion_tokens": "LONG",
            "last_request": "DOUBLE",
            "latency": "STRING",           # the durations' histogram, JSON {bucket: count} (see latency_bucket)
        },
        "indexes": [("hour, model_id, task", True), ("hour", False)],
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
REQUEST_COLUMNS = "ts, model_id, task, status, duration_ms, cold, prompt_tokens, completion_tokens"
# Ranges up to this long are summarized from their requests (when the request history still has them).
RAW_SPAN_S = 6 * 3600
# The hourly summary is kept this long, or as long as the requests when that is longer.
HOURS_KEPT_DAYS = 90

# Latency as a histogram: a duration of ``ms`` falls in bucket ceil(log(ms, GAMMA)), and a bucket reads as a value
# within ACCURACY of every duration in it (as DDSketch does). Histograms add up, so percentiles over many hours are
# as close as over the requests themselves.
ACCURACY = 0.01
GAMMA = (1 + ACCURACY) / (1 - ACCURACY)
FASTEST_MS = 0.001  # shorter durations count as this

STATUSES = ("ok", "client_error", "error")
# What a tally adds up, besides its latency histogram and last request.
SUMS = (*STATUSES, "cold_starts", "prompt_tokens", "completion_tokens")


def latency_bucket(ms: float) -> int:
    return math.ceil(math.log(max(ms, FASTEST_MS), GAMMA))


def percentile(histogram: Dict[int, int], q: float) -> Optional[float]:
    """Nearest-rank percentile of a latency histogram, within ACCURACY (None when empty)."""
    total = sum(histogram.values())
    if not total:
        return None
    rank, seen = max(1, math.ceil(total * q / 100)), 0
    for bucket in sorted(histogram):
        seen += histogram[bucket]
        if seen >= rank:
            return 2 * GAMMA ** bucket / (GAMMA + 1)
    return None


def _column(row: Any, name: str) -> Any:
    """``row[name]``, or None for rows without that column."""
    try:
        return row[name]
    except (KeyError, IndexError):
        return None


def tally() -> Dict[str, Any]:
    """Nothing counted yet."""
    return {**dict.fromkeys(SUMS, 0), "last_request": None, "latency": Counter()}


def add(into: Dict[str, Any], part: Dict[str, Any]) -> None:
    """Count ``part`` (a request, an hour or another tally) into ``into``."""
    for name in SUMS:
        into[name] += part[name] or 0
    if part["last_request"] is not None:
        into["last_request"] = max(into["last_request"] or 0, part["last_request"])
    into["latency"].update(part["latency"])


def of_request(row: Any) -> Dict[str, Any]:
    """A request row as a tally of one, at its time. Rows from before the token columns existed lack them."""
    return {
        "at": row["ts"], "model_id": row["model_id"], "task": row["task"],
        **{status: int(row["status"] == status) for status in STATUSES},
        "cold_starts": int(bool(row["cold"])),
        "prompt_tokens": _column(row, "prompt_tokens"), "completion_tokens": _column(row, "completion_tokens"),
        "last_request": row["ts"], "latency": {latency_bucket(row["duration_ms"]): 1},
    }


def of_hour(row: Dict[str, Any]) -> Dict[str, Any]:
    """An hourly summary row as a tally, at its hour's start."""
    return {**row, "at": row["hour"], "latency": {int(b): n for b, n in json.loads(row["latency"] or "{}").items()}}


def hours_of(rows: Iterable[Any]) -> Dict[Tuple[int, str, str], Dict[str, Any]]:
    """Request rows tallied by (hour, model_id, task)."""
    hours: Dict[Tuple[int, str, str], Dict[str, Any]] = {}
    for row in rows:
        key = (int(row["ts"] // 3600 * 3600), row["model_id"], row["task"])
        add(hours.setdefault(key, tally()), of_request(row))
    return hours


def hourly_buckets(since: float, until: float, buckets: int) -> Tuple[float, float, int]:
    """A timeline of at most ``buckets`` points of whole hours, from the hour ``since`` falls in: where it starts,
    how long each point is and how many there are."""
    start = since // 3600 * 3600
    bucket_s = max(1, math.ceil((until - start) / buckets / 3600)) * 3600
    return start, bucket_s, math.ceil((until - start) / bucket_s)


def summarize(parts: Iterable[Dict[str, Any]], since: float, until: float, buckets: int = 60,
              bucket_s: Optional[float] = None) -> Dict[str, Any]:
    """Totals, per-model stats and a timeline of ``buckets`` points from tallies (of_request, of_hour).

    The points start at ``since``, each ``bucket_s`` long (an even share of the range when left out); a part
    counts in the point its ``at`` falls in.
    """
    bucket_s = bucket_s or max((until - since) / buckets, 1.0)
    timeline = [tally() for _ in range(buckets)]
    per_model: Dict[str, Dict[str, Any]] = {}
    total = tally()
    for part in parts:
        add(total, part)
        key = f"{part['model_id']}:{part['task']}"
        if key not in per_model:
            per_model[key] = {"model_key": key, "model_id": part["model_id"], "task": part["task"], **tally()}
        add(per_model[key], part)
        index = min(int((part["at"] - since) // bucket_s), buckets - 1)
        if index >= 0:
            add(timeline[index], part)

    def latency(t: Dict[str, Any]) -> Dict[str, Optional[float]]:
        return {"p50_ms": percentile(t["latency"], 50), "p95_ms": percentile(t["latency"], 95)}

    def counts(t: Dict[str, Any]) -> Dict[str, Any]:
        return {"requests": sum(t[s] for s in STATUSES), "client_errors": t["client_error"], "errors": t["error"],
                "prompt_tokens": t["prompt_tokens"], "completion_tokens": t["completion_tokens"], **latency(t)}

    totals = counts(total)
    return {
        "since": since,
        "until": until,
        "bucket_s": bucket_s,
        "totals": {
            **totals,
            "ok": total["ok"],
            "error_rate": total["error"] / totals["requests"] if totals["requests"] else 0.0,
            "requests_per_min": totals["requests"] / ((until - since) / 60),
        },
        "models": sorted(({"model_key": m["model_key"], "model_id": m["model_id"], "task": m["task"], **counts(m),
                           "cold_starts": m["cold_starts"], "last_request": m["last_request"]}
                          for m in per_model.values()), key=lambda m: m["requests"], reverse=True),
        "timeline": [{"start": since + i * bucket_s, **{s: p[s] for s in STATUSES}, **latency(p)}
                     for i, p in enumerate(timeline)],
    }


def _hour_row(key: Tuple[int, str, str], counted: Dict[str, Any]) -> Dict[str, Any]:
    hour, model_id, task = key
    return {"hour": hour, "model_id": model_id, "task": task, **{name: counted[name] for name in SUMS},
            "last_request": counted["last_request"], "latency": json.dumps(counted["latency"], separators=(",", ":"))}


class MonitoringStore(QueuedStore):
    """Records request metrics and model events; see the module docstring. The dashboard pages events by id."""

    SCHEMA = SCHEMA
    RETAINED = ((REQUEST, "ts"), (EVENT, "ts"))
    MIGRATIONS = (("monitoring-hourly-summary", "_summarize_past_hours"),)

    def __init__(self, database: Database, retention_days: float = 7.0, max_queue: int = 10_000):
        super().__init__(database, "monitoring-writer", retention_days, max_queue, batch_size=500)

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

    def _write(self, items: List[Any]) -> None:
        hours = hours_of(doc for type_name, doc in items if type_name == REQUEST)
        db = self._db()

        def work() -> None:
            for type_name, doc in items:
                self._insert(db, type_name, doc)
            self._add_hours(db, hours)

        self._commit(db, work)

    def _add_hours(self, db, hours: Dict[Tuple[int, str, str], Dict[str, Any]]) -> None:
        """Count ``hours`` (hours_of) into the hourly summary, in the caller's transaction."""
        if not hours:
            return
        rows = self._rows(db, f"SELECT FROM {HOUR} WHERE hour IN :hours", {"hours": sorted({k[0] for k in hours})})
        kept = {(r["hour"], r["model_id"], r["task"]): r for r in rows}
        for key, counted in hours.items():
            if key not in kept:
                self._insert(db, HOUR, _hour_row(key, counted))
                continue
            merged = tally()
            add(merged, counted)
            add(merged, of_hour(kept[key]))
            row = _hour_row(key, merged)
            db.command("sql", f"UPDATE {HOUR} CONTENT {content(row)} WHERE hour = :hour AND model_id = :model_id "
                              f"AND task = :task", row)

    def _summarize_past_hours(self, db) -> None:
        """The hourly summary of the requests recorded before there was one, a day of them at a time."""
        bounds = self._rows(db, f"SELECT min(ts) AS first, max(ts) AS last FROM {REQUEST}")
        if not bounds or bounds[0].get("first") is None:
            return
        hours: Dict[Tuple[int, str, str], Dict[str, Any]] = {}
        day = bounds[0]["first"] // 86400 * 86400
        while day <= bounds[0]["last"]:
            hours.update(hours_of(self._rows(db, f"SELECT {REQUEST_COLUMNS} FROM {REQUEST} WHERE ts >= :a AND ts < :b",
                                             {"a": day, "b": day + 86400})))
            day += 86400

        def work() -> None:
            db.command("sql", f"DELETE FROM {HOUR}")  # left by a run that failed part way
            for key, counted in hours.items():
                self._insert(db, HOUR, _hour_row(key, counted))

        self._commit(db, work)

    def _prune(self) -> int:
        kept_days = max(self.retention_days, HOURS_KEPT_DAYS)
        return super()._prune() + self._delete_older(HOUR, "hour", time.time() - kept_days * 86400)

    # --------------------------------------------------------------- reading
    @on_pool
    def summary(self, since: float, until: float, buckets: int = 60) -> Dict[str, Any]:
        """summarize() over the range: from its requests when it is at most RAW_SPAN_S long and the request
        history still has them, else from the hourly summary, starting at the hour ``since`` falls in."""
        if until - since <= RAW_SPAN_S and since >= time.time() - self.retention_days * 86400:
            rows = self._select(f"SELECT {REQUEST_COLUMNS} FROM {REQUEST} WHERE ts >= :since AND ts <= :until",
                                {"since": since, "until": until})
            return summarize(map(of_request, rows), since, until, buckets)
        start, bucket_s, points = hourly_buckets(since, until, buckets)
        rows = self._select(f"SELECT FROM {HOUR} WHERE hour >= :start AND hour < :until",
                            {"start": start, "until": until})
        return summarize(map(of_hour, rows), start, until, points, bucket_s)

    @on_pool
    def events(
        self,
        after_id: int = 0,
        limit: int = 100,
        since: Optional[float] = None,
        until: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """Newest first; with ``after_id``, only events newer than that one; ``since``/``until`` bound ``ts``."""
        where = Where().add("id", ">", after_id).add("ts", ">=", since).add("ts", "<=", until)
        return self._select(f"SELECT FROM {EVENT}{where} ORDER BY id DESC LIMIT {int(limit)}", where.params)

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
        where = (Where("id IS NOT NULL").add("ts", ">=", since).add("ts", "<=", until).add("status", "=", status)
                 .like(["model_id", "task"], model))
        return self._select(f"SELECT FROM {REQUEST}{where} ORDER BY id DESC LIMIT {int(limit)}", where.params)
