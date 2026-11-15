"""
The task and schedule tables: what was asked for, where it got to, and how it
ended. Every piece of background work is a row of the ``tasks`` table, a task
of some kind (tasks/runner.py): a job (a script in its own process), an eval
run, a webhook delivery. They are /db tables in the ``tasks`` workspace (db/tables.py),
which the admin's Data section shows read-only; the store writes them with
its own SQL, since a task is claimed with a conditional UPDATE so only one
runner runs it.

Every method is async: it runs on the database's thread pool
(db/engine.py), never on the event loop. A write that clashes with a
concurrent one is run again.
"""

import json
import time
from typing import Any, Dict, List, Literal, Optional, Sequence, get_args

from db import Database, DocumentStore, Where, on_pool
from db.tables import TableSchema, TableStore, schema_of, table_type

WORKSPACE = "tasks"
TABLES: Dict[str, TableSchema] = {
    "tasks": schema_of(
        "kind:string+",          # job, run, judge, webhook, copy, workflow (tasks/runner.py), each with its limit
        "priority:string",       # high | normal | low (models/events.py)
        "ref:string",            # what it works on: a run's id, a bucket (workspace/bucket), a webhook's URL
        "model_id:string",       # the model it needs, when it needs one: tasks on loaded models go first
        "job:string",            # a job's name (GET /jobs/types), for the kind job
        "status:string+",        # queued | running | succeeded | failed | cancelled
        "params:object",
        "progress:number",       # 0..1, null if unknown
        "message:string",
        "result:any",            # what the task gave, as JSON
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
        "state:object",          # how far a run got, as its kind keeps it (checkpoint): it goes on from there
    ),
    "schedules": schema_of(
        "name:string",
        "kind:string",           # the kind of task it submits (tasks/schedule.py)
        "job:string",            # what that kind runs: a job's name for "job"
        "params:object",
        "cron:string",           # five fields, or null when interval_s is set
        "interval_s:number", "enabled:boolean", "webhook_url:string", "priority:string",
        "created:number", "updated:number", "next_run:number+", "last_run:number",
        "last_task_id:string",
        "retries:integer", "retry_delay_s:number", "timeout_s:number",  # given to the tasks it submits
    ),
}
TASK = table_type(WORKSPACE, "tasks")
SCHEDULE = table_type(WORKSPACE, "schedules")

# A task's status, as the API declares it too (api/responses/tasks.py).
Status = Literal["queued", "running", "succeeded", "failed", "cancelled"]
QUEUED, RUNNING, SUCCEEDED, FAILED, CANCELLED = get_args(Status)
# The wait before a task's first retry when it was not given one.
RETRY_DELAY_S = 30.0
FINISHED = (SUCCEEDED, FAILED, CANCELLED)
# What the API shows of a row (api/responses/tasks.py, Task).
PUBLIC = ("id", "kind", "ref", "job", "model_id", "status", "priority", "params", "progress", "message", "result",
          "error", "created", "started", "finished", "schedule_id", "webhook_url", "retries", "retry_delay_s",
          "timeout_s", "retried", "not_before")
# With _seq, the write number a task was made with (db/rows.py), which no change of it moves: the order of a list.
TASK_COLUMNS = ("id", "_seq", *TABLES["tasks"].fields)
SCHEDULE_COLUMNS = ("id", *TABLES["schedules"].fields)


def _task(row: Dict[str, Any]) -> Dict[str, Any]:
    """A stored row with every column (null ones too) and its flag as a boolean."""
    out = {key: row.get(key) for key in TASK_COLUMNS}
    out["cancel"] = bool(out["cancel"])
    out["attempts"] = out["attempts"] or 0  # set on a resume, not when it is queued
    return out


def _schedule(row: Dict[str, Any]) -> Dict[str, Any]:
    out = {key: row.get(key) for key in SCHEDULE_COLUMNS}
    out["enabled"] = bool(out["enabled"])
    return out


def public(row: Dict[str, Any]) -> Dict[str, Any]:
    return {key: row.get(key) for key in PUBLIC}


