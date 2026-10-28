"""
The task and schedule tables: what was asked for, where it got to, and how it
ended. Every piece of background work is a row of the ``jobs`` table, a task
of some kind (jobs/tasks.py): a job (a script in its own process) or an eval
run. They are /db tables in the ``jobs`` namespace (vectorstore/tables.py),
which the admin's Data section shows read-only; the store writes them with
its own SQL, since a task is claimed with a conditional UPDATE so only one
runner runs it.

Every method is async: it runs on the database's thread pool
(vectorstore/engine.py), never on the event loop. A write that clashes with a
concurrent one is run again.
"""

import json
import time
from typing import Any, Dict, List, Optional

from errors import NotFound
from vectorstore import Database, DocumentStore, on_pool
from vectorstore.tables import TableSchema, TableStore, schema_of, table_type

NAMESPACE = "jobs"
TABLES: Dict[str, TableSchema] = {
    "jobs": schema_of(
        "kind:string+",          # job | run | judge | download (jobs/tasks.py), each with its own limit
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
    ),
    "schedules": schema_of(
        "name:string", "job:string",
        "params:object",
        "cron:string",           # five fields, or null when interval_s is set
        "interval_s:number", "enabled:boolean", "webhook_url:string", "priority:string",
        "created:number", "updated:number", "next_run:number+", "last_run:number",
        "last_job_id:string",
    ),
}
JOB = table_type(NAMESPACE, "jobs")
SCHEDULE = table_type(NAMESPACE, "schedules")

QUEUED, RUNNING, SUCCEEDED, FAILED, CANCELLED = "queued", "running", "succeeded", "failed", "cancelled"
FINISHED = (SUCCEEDED, FAILED, CANCELLED)
# What the API shows of a row.
PUBLIC = ("id", "job", "status", "priority", "params", "progress", "message", "result", "error", "created", "started",
          "finished", "schedule_id", "webhook_url")
JOB_COLUMNS = ("id", *TABLES["jobs"].fields)
SCHEDULE_COLUMNS = ("id", *TABLES["schedules"].fields)


def _job(row: Dict[str, Any]) -> Dict[str, Any]:
    """A stored row with every column (null ones too) and its flag as a boolean."""
    out = {key: row.get(key) for key in JOB_COLUMNS}
    out["cancel"] = bool(out["cancel"])
    out["result"] = None if out["result"] is None else json.loads(out["result"])
    out["priority"] = out["priority"] or "normal"
    out["attempts"] = out["attempts"] or 0
    return out


def _schedule(row: Dict[str, Any]) -> Dict[str, Any]:
    out = {key: row.get(key) for key in SCHEDULE_COLUMNS}
    out["params"] = out["params"] or {}
    out["enabled"] = bool(out["enabled"])
    out["priority"] = out["priority"] or "normal"
    return out


class JobNotFound(NotFound, KeyError):
    """No job, or no schedule, with that id."""


def public(row: Dict[str, Any]) -> Dict[str, Any]:
    return {key: row.get(key) for key in PUBLIC}


class JobStore(DocumentStore):
    MIGRATIONS = (("jobs/2026-10-task-kinds", "_task_kinds"),)

    def __init__(self, database: Database, tables: TableStore):
        super().__init__(database)
        self._t = tables
        tables.manage(NAMESPACE, "jobs and schedules: change them through /jobs", TABLES)

    def _ensure_schema(self, db) -> None:
        super()._ensure_schema(db)
        self._t.ensure_managed(NAMESPACE)

    def _task_kinds(self, db) -> None:
        """Until 2026-10 the table held jobs only: each row is a job."""
        self._commit(db, lambda: db.command(
            "sql", f"UPDATE {JOB} SET kind = 'job', priority = 'normal' WHERE kind IS NULL"))

    @on_pool
    def insert(self, id: str, job: Optional[str], params: Dict[str, Any], schedule_id: Optional[str] = None,
               webhook_url: Optional[str] = None, *, kind: str = "job", priority: str = "normal",
               ref: Optional[str] = None, model_id: Optional[str] = None) -> Dict[str, Any]:
        doc = {"id": id, "kind": kind, "priority": priority, "ref": ref, "model_id": model_id, "job": job,
               "status": QUEUED, "params": params, "created": time.time(), "cancel": False, "schedule_id": schedule_id,
               "webhook_url": webhook_url}
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
        rows = self._select(f"SELECT FROM {SCHEDULE} WHERE id = :id", {"id": id})
        return _schedule(rows[0]) if rows else None

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
        rows = self._select(f"SELECT FROM {JOB} WHERE id = :id", {"id": id})
        return _job(rows[0]) if rows else None

    get = on_pool(_get)

    @on_pool
    def list(self, limit: int = 50, status: Optional[str] = None, kind: str = "job") -> List[Dict[str, Any]]:
        where, params = " WHERE kind = :kind", {"kind": kind}
        if status:
            where, params = where + " AND status = :status", {**params, "status": status}
        rows = self._select(f"SELECT FROM {JOB}{where} ORDER BY created DESC LIMIT {int(limit)}", params)
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
        rows = self._select(f"SELECT FROM {JOB} WHERE kind = :kind AND ref = :ref ORDER BY created DESC LIMIT 1",
                            {"kind": kind, "ref": ref})
        return _job(rows[0]) if rows else None

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
