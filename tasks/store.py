"""
The task and schedule tables: what was asked for, where it got to, and how it
ended. Every piece of background work is a row of the ``jobs`` table, a task
of some kind (tasks/runner.py): a job (a script in its own process), an eval
run, a webhook delivery. They are /db tables in the ``jobs`` namespace (db/tables.py),
which the admin's Data section shows read-only; the store writes them with
its own SQL, since a task is claimed with a conditional UPDATE so only one
runner runs it.

Every method is async: it runs on the database's thread pool
(db/engine.py), never on the event loop. A write that clashes with a
concurrent one is run again.
"""

import json
import time
from typing import Any, Dict, List, Optional

from db import Database, DocumentStore, Where, on_pool
from db.tables import TableSchema, TableStore, schema_of, table_type

NAMESPACE = "jobs"
TABLES: Dict[str, TableSchema] = {
    "jobs": schema_of(
        "kind:string+",          # job | run | judge | download | webhook (tasks/runner.py), each with its own limit
        "priority:string",       # high | normal | low (models/events.py)
        "ref:string",            # what it works on: an eval run's id, a Hub repo
        "model_id:string",       # the model it needs, when it needs one: tasks on loaded models go first
        "job:string",            # a job's name (GET /jobs/types)
        "status:string+",        # queued | running | succeeded | failed | cancelled
        "params:object",
        "progress:number",       # 0..1, null if unknown
        "message:string",
        "result:string",         # JSON text: as a map, ArcadeDB would read a top-level "@type" as a type name
        "error:string",
        "created:number+", "started:number", "finished:number",
        "pid:integer",           # the job's process (its process group)
        "attempts:integer",      # times it was resumed after the service died while it ran
        "cancel:boolean",
        "schedule_id:string+",   # the schedule that submitted it, if any
        "webhook_url:string",    # POSTed to when it finishes, besides WEBHOOK_URLS
        "retries:integer",       # times it is retried after failing (tasks/runner.py), and the wait before the first
        "retry_delay_s:number",
        "timeout_s:number",      # longest it may run, null for no limit
        "retried:integer",       # times it has been retried so far
        "not_before:number",     # a retry waits in the queue until then
    ),
    "schedules": schema_of(
        "name:string", "job:string",
        "params:object",
        "cron:string",           # five fields, or null when interval_s is set
        "interval_s:number", "enabled:boolean", "webhook_url:string", "priority:string",
        "created:number", "updated:number", "next_run:number+", "last_run:number",
        "last_job_id:string",
        "retries:integer", "retry_delay_s:number", "timeout_s:number",  # given to the jobs it submits
    ),
}
JOB = table_type(NAMESPACE, "jobs")
SCHEDULE = table_type(NAMESPACE, "schedules")

QUEUED, RUNNING, SUCCEEDED, FAILED, CANCELLED = "queued", "running", "succeeded", "failed", "cancelled"
# The wait before a task's first retry when it was not given one (rows from before retries existed too).
RETRY_DELAY_S = 30.0
FINISHED = (SUCCEEDED, FAILED, CANCELLED)
# What the API shows of a row.
PUBLIC = ("id", "job", "status", "priority", "params", "progress", "message", "result", "error", "created", "started",
          "finished", "schedule_id", "webhook_url", "retries", "retry_delay_s", "timeout_s", "retried", "not_before")
JOB_COLUMNS = ("id", *TABLES["jobs"].fields)
SCHEDULE_COLUMNS = ("id", *TABLES["schedules"].fields)


def _job(row: Dict[str, Any]) -> Dict[str, Any]:
    """A stored row with every column (null ones too) and its flag as a boolean."""
    out = {key: row.get(key) for key in JOB_COLUMNS}
    out["cancel"] = bool(out["cancel"])
    out["result"] = None if out["result"] is None else json.loads(out["result"])
    out["priority"] = out["priority"] or "normal"
    out["attempts"] = out["attempts"] or 0
    out["retries"] = out["retries"] or 0
    out["retried"] = out["retried"] or 0
    out["retry_delay_s"] = out["retry_delay_s"] or RETRY_DELAY_S
    return out


def _schedule(row: Dict[str, Any]) -> Dict[str, Any]:
    out = {key: row.get(key) for key in SCHEDULE_COLUMNS}
    out["params"] = out["params"] or {}
    out["enabled"] = bool(out["enabled"])
    out["priority"] = out["priority"] or "normal"
    out["retries"] = out["retries"] or 0
    out["retry_delay_s"] = out["retry_delay_s"] or RETRY_DELAY_S
    return out


def public(row: Dict[str, Any]) -> Dict[str, Any]:
    return {key: row.get(key) for key in PUBLIC}


