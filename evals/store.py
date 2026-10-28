"""
Traces, datasets, runs and scores in the embedded ArcadeDB database.

Shares the database and its thread pool with every store (vectorstore/engine.py),
so every call here blocks and runs there, never on the event loop.

Traces: one document per request (model, task, input, output, timing,
outcome, tags), with a full-text index over their words. A day's traces go
into their own bucket (tk_trace_d20261017), and expiry drops whole buckets
older than the retention, instead of deleting row by row. Recording never
blocks a request: traces go onto a queue that one writer thread drains in
batches.

Datasets hold examples (an input, maybe a reference answer), copied from
traces or added directly, kept as given (a trace whose input was cut is
refused, since a run would then measure something else); they are never
expired. A run is one model over a dataset, its items the outputs; scores
belong to items (see scoring.py). These are /db tables in the ``evals``
namespace (vectorstore/tables.py): browsable in /admin's Data section,
read-only there, written only here.
"""

import json
import logging
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, Iterator, List, Optional, TYPE_CHECKING

import errors
from vectorstore import BatchWriter, Database, DocumentStore, StoreError, on_pool
from vectorstore.tables import TableSchema, TableStore, schema_of, table_type

from .capture import compact, compacted, input_text, output_text, text_of

if TYPE_CHECKING:
    from models.events import RequestEvent

logger = logging.getLogger(__name__)

TRACE = "tk_trace"
NAMESPACE = "evals"
DATASET, EXAMPLE, RUN, ITEM, SCORE = (table_type(NAMESPACE, t) for t in ("datasets", "examples", "runs", "items", "scores"))
BUCKET = re.compile(r"^tk_trace_d(\d{8})$")
PREVIEW_CHARS = 240

# The traces' type: its properties and the indexes on them (name, unique).
SCHEMA: Dict[str, Dict[str, Any]] = {
    TRACE: {
        "props": {
            "id": "STRING", "ts": "DOUBLE", "model_id": "STRING", "task": "STRING", "status": "STRING",
            "duration_ms": "DOUBLE", "cold": "BOOLEAN", "error": "STRING", "tags": "LIST", "session": "STRING",
            "backend": "STRING", "input": "MAP", "output": "MAP", "text": "STRING", "input_chars": "INTEGER", "output_chars": "INTEGER",
        },
        "indexes": [("id", True), ("ts", False), ("model_id", False), ("task", False), ("status", False), ("session", False)],
        "fulltext": ["text"],
    },
}


# The evals namespace's tables (each has a unique string id besides).
TABLES: Dict[str, TableSchema] = {
    "datasets": schema_of("name:string!", "description:string", "created:number"),
    "examples": schema_of("dataset_id:string+", "created:number", "task:string", "input:object", "reference:string",
                        "text:string", "tags:string[]", "source_trace_id:string", "source_model_id:string",
                        "source_output:object"),
    "runs": schema_of("dataset_id:string+", "name:string", "model_id:string", "task:string", "params:object",
                    "status:string", "created:number", "started:number", "finished:number", "total:integer",
                    "done:integer", "failed:integer", "error:string", "webhook_url:string"),
    "items": schema_of("run_id:string+", "example_id:string+", "created:number", "output:object", "text:string",
                     "status:string", "error:string", "duration_ms:number", "cold:boolean"),
    "scores": schema_of("run_id:string+", "item_id:string+", "example_id:string+", "metric:string", "value:number",
                      "label:string", "rationale:string", "judge:string", "against:string", "created:number"),
}
# Examples are read this many at a time (runs, exports, comparisons).
EXAMPLE_PAGE = 500

RUN_STATUSES = ("queued", "running", "done", "failed", "cancelled", "interrupted")


class EvalError(StoreError, ValueError):
    """A request the store refuses (a client error)."""


class NotFound(EvalError, errors.NotFound):
    """No such dataset, example, run or trace: an errors.NotFound too, so one ``except`` catches every store's."""

    status = 404  # before StoreError's 400 in the bases' order


def new_id() -> str:
    return uuid.uuid4().hex