class TaskStore(DocumentStore):
    WORKSPACE = WORKSPACE
    OWNER = ("the task queue (jobs, eval runs, webhooks, copies, workflow runs): changed through their own "
             "routes")
    TABLES = TABLES

    def __init__(self, database: Database, tables: TableStore):
        super().__init__(database, tables)

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
        self._commit(db, lambda: self._t.write_row(db, WORKSPACE, "tasks", doc))
        return self._get(id)

    @on_pool
    def active_for_schedule(self, schedule_id: str) -> List[Dict[str, Any]]:
        """The schedule's tasks still queued or running."""
        rows = self._select(f"SELECT * FROM {TASK} WHERE schedule_id = $s AND status = ANY($active)",
                            {"s": schedule_id, "active": [QUEUED, RUNNING]})
        return [_task(r) for r in rows]

    # ------------------------------------------------------------ schedules
    @on_pool
    def schedules(self) -> List[Dict[str, Any]]:
        return [_schedule(r) for r in self._select(f"SELECT * FROM {SCHEDULE} ORDER BY name, created")]

    def _get_schedule(self, id: str) -> Optional[Dict[str, Any]]:
        row = self._one(f"SELECT * FROM {SCHEDULE} WHERE id = $id", {"id": id})
        return _schedule(row) if row else None

    schedule = on_pool(_get_schedule)

    @on_pool
    def due_schedules(self, now: float) -> List[Dict[str, Any]]:
        rows = self._select(f"SELECT * FROM {SCHEDULE} WHERE enabled = true AND next_run IS NOT NULL AND next_run <= $now "
                            f"ORDER BY next_run", {"now": now})
        return [_schedule(r) for r in rows]

    @on_pool
    def put_schedule(self, row: Dict[str, Any]) -> Dict[str, Any]:
        """Insert or replace a schedule (every column of SCHEDULE_COLUMNS given)."""
        doc = {k: row.get(k) for k in SCHEDULE_COLUMNS}
        doc["params"] = doc["params"] or {}
        doc["enabled"] = bool(doc["enabled"])
        db = self._db()
        self._commit(db, lambda: self._t.write_row(db, WORKSPACE, "schedules", doc))
        return self._get_schedule(row["id"])

    @on_pool
    def mark_schedule_run(self, id: str, now: float, task_id: Optional[str], next_run: Optional[float]) -> None:
        if task_id is None:  # skipped: the previous task is still going
            self._command(f"UPDATE {SCHEDULE} SET next_run = $next WHERE id = $id", {"next": next_run, "id": id})
        else:
            self._command(f"UPDATE {SCHEDULE} SET last_run = $now, last_task_id = $task, next_run = $next "
                          "WHERE id = $id", {"now": now, "task": task_id, "next": next_run, "id": id})

    @on_pool
    def delete_schedule(self, id: str) -> None:
        self._command(f"DELETE FROM {SCHEDULE} WHERE id = $id", {"id": id})

    # ---------------------------------------------------------------- tasks
    def _get(self, id: str) -> Optional[Dict[str, Any]]:
        row = self._one(f"SELECT * FROM {TASK} WHERE id = $id", {"id": id})
        return _task(row) if row else None

    get = on_pool(_get)

    @on_pool
    def list(self, limit: int = 50, statuses: Sequence[str] = (), kind: Optional[str] = None,
             ref: Optional[str] = None, before: Optional[int] = None, jobs: Sequence[str] = ()) -> List[Dict[str, Any]]:
        """Newest first, every kind or one, of any of ``statuses`` (any status when none) and of ``jobs`` (any when
        none); with ``ref``, only those working on it; with ``before``, those made before that write number (the last
        ``_seq`` of a page), so a page goes on where the last one stopped however many tasks came since."""
        where = (Where().add("kind", "=", kind).among("status", statuses).among("job", jobs).add("ref", "=", ref)
                 .add("_seq", "<", before))
        rows = self._select(f"SELECT * FROM {TASK}{where} ORDER BY _seq DESC LIMIT {int(limit)}", where.params)
        return [_task(r) for r in rows]

    @on_pool
    def queued(self, kind: str) -> List[Dict[str, Any]]:
        """The kind's tasks waiting to start (not those being cancelled), oldest first."""
        rows = self._select(f"SELECT * FROM {TASK} WHERE status = $queued AND kind = $kind AND cancel = false ORDER BY created",
                            {"queued": QUEUED, "kind": kind})
        return [_task(r) for r in rows]

    @on_pool
    def latest(self, kind: str, ref: str) -> Optional[Dict[str, Any]]:
        """The newest task of ``kind`` working on ``ref`` (an eval run is queued again when it resumes)."""
        row = self._one(f"SELECT * FROM {TASK} WHERE kind = $kind AND ref = $ref ORDER BY created DESC LIMIT 1",
                        {"kind": kind, "ref": ref})
        return _task(row) if row else None

    @on_pool
    def latest_of(self, kind: str, refs: List[str]) -> Dict[str, Dict[str, Any]]:
        """The newest task of ``kind`` working on each of ``refs`` that has one: ref -> task."""
        if not refs:
            return {}
        rows = self._select(f"SELECT * FROM {TASK} WHERE kind = $kind AND ref = ANY($refs) ORDER BY created",
                            {"kind": kind, "refs": list(refs)})
        return {row["ref"]: _task(row) for row in rows}

    @on_pool
    def with_status(self, status: str) -> List[Dict[str, Any]]:
        rows = self._select(f"SELECT * FROM {TASK} WHERE status = $status ORDER BY created", {"status": status})
        return [_task(r) for r in rows]

    @on_pool
    def claim(self, id: str) -> bool:
        """Mark a queued task running; False if it is no longer queued (cancelled, or claimed)."""
        return self._command(
            f"UPDATE {TASK} SET status = $running, started = $now, progress = null, message = 'starting' "
            f"WHERE id = $id AND status = $queued AND cancel = false",
            {"running": RUNNING, "now": time.time(), "id": id, "queued": QUEUED},
        ) == 1

    @on_pool
    def requeue(self, id: str, attempts: int) -> bool:
        """Put a running task back in the queue (the service stopped under it), its resumes counted as ``attempts``."""
        return self._command(
            f"UPDATE {TASK} SET status = $queued, started = null, pid = null, progress = null, "
            f"message = 'resumed after a restart', attempts = $attempts WHERE id = $id AND status = $running",
            {"queued": QUEUED, "attempts": attempts, "id": id, "running": RUNNING},
        ) == 1

    @on_pool
    def retry(self, id: str, retried: int, not_before: float, error: str, message: str) -> bool:
        """Put a running task that failed back in the queue until ``not_before``, as its ``retried``th retry; False if
        it is no longer running, or is being cancelled."""
        return self._command(
            f"UPDATE {TASK} SET status = $queued, started = null, pid = null, progress = null, message = $message, "
            f"error = $error, retried = $retried, not_before = $not_before "
            f"WHERE id = $id AND status = $running AND cancel = false",
            {"queued": QUEUED, "message": message, "error": error,
             "retried": retried, "not_before": not_before, "id": id, "running": RUNNING},
        ) == 1

    @on_pool
    def due_now(self, id: str) -> bool:
        """Make a queued task that waits for its retry due now; False if it is not queued."""
        return self._command(f"UPDATE {TASK} SET not_before = null WHERE id = $id AND status = $queued",
                             {"id": id, "queued": QUEUED}) == 1

    @on_pool
    def set_pid(self, id: str, pid: int) -> None:
        self._command(f"UPDATE {TASK} SET pid = $pid WHERE id = $id", {"pid": pid, "id": id})

    @on_pool
    def progress(self, id: str, fraction: Optional[float], message: str) -> None:
        self._command(f"UPDATE {TASK} SET progress = $fraction, message = $message WHERE id = $id AND status = $running",
                      {"fraction": fraction, "message": message, "id": id, "running": RUNNING})

    @on_pool
    def checkpoint(self, id: str, state: Any) -> None:
        self._command(f"UPDATE {TASK} SET state = $state::JSON WHERE id = $id", {"state": json.dumps(state), "id": id})

    @on_pool
    def request_cancel(self, id: str) -> None:
        self._command(f"UPDATE {TASK} SET cancel = true WHERE id = $id", {"id": id})

    @on_pool
    def finish(self, id: str, status: str, result: Any = None, error: Optional[str] = None,
               only_if: tuple = (QUEUED, RUNNING)) -> bool:
        """End a task unless it already ended; False if its status was not in ``only_if``."""
        return self._command(
            f"UPDATE {TASK} SET status = $status, result = $result::JSON, error = $error, finished = $now "
            f"WHERE id = $id AND status = ANY($only_if)",
            {"status": status, "result": None if result is None else json.dumps(result), "error": error,
             "now": time.time(), "id": id, "only_if": list(only_if)},
        ) == 1

    @on_pool
    def delete(self, id: str) -> None:
        self._command(f"DELETE FROM {TASK} WHERE id = $id", {"id": id})

    @on_pool
    def delete_of(self, kind: str, ref: str) -> int:
        """Delete the kind's tasks of ``ref`` that have ended (a workflow's runs, as it is deleted); how many."""
        return self._command(f"DELETE FROM {TASK} WHERE kind = $kind AND ref = $ref AND status = ANY($finished)",
                             {"kind": kind, "ref": ref, "finished": list(FINISHED)})

    @on_pool
    def prune(self, kind: str, before: float) -> int:
        """Delete the kind's tasks that ended before ``before``; how many."""
        return self._command(f"DELETE FROM {TASK} WHERE kind = $kind AND status = ANY($finished) AND finished < $cutoff",
                             {"kind": kind, "finished": list(FINISHED), "cutoff": before})
