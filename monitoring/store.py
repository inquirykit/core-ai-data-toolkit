"""
The request log and model events for the /admin dashboard, in the DuckDB
database.

A request is a row of tk_request: its model, task, outcome, duration, tokens,
and the tags, session and backend that served it. Each of its inputs (one,
or a batch's) is a trace in tk_trace while traces are kept: the input and
output, cut down to keep (models/capture.py), and their words, which a
search scans (as the /db tables' index splits them: db/schema.py). A
request's row is kept MONITORING_RETENTION_DAYS, its traces
TRACE_RETENTION_DAYS (never longer than their request); requests whose
context skips traces (an eval run's, a workflow's) have none. Recording
never blocks: rows go onto a queue that one background thread writes in
batches (db.BatchWriter), where the inputs are cut down too.

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
from typing import Any, Dict, Iterable, List, Optional, Tuple, get_args

from core.errors import NotFound
from db import Database, QueuedStore, Where, on_pool
from db.tables import words_held
from models.capture import as_map, compact, input_text, output_text, storable, text_of
from models.events import RequestStatus


logger = logging.getLogger(__name__)

REQUEST = "tk_request"
TRACE = "tk_trace"
HOUR = "tk_request_hour"
EVENT = "tk_event"
# The first characters of a request's input and output a list shows.
PREVIEW_CHARS = 240
SCHEMA: Dict[str, Dict[str, Any]] = {
    REQUEST: {
        "props": {
            "id": "BIGINT", "ts": "DOUBLE", "model_id": "VARCHAR", "task": "VARCHAR",
            "status": "VARCHAR",            # ok | client_error | error
            "duration_ms": "DOUBLE",
            "cold": "BOOLEAN",             # the model was loaded for this request
            "error": "VARCHAR",             # exception class name
            "trace_id": "VARCHAR",          # its first input's trace, when it has one
            "prompt_tokens": "INTEGER",    # token counts of generation requests, when the model reports them
            "completion_tokens": "INTEGER",
            "tags": "VARCHAR[]",           # X-Trace-Tag, X-Session-Id (models/events.py)
            "session": "VARCHAR",
            "backend": "VARCHAR",          # which of a model server's backends answered
            "source": "VARCHAR",           # where it came from: api, admin, data, workflow, eval (models/events.py)
        },
        "indexes": [("id", True), ("ts", False)],
    },
    TRACE: {
        "props": {
            "id": "VARCHAR", "request_id": "BIGINT", "ts": "DOUBLE",
            "input": "JSON", "output": "JSON",
            "text": "VARCHAR",              # the input's and output's words, for a search and the preview
            "input_chars": "INTEGER", "output_chars": "INTEGER",
            "error": "VARCHAR",             # the request's error, with its text
        },
        "indexes": [("id", True), ("request_id", False), ("ts", False)],
    },
    HOUR: {
        "props": {
            "hour": "BIGINT",                # Unix time the hour starts at
            "model_id": "VARCHAR", "task": "VARCHAR",
            "ok": "INTEGER", "client_error": "INTEGER", "error": "INTEGER",  # its requests, by status
            "cold_starts": "INTEGER", "prompt_tokens": "BIGINT", "completion_tokens": "BIGINT",
            "last_request": "DOUBLE",
            "latency": "VARCHAR",           # the durations' histogram, JSON {bucket: count} (see latency_bucket)
            "sources": "VARCHAR",           # where its requests came from, JSON {source: count} (REQUEST's source)
        },
        "indexes": [("hour, model_id, task", True), ("hour", False)],
    },
    EVENT: {
        "props": {
            "id": "BIGINT", "ts": "DOUBLE",
            "kind": "VARCHAR",              # load | load_failed | unload | evict | limits
            "model_key": "VARCHAR", "duration_ms": "DOUBLE", "detail": "VARCHAR", "pid": "INTEGER",
        },
        "indexes": [("id", True), ("ts", False)],
    },
}
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

STATUSES = get_args(RequestStatus)

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



def tally() -> Dict[str, Any]:
    """Nothing counted yet."""
    return {**dict.fromkeys(SUMS, 0), "last_request": None, "latency": Counter(), "sources": Counter()}


def add(into: Dict[str, Any], part: Dict[str, Any]) -> None:
    """Count ``part`` (a request, an hour or another tally) into ``into``."""
    for name in SUMS:
        into[name] += part[name] or 0
    if part["last_request"] is not None:
        into["last_request"] = max(into["last_request"] or 0, part["last_request"])
    into["latency"].update(part["latency"])
    into["sources"].update(part["sources"])


def of_request(row: Any) -> Dict[str, Any]:
    """A request row as a tally of one, at its time."""
    return {
        "at": row["ts"], "model_id": row["model_id"], "task": row["task"],
        **{status: int(row["status"] == status) for status in STATUSES},
        "cold_starts": int(bool(row["cold"])),
        "prompt_tokens": row["prompt_tokens"], "completion_tokens": row["completion_tokens"],
        "last_request": row["ts"], "latency": {latency_bucket(row["duration_ms"]): 1},
        # A request from before the log kept where it came from is "other".
        "sources": {row.get("source") or "other": 1},
    }


def of_tallied(row: Dict[str, Any]) -> Dict[str, Any]:
    """A tally the database made of a point's requests to one model and task (``summary``), at its last request."""
    return {**row, "last_request": row["at"], "latency": row["latency"] or {}, "sources": row["sources"] or {}}