def _as_map(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {"value": value}


def _json(value: Any) -> Optional[str]:
    return None if value is None else json.dumps(value, ensure_ascii=False)


def _loads(raw: Any) -> Any:
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return raw


def bucket_for(ts: float) -> str:
    return TRACE + "_d" + datetime.fromtimestamp(ts, timezone.utc).strftime("%Y%m%d")


def expired_buckets(names: Iterable[str], retention_days: float, now: Optional[float] = None) -> List[str]:
    """The day buckets whose whole day ended more than ``retention_days`` ago."""
    cutoff = datetime.fromtimestamp(now or time.time(), timezone.utc) - timedelta(days=retention_days)
    found = []
    for name in names:
        m = BUCKET.match(name)
        if m:
            day_end = datetime.strptime(m.group(1), "%Y%m%d").replace(tzinfo=timezone.utc) + timedelta(days=1)
            if day_end <= cutoff:
                found.append(name)
    return found


class EvalStore(DocumentStore):
    """Traces, datasets, runs and scores; see the module docstring."""

    SCHEMA = SCHEMA
    MIGRATIONS = (("evals/2026-10-drop-judge-cache", "_drop_judge_cache"),)

    def __init__(self, database: Database, tables: TableStore, retention_days: float = 3,
                 max_chars: int = 8000, max_queue: int = 5000):
        super().__init__(database)
        self._t = tables
        tables.manage(NAMESPACE, "traces and evals: change them in /admin's Evals section", TABLES)
        self.retention_days = retention_days
        self.max_chars = max_chars
        self._buckets: set = set()
        self.writer = BatchWriter(self._write, "trace-writer", max_queue)

    @property
    def dropped(self) -> int:
        """Traces not kept because the queue was full."""
        return self.writer.dropped

    @property
    def capturing(self) -> bool:
        """Whether requests are traced at all (TRACE_RETENTION_DAYS > 0)."""
        return self.retention_days > 0

    # --------------------------------------------------------------- plumbing
    def _ensure_schema(self, db) -> None:
        super()._ensure_schema(db)
        self._buckets = set(self._type_buckets(db))
        self._t.ensure_managed(NAMESPACE)

    def _drop_judge_cache(self, db) -> None:
        """Until 2026-10 the judge's replies were cached in a table; Pydantic Evals asks the judge afresh."""
        if self._t._has(NAMESPACE, "judge_cache"):
            self._t._drop_table(NAMESPACE, "judge_cache")

    def _type_buckets(self, db) -> List[str]:
        rows = self._rows(db, "SELECT buckets FROM schema:types WHERE name = :n", {"n": TRACE})
        return list(rows[0]["buckets"]) if rows else []

    def _ensure_bucket(self, db, name: str) -> None:
        if name in self._buckets:
            return
        with self._schema_lock:
            if name not in self._buckets:
                if name not in self._type_buckets(db):
                    db.command("sql", f"CREATE BUCKET {name}")
                    db.command("sql", f"ALTER TYPE {TRACE} BUCKET +{name}")
                self._buckets.add(name)

    # ---------------------------------------------------------------- traces
    def on_request(self, event: "RequestEvent") -> None:
        """The model manager's sink (models/events.py): a trace per input, unless the request's context skips them."""
        if event.context.skip:
            return
        for call in event.calls:
            self.record(call.trace_id, event.model_id, event.task, call.input, call.output, event.status,
                        event.duration_ms, event.cold, error=event.error_message, tags=event.context.tags,
                        session=event.context.session, backend=event.backend)

    def record(self, trace_id: str, model_id: str, task: str, request: Any, result: Any, status: str,
               duration_ms: float, cold: bool, error: Optional[str] = None,
               tags: Optional[List[str]] = None, session: Optional[str] = None,
               backend: Optional[str] = None) -> None:
        """Queue a request's trace (on_request calls this). Never blocks.

        ``backend`` is which of a model server's backends answered, so runs over Ollama's and MLX's
        weights of the same model ID can be told apart.
        """
        if not self.capturing:
            return
        # The input and output go on the queue as they are; cutting them to size and indexing their words is the
        # writer thread's work (``_trace_doc``), not the event loop's.
        self.writer.put((trace_id, time.time(), model_id, task, status, duration_ms, cold, error, list(tags or []),
                         session, backend, request, result))

    def _trace_doc(self, item: tuple) -> Dict[str, Any]:
        trace_id, ts, model_id, task, status, duration_ms, cold, error, tags, session, backend, request, result = item
        inp = compact(request, self.max_chars)
        out = compact(result, self.max_chars) if result is not None else None
        words = text_of([input_text(inp), output_text(out) if out is not None else ""])
        return {
            "id": trace_id, "ts": ts, "model_id": model_id, "task": task, "status": status,
            "duration_ms": float(duration_ms), "cold": bool(cold), "error": error, "tags": tags,
            "session": session, "backend": backend, "input": _as_map(inp), "output": _as_map(out) if out is not None else None,
            "text": words, "input_chars": len(_json(inp) or ""), "output_chars": len(_json(out) or ""),
        }

    def _write(self, items: List[Any]) -> None:
        """The queued traces (``record``'s tuples, or finished documents) written in one transaction."""
        docs = [item if isinstance(item, dict) else self._trace_doc(item) for item in items]
        db = self._db()
        for doc in docs:
            self._ensure_bucket(db, bucket_for(doc["ts"]))
        self._commit(db, lambda: [self._insert(db, TRACE, doc, bucket_for(doc["ts"])) for doc in docs])

    def flush(self) -> None:
        """Block until every trace recorded so far is written (tests)."""
        self.writer.flush()

    def close(self) -> None:
        """Write what is queued and stop the writer."""
        self.writer.close()

    def _prune(self) -> List[str]:
        """Drop the day buckets older than the retention; returns their names."""
        db = self._db()
        dropped = expired_buckets(self._type_buckets(db), self.retention_days)
        for name in dropped:
            with self._schema_lock:
                db.command("sql", f"ALTER TYPE {TRACE} BUCKET -{name}")
                db.command("sql", f"DROP BUCKET {name}")
                self._buckets.discard(name)
            logger.info("Dropped expired traces %s", name)
        return dropped

    prune = on_pool(_prune)

    def _trace_rows(self, rows: List[Dict[str, Any]], full: bool) -> List[Dict[str, Any]]:
        out = []
        for r in rows:
            text = r.pop("text", "") or ""
            r["preview"] = text[:PREVIEW_CHARS]
            if not full:
                r.pop("input", None)
                r.pop("output", None)
            out.append(r)
        return out

    def _traces(self, filters: Dict[str, Any], limit: int) -> List[Dict[str, Any]]:
        db = self._db()
        where = ["id IS NOT NULL"]
        params: Dict[str, Any] = {}
        # (:before would be a reserved word, hence :cursor.)
        simple = {"task": "task = :task", "status": "status = :status", "session": "session = :session",
                  "tag": "tags CONTAINS :tag", "since": "ts >= :since", "until": "ts <= :until", "before": "ts < :cursor"}
        for key, clause in simple.items():
            if filters.get(key) not in (None, ""):
                where.append(clause)
                params[clause.rsplit(":", 1)[1]] = filters[key]
        if filters.get("model"):
            where.append("model_id ILIKE :model")
            params["model"] = f"%{filters['model']}%"
        if filters.get("q"):
            where.append("text CONTAINSTEXT :q")
            params["q"] = filters["q"]
        sql = (f"SELECT id, ts, model_id, task, status, duration_ms, cold, error, tags, session, backend, input_chars, "
               f"output_chars, text FROM {TRACE} WHERE {' AND '.join(where)} ORDER BY ts DESC LIMIT {int(limit)}")
        return self._trace_rows(self._rows(db, sql, params), full=False)

    async def traces(self, limit: int = 100, **filters: Any) -> List[Dict[str, Any]]:
        """Newest first. Filters: since, until, before (a ts cursor), model (substring), task, status, tag, session, q (words)."""
        return await self._run(self._traces, filters, limit)

    def _trace(self, trace_id: str) -> Dict[str, Any]:
        rows = self._select(f"SELECT FROM {TRACE} WHERE id = :id", {"id": trace_id})
        if not rows:
            raise NotFound(f"No trace {trace_id!r}")
        return self._trace_rows(rows, full=True)[0]

    async def trace(self, trace_id: str) -> Dict[str, Any]:
        return await self._run(self._trace, trace_id)

    # -------------------------------------------------------------- datasets
    def _dataset_rows(self, db, where: str = "", params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        rows = self._rows(db, f"SELECT FROM {DATASET} {where} ORDER BY created DESC", params)
        counts = {r["dataset_id"]: r["n"] for r in self._rows(db, f"SELECT dataset_id, count(*) AS n FROM {EXAMPLE} GROUP BY dataset_id")}
        runs = {r["dataset_id"]: r["n"] for r in self._rows(db, f"SELECT dataset_id, count(*) AS n FROM {RUN} GROUP BY dataset_id")}
        for r in rows:
            r["examples"] = counts.get(r["id"], 0)
            r["runs"] = runs.get(r["id"], 0)
        return rows

    async def datasets(self) -> List[Dict[str, Any]]:
        return await self._run(lambda: self._dataset_rows(self._db()))

    def _dataset(self, dataset_id: str) -> Dict[str, Any]:
        rows = self._dataset_rows(self._db(), "WHERE id = :id", {"id": dataset_id})
        if not rows:
            raise NotFound(f"No dataset {dataset_id!r}")
        return rows[0]

    async def dataset(self, dataset_id: str) -> Dict[str, Any]:
        return await self._run(self._dataset, dataset_id)

    def _create_dataset(self, name: str, description: str) -> Dict[str, Any]:
        db = self._db()
        if self._rows(db, f"SELECT id FROM {DATASET} WHERE name = :n", {"n": name}):
            raise EvalError(f"A dataset named {name!r} already exists")
        doc = {"id": new_id(), "name": name, "description": description, "created": time.time()}
        self._commit(db, lambda: self._insert(db, DATASET, doc))
        return {**doc, "examples": 0, "runs": 0}

    async def create_dataset(self, name: str, description: str = "") -> Dict[str, Any]:
        return await self._run(self._create_dataset, name, description)

    def _delete_dataset(self, dataset_id: str) -> None:
        db = self._db()
        self._dataset(dataset_id)
        run_ids = [r["id"] for r in self._rows(db, f"SELECT id FROM {RUN} WHERE dataset_id = :d", {"d": dataset_id})]

        def delete() -> None:
            for run_id in run_ids:
                db.command("sql", f"DELETE FROM {SCORE} WHERE run_id = :r", {"r": run_id})
                db.command("sql", f"DELETE FROM {ITEM} WHERE run_id = :r", {"r": run_id})
            db.command("sql", f"DELETE FROM {RUN} WHERE dataset_id = :d", {"d": dataset_id})
            db.command("sql", f"DELETE FROM {EXAMPLE} WHERE dataset_id = :d", {"d": dataset_id})
            db.command("sql", f"DELETE FROM {DATASET} WHERE id = :d", {"d": dataset_id})

        self._commit(db, delete)

    async def delete_dataset(self, dataset_id: str) -> None:
        await self._run(self._delete_dataset, dataset_id)

    # -------------------------------------------------------------- examples
    def _example_doc(self, dataset_id: str, example: Dict[str, Any]) -> Dict[str, Any]:
        """The example as given: a run sends its input to the model as it is, so nothing is cut here."""
        inp = example.get("input")
        reference = example.get("reference")
        return {
            "id": new_id(), "dataset_id": dataset_id, "created": time.time(), "task": example.get("task"),
            "input": _as_map(inp), "reference": _json(reference), "text": text_of([inp, reference]),
            "tags": list(example.get("tags") or []), "source_trace_id": example.get("source_trace_id"),
            "source_model_id": example.get("source_model_id"),
            "source_output": _as_map(example["source_output"]) if example.get("source_output") is not None else None,
        }

    def _add_examples(self, dataset_id: str, examples: List[Dict[str, Any]]) -> int:
        db = self._db()
        self._dataset(dataset_id)
        docs = [self._example_doc(dataset_id, e) for e in examples]
        self._commit(db, lambda: [self._insert(db, EXAMPLE, doc) for doc in docs])
        return len(docs)

    async def add_examples(self, dataset_id: str, examples: List[Dict[str, Any]]) -> int:
        """Each example: input (required), reference, task, tags."""
        return await self._run(self._add_examples, dataset_id, examples)

    def _add_from_traces(self, dataset_id: str, trace_ids: List[str]) -> int:
        db = self._db()
        self._dataset(dataset_id)
        examples = []
        for trace_id in trace_ids:
            rows = self._rows(db, f"SELECT FROM {TRACE} WHERE id = :id", {"id": trace_id})
            if not rows:
                raise NotFound(f"No trace {trace_id!r}")
            t = rows[0]
            if compacted(t["input"]):
                raise EvalError(f"Trace {trace_id!r}'s input was cut when it was recorded (longer than TRACE_MAX_CHARS, "
                                f"or media): add the example with its whole input instead")
            examples.append({
                "input": t["input"], "task": t["task"], "tags": t.get("tags") or [], "source_trace_id": trace_id,
                "source_model_id": t["model_id"], "source_output": t.get("output") if t.get("status") == "ok" else None,
            })
        return self._add_examples(dataset_id, examples)

    async def add_from_traces(self, dataset_id: str, trace_ids: List[str]) -> int:
        return await self._run(self._add_from_traces, dataset_id, trace_ids)

    def _example_rows(self, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        for r in rows:
            r["reference"] = _loads(r.get("reference"))
            r["preview"] = (r.pop("text", "") or "")[:PREVIEW_CHARS]
        return rows

    def _examples(self, dataset_id: str, limit: int, skip: int) -> List[Dict[str, Any]]:
        rows = self._select(f"SELECT FROM {EXAMPLE} WHERE dataset_id = :d ORDER BY created ASC, id ASC SKIP {int(skip)} LIMIT {int(limit)}",
                          {"d": dataset_id})
        return self._example_rows(rows)

    async def examples(self, dataset_id: str, limit: int = 100, skip: int = 0) -> List[Dict[str, Any]]:
        return await self._run(self._examples, dataset_id, limit, skip)

    def _all_examples(self, dataset_id: str) -> Iterator[Dict[str, Any]]:
        """Every example of the dataset, a page at a time. Blocking."""
        skip = 0
        while True:
            page = self._examples(dataset_id, EXAMPLE_PAGE, skip)
            yield from page
            if len(page) < EXAMPLE_PAGE:
                return
            skip += EXAMPLE_PAGE

    def _delete_example(self, dataset_id: str, example_id: str) -> None:
        db = self._db()
        if not self._rows(db, f"SELECT id FROM {EXAMPLE} WHERE id = :id AND dataset_id = :d", {"id": example_id, "d": dataset_id}):
            raise NotFound(f"No example {example_id!r}")

        def delete() -> None:
            db.command("sql", f"DELETE FROM {SCORE} WHERE example_id = :e", {"e": example_id})
            db.command("sql", f"DELETE FROM {ITEM} WHERE example_id = :e", {"e": example_id})
            db.command("sql", f"DELETE FROM {EXAMPLE} WHERE id = :e", {"e": example_id})

        self._commit(db, delete)

    async def delete_example(self, dataset_id: str, example_id: str) -> None:
        await self._run(self._delete_example, dataset_id, example_id)

    def _export(self, dataset_id: str) -> str:
        self._dataset(dataset_id)
        lines = []
        for e in self._all_examples(dataset_id):
            lines.append(json.dumps({k: e.get(k) for k in ("id", "task", "input", "reference", "tags", "source_model_id", "source_output")},
                                    ensure_ascii=False, default=str))
        return "\n".join(lines) + ("\n" if lines else "")

    async def export(self, dataset_id: str) -> str:
        """The examples as JSON Lines, one object per line, as POST .../examples takes them back."""
        return await self._run(self._export, dataset_id)

    # ------------------------------------------------------------------ runs
    def _create_run(self, dataset_id: str, model_id: str, task: Optional[str], params: Dict[str, Any], name: str,
                    webhook_url: Optional[str]) -> Dict[str, Any]:
        db = self._db()
        self._dataset(dataset_id)
        doc = {
            "id": new_id(), "dataset_id": dataset_id, "name": name or model_id, "model_id": model_id, "task": task,
            "params": params or {}, "status": "queued", "created": time.time(), "started": None, "finished": None,
            "total": 0, "done": 0, "failed": 0, "error": None, "webhook_url": webhook_url,
        }
        self._commit(db, lambda: self._insert(db, RUN, doc))
        return doc

    async def create_run(self, dataset_id: str, model_id: str, task: Optional[str], params: Dict[str, Any], name: str = "",
                         webhook_url: Optional[str] = None) -> Dict[str, Any]:
        """A queued run; ``webhook_url`` is POSTed to when it finishes, besides WEBHOOK_URLS."""
        return await self._run(self._create_run, dataset_id, model_id, task, params, name, webhook_url)

    def _runs(self, dataset_id: Optional[str]) -> List[Dict[str, Any]]:
        db = self._db()
        if dataset_id:
            return self._rows(db, f"SELECT FROM {RUN} WHERE dataset_id = :d ORDER BY created DESC", {"d": dataset_id})
        return self._rows(db, f"SELECT FROM {RUN} ORDER BY created DESC")

    async def runs(self, dataset_id: Optional[str] = None) -> List[Dict[str, Any]]:
        return await self._run(self._runs, dataset_id)

    def _get_run(self, run_id: str) -> Dict[str, Any]:
        rows = self._select(f"SELECT FROM {RUN} WHERE id = :id", {"id": run_id})
        if not rows:
            raise NotFound(f"No run {run_id!r}")
        return rows[0]

    async def run(self, run_id: str) -> Dict[str, Any]:
        return await self._run(self._get_run, run_id)

    def _update_run(self, run_id: str, fields: Dict[str, Any]) -> None:
        db = self._db()
        allowed = {k: v for k, v in fields.items() if k in TABLES["runs"].fields}
        if not allowed:
            return
        sets = ", ".join(f"{k} = :{k}" for k in allowed)
        self._commit(db, lambda: db.command("sql", f"UPDATE {RUN} SET {sets} WHERE id = :id", {**allowed, "id": run_id}))

    async def update_run(self, run_id: str, **fields: Any) -> None:
        await self._run(self._update_run, run_id, fields)

    def _delete_run(self, run_id: str) -> None:
        db = self._db()
        self._get_run(run_id)

        def delete() -> None:
            db.command("sql", f"DELETE FROM {SCORE} WHERE run_id = :r", {"r": run_id})
            db.command("sql", f"DELETE FROM {ITEM} WHERE run_id = :r", {"r": run_id})
            db.command("sql", f"DELETE FROM {RUN} WHERE id = :r", {"r": run_id})

        self._commit(db, delete)

    async def delete_run(self, run_id: str) -> None:
        await self._run(self._delete_run, run_id)

    def _item(self, run_id: str, example_id: str, output: Any, status: str, duration_ms: float, cold: bool,
              error: Optional[str] = None) -> Dict[str, Any]:
        out = compact(output, self.max_chars) if output is not None else None
        return {
            "id": new_id(), "run_id": run_id, "example_id": example_id, "created": time.time(),
            "output": _as_map(out) if out is not None else None, "text": output_text(out) if out is not None else "",
            "status": status, "error": error, "duration_ms": float(duration_ms), "cold": bool(cold),
        }

    def _add_items(self, run_id: str, items: List[Dict[str, Any]]) -> None:
        db = self._db()
        docs = [self._item(run_id, **item) for item in items]
        self._commit(db, lambda: [self._insert(db, ITEM, doc) for doc in docs])

    async def add_items(self, run_id: str, items: List[Dict[str, Any]]) -> None:
        """Store a page of the run's answers in one transaction (about 25 times faster than one each); each item is
        ``example_id``, ``output``, ``status``, ``duration_ms``, ``cold`` and ``error``."""
        if items:
            await self._run(self._add_items, run_id, items)

    def _answered(self, run_id: str) -> set:
        """The examples the run has an item for (a resumed run skips them)."""
        return {r["example_id"] for r in self._select(f"SELECT example_id FROM {ITEM} WHERE run_id = :r", {"r": run_id})}

    async def answered(self, run_id: str) -> set:
        return await self._run(self._answered, run_id)

    def _items(self, run_id: str) -> List[Dict[str, Any]]:
        """The run's items with their example's input and reference, in the examples' order."""
        db = self._db()
        run = self._get_run(run_id)
        examples = {e["id"]: e for e in self._all_examples(run["dataset_id"])}
        items = self._rows(db, f"SELECT FROM {ITEM} WHERE run_id = :r", {"r": run_id})
        by_example = {i["example_id"]: i for i in items}
        rows = []
        for example_id, e in examples.items():
            item = by_example.get(example_id)
            if item is None:
                continue
            item.pop("text", None)
            rows.append({**item, "input": e["input"], "reference": e["reference"], "task": e["task"],
                         "source_output": e.get("source_output"), "source_model_id": e.get("source_model_id")})
        return rows

    async def items(self, run_id: str) -> List[Dict[str, Any]]:
        return await self._run(self._items, run_id)

    # ---------------------------------------------------------------- scores
    def _replace_scores(self, run_id: str, metric: str, scores: List[Dict[str, Any]]) -> None:
        """Store ``scores`` for ``metric`` on the run, in place of any it had."""
        db = self._db()
        docs = [{
            "id": new_id(), "run_id": run_id, "item_id": s["item_id"], "example_id": s["example_id"], "metric": metric,
            "value": None if s.get("value") is None else float(s["value"]), "label": s.get("label"),
            "rationale": s.get("rationale"), "judge": s.get("judge"), "against": s.get("against"), "created": time.time(),
        } for s in scores]
        def work():
            db.command("sql", f"DELETE FROM {SCORE} WHERE run_id = :r AND metric = :m", {"r": run_id, "m": metric})
            for doc in docs:
                self._insert(db, SCORE, doc)

        self._commit(db, work)

    async def replace_scores(self, run_id: str, metric: str, scores: List[Dict[str, Any]]) -> None:
        await self._run(self._replace_scores, run_id, metric, scores)

    def _scores(self, run_id: str) -> List[Dict[str, Any]]:
        return self._select(f"SELECT FROM {SCORE} WHERE run_id = :r", {"r": run_id})

    async def scores(self, run_id: str) -> List[Dict[str, Any]]:
        return await self._run(self._scores, run_id)


def summarize_scores(scores: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Per metric: how many items were scored, the mean value, and for pairwise judgments the wins, ties and losses."""
    summary: Dict[str, Dict[str, Any]] = {}
    for s in scores:
        m = summary.setdefault(s["metric"], {"count": 0, "scored": 0, "mean": None, "_sum": 0.0, "wins": 0, "ties": 0, "losses": 0})
        m["count"] += 1
        if s.get("value") is not None:
            m["scored"] += 1
            m["_sum"] += s["value"]
        if s.get("against"):
            label = s.get("label")
            if label == "win":
                m["wins"] += 1
            elif label == "tie":
                m["ties"] += 1
            elif label == "loss":
                m["losses"] += 1
    for m in summary.values():
        m["mean"] = m.pop("_sum") / m["scored"] if m["scored"] else None
        m.pop("_sum", None)
        if not m["wins"] and not m["ties"] and not m["losses"]:
            for k in ("wins", "ties", "losses"):
                m.pop(k)
    return summary