class TaskStore(DocumentStore):
    MIGRATIONS = (("jobs/2026-10-task-kinds", "_task_kinds"),)
    NAMESPACE = NAMESPACE
    OWNER = "jobs and schedules: change them through /jobs"
    TABLES = TABLES

    def __init__(self, database: Database, tables: TableStore):
        super().__init__(database, tables)

    def _task_kinds(self, db) -> None:
        """Until 2026-10 the table held jobs only: each row is a job."""
        self._commit(db, lambda: db.command(
            "sql", f"UPDATE {JOB} SET kind = 'job', priority = 'normal' WHERE kind IS NULL"))

    @on_pool
    def insert(self, id: str, job: Optional[str], params: Dict[str, Any], schedule_id: Optional[str] = None,
               webhook_url: Optional[str] = None, *, kind: str = "job", priority: str = "normal",
               ref: Optional[str] = None, model_id: Optional[str] = None, retries: int = 0,
               retry_delay_s: float = RETRY_DELAY_S, timeout_s: Optional[float] = None) -> Dict[str, Any]:
        doc = {"id": id, "kind": kind, "priority": priority, "ref": ref, "model_id": model_id, "job": job,
               "status": QUEUED, "params": params, "created": time.time(), "cancel": False, "schedule_id": schedule_id,
               "webhook_url": webhook_url, "retries": retries, "retry_delay_s": retry_delay_s, "timeout_s": timeout_s,
               "retried": 0}
        db = self._db()
        self._commit(db, lambda: self._insert(db, JOB, doc))
        return self._get(id)

    @on_pool
    def active_for_schedule(self, schedule_id: str) -> List[Dict[str, Any]]:
        """The schedule's jobs still queued or running."""
        rows = self._select(f"SELECT FROM {JOB} WHERE schedule_id = :s AND status IN :active",
                            {"s": schedule_id, "active": [QUEUED, RUNNING]})
        return [_job(r) for r in rows]

    # ------------------------------------------------------------ schedules
    @on_pool
    def schedules(self) -> List[Dict[str, Any]]:
        return [_schedule(r) for r in self._select(f"SELECT FROM {SCHEDULE} ORDER BY name, created")]

    def _get_schedule(self, id: str) -> Optional[Dict[str, Any]]:
        row = self._one(f"SELECT FROM {SCHEDULE} WHERE id = :id", {"id": id})
        return _schedule(row) if row else None

    schedule = on_pool(_get_schedule)

    @on_pool
    def due_schedules(self, now: float) -> List[Dict[str, Any]]:
        rows = self._select(f"SELECT FROM {SCHEDULE} WHERE enabled = true AND next_run IS NOT NULL AND next_run <= :now "
                            f"ORDER BY next_run", {"now": now})
        return [_schedule(r) for r in rows]

    @on_pool
    def put_schedule(self, row: Dict[str, Any]) -> Dict[str, Any]:
        """Insert or replace a schedule (every column of SCHEDULE_COLUMNS given)."""
        doc = {k: row.get(k) for k in SCHEDULE_COLUMNS}
        doc["params"] = doc["params"] or {}
        doc["enabled"] = bool(doc["enabled"])
        self._command(f"UPDATE {SCHEDULE} CONTENT :doc UPSERT WHERE id = :id", {"doc": doc, "id": doc["id"]})
        return self._get_schedule(row["id"])

    @on_pool
    def mark_schedule_run(self, id: str, now: float, job_id: Optional[str], next_run: Optional[float]) -> None:
        if job_id is None:  # skipped: the previous job is still going
            self._command(f"UPDATE {SCHEDULE} SET next_run = :next WHERE id = :id", {"next": next_run, "id": id})
        else:
            self._command(f"UPDATE {SCHEDULE} SET last_run = :now, last_job_id = :job, next_run = :next WHERE id = :id",
                          {"now": now, "job": job_id, "next": next_run, "id": id})

    @on_pool
    def delete_schedule(self, id: str) -> None:
        self._command(f"DELETE FROM {SCHEDULE} WHERE id = :id", {"id": id})

    # ----------------------------------------------------------------- jobs
    def _get(self, id: str) -> Optional[Dict[str, Any]]:
        row = self._one(f"SELECT FROM {JOB} WHERE id = :id", {"id": id})
        return _job(row) if row else None

    get = on_pool(_get)

    @on_pool
    def list(self, limit: int = 50, status: Optional[str] = None, kind: str = "job") -> List[Dict[str, Any]]:
        where = Where().add("kind", "=", kind).add("status", "=", status)
        rows = self._select(f"SELECT FROM {JOB}{where} ORDER BY created DESC LIMIT {int(limit)}", where.params)
        return [_job(r) for r in rows]

    @on_pool
    def queued(self, kind: str) -> List[Dict[str, Any]]:
        """The kind's tasks waiting to start (not those being cancelled), oldest first."""
        rows = self._select(f"SELECT FROM {JOB} WHERE status = :queued AND kind = :kind AND cancel = false ORDER BY created",
                            {"queued": QUEUED, "kind": kind})
        return [_job(r) for r in rows]

    @on_pool
    def latest(self, kind: str, ref: str) -> Optional[Dict[str, Any]]:
        """The newest task of ``kind`` working on ``ref`` (an eval run is queued again when it resumes)."""
        row = self._one(f"SELECT FROM {JOB} WHERE kind = :kind AND ref = :ref ORDER BY created DESC LIMIT 1",
                        {"kind": kind, "ref": ref})
        return _job(row) if row else None

    @on_pool
    def latest_of(self, kind: str, refs: List[str]) -> Dict[str, Dict[str, Any]]:
        """The newest task of ``kind`` working on each of ``refs`` that has one: ref -> task."""
        if not refs:
            return {}
        rows = self._select(f"SELECT FROM {JOB} WHERE kind = :kind AND ref IN :refs ORDER BY created",
                            {"kind": kind, "refs": list(refs)})
        return {row["ref"]: _job(row) for row in rows}

    @on_pool
    def with_status(self, status: str) -> List[Dict[str, Any]]:
        rows = self._select(f"SELECT FROM {JOB} WHERE status = :status ORDER BY created", {"status": status})
        return [_job(r) for r in rows]

    @on_pool
    def claim(self, id: str) -> bool:
        """Mark a queued job running; False if it is no longer queued (cancelled, or claimed)."""
        return self._command(
            f"UPDATE {JOB} SET status = :running, started = :now, progress = null, message = 'starting' "
            f"WHERE id = :id AND status = :queued AND cancel = false",
            {"running": RUNNING, "now": time.time(), "id": id, "queued": QUEUED},
        ) == 1

    @on_pool
    def requeue(self, id: str, attempts: int) -> bool:
        """Put a running task back in the queue (the service stopped under it), its resumes counted as ``attempts``."""
        return self._command(
            f"UPDATE {JOB} SET status = :queued, started = null, pid = null, progress = null, "
            f"message = 'resumed after a restart', attempts = :attempts WHERE id = :id AND status = :running",
            {"queued": QUEUED, "attempts": attempts, "id": id, "running": RUNNING},
        ) == 1

    @on_pool
    def retry(self, id: str, retried: int, not_before: float, error: str, message: str) -> bool:
        """Put a running task that failed back in the queue until ``not_before``, as its ``retried``th retry; False if
        it is no longer running, or is being cancelled."""
        return self._command(
            f"UPDATE {JOB} SET status = :queued, started = null, pid = null, progress = null, message = :message, "
            f"error = :error, retried = :retried, not_before = :not_before "
            f"WHERE id = :id AND status = :running AND cancel = false",
            {"queued": QUEUED, "message": message, "error": error,
             "retried": retried, "not_before": not_before, "id": id, "running": RUNNING},
        ) == 1

    @on_pool
    def set_pid(self, id: str, pid: int) -> None:
        self._command(f"UPDATE {JOB} SET pid = :pid WHERE id = :id", {"pid": pid, "id": id})

    @on_pool
    def progress(self, id: str, fraction: Optional[float], message: str) -> None:
        self._command(f"UPDATE {JOB} SET progress = :fraction, message = :message WHERE id = :id AND status = :running",
                      {"fraction": fraction, "message": message, "id": id, "running": RUNNING})

    @on_pool
    def request_cancel(self, id: str) -> None:
        self._command(f"UPDATE {JOB} SET cancel = true WHERE id = :id", {"id": id})

    @on_pool
    def finish(self, id: str, status: str, result: Any = None, error: Optional[str] = None,
               only_if: tuple = (QUEUED, RUNNING)) -> bool:
        """End a job unless it already ended; False if its status was not in ``only_if``."""
        return self._command(
            f"UPDATE {JOB} SET status = :status, result = :result, error = :error, finished = :now "
            f"WHERE id = :id AND status IN :only_if",
            {"status": status, "result": None if result is None else json.dumps(result), "error": error,
             "now": time.time(), "id": id, "only_if": list(only_if)},
        ) == 1

    @on_pool
    def delete(self, id: str) -> None:
        self._command(f"DELETE FROM {JOB} WHERE id = :id", {"id": id})

    @on_pool
    def prune(self, kind: str, before: float) -> int:
        """Delete the kind's tasks that ended before ``before``; how many."""
        # "before" is a word of ArcadeDB's SQL, so not a parameter's name.
        return self._command(f"DELETE FROM {JOB} WHERE kind = :kind AND status IN :finished AND finished < :cutoff",
                             {"kind": kind, "finished": list(FINISHED), "cutoff": before})