def of_hour(row: Dict[str, Any]) -> Dict[str, Any]:
    """An hourly summary row as a tally, at its hour's start."""
    return {**row, "at": row["hour"], "latency": {int(b): n for b, n in json.loads(row["latency"] or "{}").items()},
            "sources": json.loads(row.get("sources") or "{}")}


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
    """Totals, per-model stats and a timeline of ``buckets`` points from tallies (of_request, of_tallied, of_hour).

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
            "sources": dict(total["sources"]),
        },
        "models": sorted(({"model_key": m["model_key"], "model_id": m["model_id"], "task": m["task"], **counts(m),
                           "cold_starts": m["cold_starts"], "last_request": m["last_request"]}
                          for m in per_model.values()), key=lambda m: (-m["requests"], m["model_key"])),
        "timeline": [{"start": since + i * bucket_s, **{s: p[s] for s in STATUSES}, **latency(p)}
                     for i, p in enumerate(timeline)],
    }


def _hour_row(key: Tuple[int, str, str], counted: Dict[str, Any]) -> Dict[str, Any]:
    hour, model_id, task = key
    return {"hour": hour, "model_id": model_id, "task": task, **{name: counted[name] for name in SUMS},
            "last_request": counted["last_request"], "latency": json.dumps(counted["latency"], separators=(",", ":")),
            "sources": json.dumps(counted["sources"], separators=(",", ":"))}


class MonitoringStore(QueuedStore):
    """The request log and model events; see the module docstring. The dashboard pages events by id."""

    SCHEMA = SCHEMA
    RETAINED = ((REQUEST, "ts"), (EVENT, "ts"))

    def __init__(self, database: Database, retention_days: float = 7.0, trace_days: float = 3.0,
                 trace_max_chars: int = 8000, max_queue: int = 10_000):
        super().__init__(database, "monitoring-writer", retention_days, max_queue, batch_size=500)
        self.trace_days = trace_days
        self.trace_max_chars = trace_max_chars

    @property
    def tracing(self) -> bool:
        """Whether requests' inputs and outputs are kept (TRACE_RETENTION_DAYS > 0)."""
        return self.trace_days > 0

    @property
    def traces_kept_days(self) -> float:
        """How long a trace is kept: TRACE_RETENTION_DAYS, never longer than its request."""
        return min(self.trace_days, self.retention_days)

    # --------------------------------------------------------------- writing
    def record_request(
        self, model_id: str, task: str, status: str, duration_ms: float, cold: bool, error: Optional[str] = None,
        prompt_tokens: Optional[int] = None, completion_tokens: Optional[int] = None, *, tags: Iterable[str] = (),
        session: Optional[str] = None, backend: Optional[str] = None, source: Optional[str] = None,
        calls: Iterable[tuple] = (), message: Optional[str] = None,
    ) -> None:
        """Queue the request's row, and a trace of each of ``calls`` (its trace id, input and output) while traces are
        kept; ``message`` is its error with its text. Never blocks: the inputs and outputs go on the queue as they
        are, and are cut down to keep by the writer."""
        calls = list(calls) if self.tracing else []
        text = lambda value: None if value is None else storable(value)  # noqa: E731 (what a client named)
        self.writer.put((REQUEST, {
            "id": self._next_id(), "ts": time.time(), "model_id": text(model_id), "task": text(task), "status": status,
            "duration_ms": float(duration_ms), "cold": bool(cold), "error": error,
            "trace_id": calls[0][0] if calls else None, "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens, "tags": [storable(t) for t in tags], "session": text(session),
            "backend": text(backend), "source": source,
        }, calls, text(message)))

    def _traces_of(self, request: Dict[str, Any], calls: List[tuple], error: Optional[str]) -> List[Dict[str, Any]]:
        """A request's inputs and outputs as the rows of TRACE, cut down to keep."""
        docs = []
        for trace_id, given, answered in calls:
            inp = compact(given, self.trace_max_chars)
            out = compact(answered, self.trace_max_chars) if answered is not None else None
            docs.append({
                "id": trace_id, "request_id": request["id"], "ts": request["ts"], "input": as_map(inp),
                "output": None if out is None else as_map(out),
                "text": text_of([input_text(inp), output_text(out) if out is not None else ""]),
                "input_chars": len(json.dumps(inp, ensure_ascii=False)),
                "output_chars": 0 if out is None else len(json.dumps(out, ensure_ascii=False)), "error": error,
            })
        return docs

    def record_event(
        self, kind: str, model_key: str, duration_ms: Optional[float] = None, detail: Optional[str] = None
    ) -> None:
        self.writer.put((EVENT, {
            "id": self._next_id(), "ts": time.time(), "kind": kind, "model_key": model_key,
            "duration_ms": None if duration_ms is None else float(duration_ms), "detail": detail, "pid": os.getpid(),
        }))

    def _write(self, items: List[Any]) -> None:
        requests = [item for item in items if item[0] == REQUEST]
        hours = hours_of(doc for _, doc, *_ in requests)
        traces = [t for _, doc, calls, error in requests for t in self._traces_of(doc, calls, error)]
        db = self._db()

        def work() -> None:
            self._insert_many(db, REQUEST, [doc for _, doc, *_ in requests])
            self._insert_many(db, TRACE, traces)
            self._insert_many(db, EVENT, [item[1] for item in items if item[0] == EVENT])
            self._add_hours(db, hours)

        self._commit(db, work)

    def _add_hours(self, db, hours: Dict[Tuple[int, str, str], Dict[str, Any]]) -> None:
        """Count ``hours`` (hours_of) into the hourly summary, in the caller's transaction."""
        if not hours:
            return
        rows = self._rows(db, f"SELECT * FROM {HOUR} WHERE hour = ANY($hours)", {"hours": sorted({k[0] for k in hours})})
        kept = {(r["hour"], r["model_id"], r["task"]): r for r in rows}
        for key, counted in hours.items():
            if key not in kept:
                self._insert(db, HOUR, _hour_row(key, counted))
                continue
            merged = tally()
            add(merged, counted)
            add(merged, of_hour(kept[key]))
            row = self._values(HOUR, _hour_row(key, merged))
            db.execute(f"UPDATE {HOUR} SET {', '.join(f'{k} = ${k}' for k in row)} WHERE hour = $hour AND "
                       "model_id = $model_id AND task = $task", row)

    def _prune(self) -> int:
        """Requests and events past the retention, traces past theirs (never kept longer than their request: all of
        them while traces are off), and hours past theirs."""
        now = time.time()
        kept_days = max(self.retention_days, HOURS_KEPT_DAYS)
        traces = self._delete_older(TRACE, "ts", now - self.traces_kept_days * 86400)
        return super()._prune() + traces + self._delete_older(HOUR, "hour", now - kept_days * 86400)

    # --------------------------------------------------------------- reading
    @on_pool
    def summary(self, since: float, until: float, buckets: int = 60) -> Dict[str, Any]:
        """summarize() over the range: from its requests when it is at most RAW_SPAN_S long and the request
        history still has them, else from the hourly summary, starting at the hour ``since`` falls in."""
        if until - since <= RAW_SPAN_S and since >= time.time() - self.retention_days * 86400:
            # Tallied by the database, a row per model, task and point of the timeline: the dashboard asks every few
            # seconds, and a busy hour's requests read into Python one by one would cost seconds each time.
            bucket_s = max((until - since) / buckets, 1.0)
            statuses = ", ".join(f"count(*) FILTER (WHERE status = '{s}') AS {s}" for s in STATUSES)
            rows = self._select(
                f"SELECT model_id, task, max(ts) AS at, {statuses}, count(*) FILTER (WHERE cold) AS cold_starts, "
                "sum(prompt_tokens) AS prompt_tokens, sum(completion_tokens) AS completion_tokens, "
                # latency_bucket(), as the hourly summary counts it
                "histogram(ceil(ln(greatest(duration_ms, $fastest)) / ln($gamma))::INTEGER) AS latency, "
                f"histogram(coalesce(source, 'other')) AS sources FROM {REQUEST} WHERE ts >= $since AND ts <= $until "
                "GROUP BY model_id, task, floor((ts - $since) / $bucket_s)",
                {"since": since, "until": until, "bucket_s": bucket_s, "fastest": FASTEST_MS, "gamma": GAMMA})
            return summarize(map(of_tallied, rows), since, until, buckets, bucket_s)
        start, bucket_s, points = hourly_buckets(since, until, buckets)
        rows = self._select(f"SELECT * FROM {HOUR} WHERE hour >= $start AND hour < $until",
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
        return self._select(f"SELECT * FROM {EVENT}{where} ORDER BY id DESC LIMIT {int(limit)}", where.params)

    @on_pool
    def recent_requests(
        self,
        limit: int = 50,
        since: Optional[float] = None,
        until: Optional[float] = None,
        status: Optional[str] = None,
        model: Optional[str] = None,
        tag: Optional[str] = None,
        session: Optional[str] = None,
        q: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Newest first, each with the first words of its first trace (``preview``), or of the trace that holds the
        words of ``q`` (and ``trace_id`` that one's). ``status``, ``tag`` and ``session`` match exactly; ``model`` is
        a case-insensitive substring of the model ID or task; ``q`` holds words one of its traces holds, which only a
        request with traces can."""
        where = (Where("r.id IS NOT NULL").add("r.ts", ">=", since).add("r.ts", "<=", until)
                 .add("r.status", "=", status).add("r.session", "=", session).contains("r.tags", tag)
                 .like(["r.model_id", "r.task"], model))
        if not q:
            # The newest first, then their previews: a join of those alone.
            return self._select(f"SELECT r.*, left(t.text, {PREVIEW_CHARS}) AS preview FROM (SELECT * FROM {REQUEST} r"
                                f"{where} ORDER BY r.id DESC LIMIT {int(limit)}) r LEFT JOIN {TRACE} t ON t.id = r.trace_id "
                                "ORDER BY r.id DESC", where.params)
        # The traces that hold the words, within the range (a trace is its request's time), one per request (any one
        # of a batch's that match: nothing keeps their order).
        held, params = words_held("x.text", q)
        bounds = [f"x.ts {op} ${name}" for op, name, value in ((">=", "m_since", since), ("<=", "m_until", until))
                  if value is not None]
        return self._select(f"WITH m AS (SELECT request_id, min(id) AS trace_id FROM {TRACE} x WHERE "
                            f"{' AND '.join([held, *bounds])} GROUP BY request_id) SELECT r.* EXCLUDE (trace_id), "
                            f"m.trace_id, left(t.text, {PREVIEW_CHARS}) AS preview FROM {REQUEST} r JOIN m ON "
                            f"m.request_id = r.id JOIN {TRACE} t ON t.id = m.trace_id{where} ORDER BY r.id DESC "
                            f"LIMIT {int(limit)}", {**where.params, **params, "m_since": since, "m_until": until})

    @on_pool
    def trace(self, trace_id: str) -> Dict[str, Any]:
        """One input of a request with its output, and the request's model, outcome and timing; NotFound once it is
        gone (TRACE_RETENTION_DAYS)."""
        rows = self._select(f"SELECT t.id, t.ts, t.input, t.output, t.input_chars, t.output_chars, t.error, "
                            f"left(t.text, {PREVIEW_CHARS}) AS preview, r.model_id, r.task, r.status, r.duration_ms, "
                            f"r.cold, r.tags, r.session, r.backend FROM {TRACE} t JOIN {REQUEST} r ON r.id = t.request_id "
                            "WHERE t.id = $id", {"id": trace_id})
        if not rows:
            raise NotFound(f"No trace {trace_id!r}")
        return rows[0]
