"""
Datasets, runs and scores in the embedded DuckDB database.

Shares the database and its thread pool with every store (db/engine.py),
so every call here blocks and runs there, never on the event loop.

Datasets hold examples (an input, maybe a reference answer), copied from
traces or added directly, kept as given (a trace whose input was cut is
refused, since a run would then measure something else); they are never
expired. An example without a reference is one to label: ``to_label`` is
the queue of them, each with a suggestion (a run's output for it, else what
the traced model answered), the least confident first, and the reference
a labeler picks (from the dataset's ``labels``) or types is set with
``set_reference``. The labeled examples are what the text-classifier job
trains on (``training_rows``). A run is one model over a dataset, its items the outputs; scores
belong to items (see scoring.py). These are /db tables in the ``evals``
workspace (db/tables.py): browsable in /admin's Data section,
read-only there, written only here.
"""

import json
import logging
import time
import uuid
from typing import Any, Dict, Iterator, List, Optional, Tuple

from core.errors import NotFound, Refused
from db import Database, DocumentStore, Where, on_pool
from db.tables import TableSchema, TableStore, decode_cursor, decoded, encode_cursor, schema_of, table_type
from models.capture import as_map, compact, compacted, output_text, text_of

logger = logging.getLogger(__name__)

WORKSPACE = "evals"
DATASET, EXAMPLE, RUN, ITEM, SCORE = (table_type(WORKSPACE, t) for t in ("datasets", "examples", "runs", "items", "scores"))
PREVIEW_CHARS = 240

# The evals workspace's tables (each has a unique string id besides).
TABLES: Dict[str, TableSchema] = {
    # labels: the choices an example's reference is picked from when labeling (none: typed)
    "datasets": schema_of("name:string!", "description:string", "created:number", "labels:string[]"),
    "examples": schema_of("dataset_id:string+", "created:number", "task:string", "input:object", "reference:string",
                        "text:string", "tags:string[]", "source_trace_id:string", "source_model_id:string",
                        "source_output:object"),
    # A run's status, times and error are its newest task's (evals/runner.py).
    "runs": schema_of("dataset_id:string+", "name:string", "model_id:string", "task:string", "params:object",
                    "created:number", "total:integer", "done:integer", "failed:integer", "webhook_url:string"),
    "items": schema_of("run_id:string+", "example_id:string+", "created:number", "output:object", "text:string",
                     "status:string", "error:string", "duration_ms:number", "cold:boolean"),
    "scores": schema_of("run_id:string+", "item_id:string+", "example_id:string+", "metric:string", "value:number",
                      "label:string", "rationale:string", "judge:string", "against:string", "created:number"),
}
# Examples are read this many at a time (runs, exports, comparisons).
EXAMPLE_PAGE = 500


def new_id() -> str:
    return uuid.uuid4().hex


def _json(value: Any) -> Optional[str]:
    return None if value is None else json.dumps(value, ensure_ascii=False)


def reference_text(reference: Any) -> Optional[str]:
    """A reference as text: itself, its text, answer, label or output field, else its JSON."""
    if reference is None:
        return None
    if isinstance(reference, str):
        return reference
    if isinstance(reference, dict):
        for key in ("text", "answer", "label", "output"):
            if isinstance(reference.get(key), str):
                return reference[key]
    return json.dumps(reference, ensure_ascii=False, default=str)


def _loads(raw: Any) -> Any:
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return raw


class EvalStore(DocumentStore):
    """Datasets, runs and scores; see the module docstring. ``max_chars`` cuts a run's outputs as traces are."""

    WORKSPACE = WORKSPACE
    OWNER = "evals: change them in /admin's Evals section"
    TABLES = TABLES

    def __init__(self, database: Database, tables: TableStore, max_chars: int = 8000):
        super().__init__(database, tables)
        self.max_chars = max_chars

    # -------------------------------------------------------------- datasets
    def _dataset_rows(self, db, where: str = "", params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        rows = [decoded(r) for r in self._rows(db, f"SELECT * FROM {DATASET} {where} ORDER BY created DESC", params)]
        # Only these datasets' examples and runs are counted: one dataset's lookup comes before every write to it.
        ids = {"ids": [r["id"] for r in rows]}
        counts, runs = ({r["dataset_id"]: r["n"] for r in self._rows(
            db, f"SELECT dataset_id, count(*) AS n FROM {t} WHERE dataset_id = ANY($ids) GROUP BY dataset_id", ids)}
            for t in (EXAMPLE, RUN))
        for r in rows:
            r["examples"] = counts.get(r["id"], 0)
            r["runs"] = runs.get(r["id"], 0)
            r["labels"] = r.get("labels") or []
        return rows

    @on_pool
    def datasets(self) -> List[Dict[str, Any]]:
        return self._dataset_rows(self._db())

    def _dataset(self, dataset_id: str) -> Dict[str, Any]:
        rows = self._dataset_rows(self._db(), "WHERE id = $id", {"id": dataset_id})
        if not rows:
            raise NotFound(f"No dataset {dataset_id!r}")
        return rows[0]

    dataset = on_pool(_dataset)

    @on_pool
    def create_dataset(self, name: str, description: str = "", labels: Optional[List[str]] = None) -> Dict[str, Any]:
        db = self._db()
        if self._rows(db, f"SELECT id FROM {DATASET} WHERE name = $n", {"n": name}):
            raise Refused(f"A dataset named {name!r} already exists")
        doc = {"id": new_id(), "name": name, "description": description, "created": time.time(), "labels": labels or []}
        self._commit(db, lambda: self._t.write_row(db, WORKSPACE, "datasets", doc))
        return {**doc, "examples": 0, "runs": 0}

    @on_pool
    def set_labels(self, dataset_id: str, labels: List[str]) -> Dict[str, Any]:
        """Set the choices the dataset's references are picked from when labeling."""
        db = self._db()
        dataset = self._dataset(dataset_id)
        doc = {**{k: dataset[k] for k in ("id", "name", "description", "created")}, "labels": labels}
        self._commit(db, lambda: self._t.write_row(db, WORKSPACE, "datasets", doc))
        return self._dataset(dataset_id)

    @on_pool
    def delete_dataset(self, dataset_id: str) -> None:
        db = self._db()
        self._dataset(dataset_id)
        run_ids = [r["id"] for r in self._rows(db, f"SELECT id FROM {RUN} WHERE dataset_id = $d", {"d": dataset_id})]

        def delete() -> None:
            for run_id in run_ids:
                db.execute(f"DELETE FROM {SCORE} WHERE run_id = $r", {"r": run_id})
                db.execute(f"DELETE FROM {ITEM} WHERE run_id = $r", {"r": run_id})
            db.execute(f"DELETE FROM {RUN} WHERE dataset_id = $d", {"d": dataset_id})
            db.execute(f"DELETE FROM {EXAMPLE} WHERE dataset_id = $d", {"d": dataset_id})
            db.execute(f"DELETE FROM {DATASET} WHERE id = $d", {"d": dataset_id})

        self._commit(db, delete)

    # -------------------------------------------------------------- examples
    def _example_doc(self, dataset_id: str, example: Dict[str, Any]) -> Dict[str, Any]:
        """The example as given: a run sends its input to the model as it is, so nothing is cut here."""
        inp = example.get("input")
        reference = example.get("reference")
        return {
            "id": new_id(), "dataset_id": dataset_id, "created": time.time(), "task": example.get("task"),
            "input": as_map(inp), "reference": _json(reference), "text": text_of([inp, reference]),
            "tags": list(example.get("tags") or []), "source_trace_id": example.get("source_trace_id"),
            "source_model_id": example.get("source_model_id"),
            "source_output": as_map(example["source_output"]) if example.get("source_output") is not None else None,
        }

    def _add_examples(self, dataset_id: str, examples: List[Dict[str, Any]]) -> int:
        """Each example: input (required), reference, task, tags."""
        db = self._db()
        self._dataset(dataset_id)
        docs = [self._example_doc(dataset_id, e) for e in examples]
        self._commit(db, lambda: self._t.write_rows(db, WORKSPACE, "examples", docs))
        return len(docs)

    add_examples = on_pool(_add_examples)

    @on_pool
    def add_from_traces(self, dataset_id: str, traces: List[Dict[str, Any]]) -> int:
        """Examples copied from ``traces`` (the request log's: monitoring/store.py): each its input, and the output it
        got when it went well; Refused for one whose input was cut when it was kept."""
        self._dataset(dataset_id)
        examples = []
        for t in traces:
            trace_id = t["id"]
            if compacted(t["input"]):
                raise Refused(f"Trace {trace_id!r}'s input was cut when it was recorded (longer than TRACE_MAX_CHARS, "
                                f"or media): add the example with its whole input instead")
            examples.append({
                "input": t["input"], "task": t["task"], "tags": t.get("tags") or [], "source_trace_id": trace_id,
                "source_model_id": t["model_id"], "source_output": t.get("output") if t.get("status") == "ok" else None,
            })
        return self._add_examples(dataset_id, examples)

    def _example_rows(self, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        for r in rows:
            r["reference"] = _loads(r.get("reference"))
            r["preview"] = (r.pop("text", "") or "")[:PREVIEW_CHARS]
        return rows

    def _examples(self, dataset_id: str, limit: int = 100, skip: int = 0) -> List[Dict[str, Any]]:
        rows = [decoded(r) for r in self._select(f"SELECT * FROM {EXAMPLE} WHERE dataset_id = $d ORDER BY created ASC, "
                                                 f"id ASC LIMIT {int(limit)} OFFSET {int(skip)}", {"d": dataset_id})]
        return self._example_rows(rows)

    examples = on_pool(_examples)

    def _all_examples(self, dataset_id: str) -> Iterator[Dict[str, Any]]:
        """Every example of the dataset, a page at a time. Blocking."""
        skip = 0
        while True:
            page = self._examples(dataset_id, EXAMPLE_PAGE, skip)
            yield from page
            if len(page) < EXAMPLE_PAGE:
                return
            skip += EXAMPLE_PAGE

    @on_pool
    def delete_example(self, dataset_id: str, example_id: str) -> None:
        db = self._db()
        if not self._rows(db, f"SELECT id FROM {EXAMPLE} WHERE id = $id AND dataset_id = $d", {"id": example_id, "d": dataset_id}):
            raise NotFound(f"No example {example_id!r}")

        def delete() -> None:
            db.execute(f"DELETE FROM {SCORE} WHERE example_id = $e", {"e": example_id})
            db.execute(f"DELETE FROM {ITEM} WHERE example_id = $e", {"e": example_id})
            db.execute(f"DELETE FROM {EXAMPLE} WHERE id = $e", {"e": example_id})

        self._commit(db, delete)

    @on_pool
    def set_reference(self, dataset_id: str, example_id: str, reference: Any) -> Dict[str, Any]:
        """Set the example's reference; None takes it away, so it is one to label again. The runs' scores of it stay
        as they were scored: score the runs again."""
        db = self._db()
        row = self._one(f"SELECT * FROM {EXAMPLE} WHERE id = $id AND dataset_id = $d", {"id": example_id, "d": dataset_id})
        if row is None:
            raise NotFound(f"No example {example_id!r}")
        doc = {k: v for k, v in decoded(row).items() if k in TABLES["examples"].fields or k == "id"}
        doc.update(reference=_json(reference), text=text_of([doc.get("input"), reference]))
        self._commit(db, lambda: self._t.write_row(db, WORKSPACE, "examples", doc))
        return self._example_rows([decoded(self._one(f"SELECT * FROM {EXAMPLE} WHERE id = $id", {"id": example_id}))])[0]

    @on_pool
    def to_label(self, dataset_id: str, run_id: Optional[str], limit: int, skip: int = 0
                 ) -> Tuple[List[Dict[str, Any]], int]:
        """The examples without a reference, ``limit`` of them after the first ``skip``, and how many there are.
        Each has its ``suggestion``: what run ``run_id`` answered for it, else what the traced model did, else None.
        The least confident suggestion comes first (its ``confidence``), then those without one, oldest first.

        A labeled example leaves the queue, so the examples a labeler passed over stay at its front: the next page
        is after those (``skip``), whatever was labeled since."""
        self._dataset(dataset_id)
        suggestion = "coalesce(i.output, e.source_output)"
        rows = self._select(
            f"SELECT e.*, {suggestion} AS suggestion FROM {EXAMPLE} e LEFT JOIN {ITEM} i "
            f"ON i.example_id = e.id AND i.run_id = $r WHERE e.dataset_id = $d AND e.reference IS NULL "
            f"ORDER BY TRY_CAST(json_extract({suggestion}, '$.confidence') AS DOUBLE) ASC NULLS LAST, e.created, e.id "
            f"LIMIT {int(limit)} OFFSET {int(skip)}", {"d": dataset_id, "r": run_id})
        left = self._one(f"SELECT count(*) AS n FROM {EXAMPLE} WHERE dataset_id = $d AND reference IS NULL",
                         {"d": dataset_id})["n"]
        examples = []
        for r in rows:
            suggestion = r.pop("suggestion")
            examples.append({**self._example_rows([decoded(r)])[0], "suggestion": suggestion})
        return examples, left

    @on_pool
    def training_rows(self, dataset_id: str) -> List[Dict[str, str]]:
        """The labeled examples a classifier learns from: each one's text (its input's ``text``) and label (its
        reference, as text), for those with both. Labels that differ only in case are one, spelled as first seen, as
        the classification report counts them."""
        self._dataset(dataset_id)
        rows, spelled = [], {}
        for e in self._all_examples(dataset_id):
            text, label = (e.get("input") or {}).get("text"), (reference_text(e.get("reference")) or "").strip()
            if isinstance(text, str) and text.strip() and label:
                rows.append({"text": text, "label": spelled.setdefault(label.casefold(), label)})
        return rows

    @on_pool
    def export(self, dataset_id: str) -> str:
        """The examples as JSON Lines, one object per line, as POST .../examples takes them back."""
        self._dataset(dataset_id)
        lines = []
        for e in self._all_examples(dataset_id):
            lines.append(json.dumps({k: e.get(k) for k in ("id", "task", "input", "reference", "tags", "source_model_id", "source_output")},
                                    ensure_ascii=False, default=str))
        return "\n".join(lines) + ("\n" if lines else "")

    # ------------------------------------------------------------------ runs
    @on_pool
    def create_run(self, dataset_id: str, model_id: str, task: Optional[str], params: Dict[str, Any], name: str = "",
                   webhook_url: Optional[str] = None) -> Dict[str, Any]:
        """A run, to be queued (RunExecutor.submit); ``webhook_url`` is POSTed to when it finishes, besides
        WEBHOOK_URLS."""
        db = self._db()
        self._dataset(dataset_id)
        doc = {
            "id": new_id(), "dataset_id": dataset_id, "name": name or model_id, "model_id": model_id, "task": task,
            "params": params or {}, "created": time.time(), "total": 0, "done": 0, "failed": 0,
            "webhook_url": webhook_url,
        }
        self._commit(db, lambda: self._t.write_row(db, WORKSPACE, "runs", doc))
        return doc

    @on_pool
    def runs(self, dataset_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Every run (of one dataset), newest first."""
        where = Where().add("dataset_id", "=", dataset_id)
        return [decoded(r) for r in self._select(f"SELECT * FROM {RUN}{where} ORDER BY _seq DESC", where.params)]

    @on_pool
    def run_page(self, dataset_id: Optional[str], limit: int, cursor: Optional[str]
                 ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        """A page of the runs (of one dataset), newest first, after the page whose ``next`` is ``cursor``, and this
        page's next: by write number (``_seq``), so a page goes on where the last stopped however many came since."""
        where = Where().add("dataset_id", "=", dataset_id).add("_seq", "<", decode_cursor(cursor, "s") if cursor else None)
        rows = self._select(f"SELECT * FROM {RUN}{where} ORDER BY _seq DESC LIMIT {int(limit) + 1}", where.params)
        after = encode_cursor(rows[limit - 1]["_seq"], "s") if len(rows) > limit else None
        return [decoded(r) for r in rows[:limit]], after

    def _get_run(self, run_id: str) -> Dict[str, Any]:
        run = self._one(f"SELECT * FROM {RUN} WHERE id = $id", {"id": run_id})
        if run is None:
            raise NotFound(f"No run {run_id!r}")
        return decoded(run)

    run = on_pool(_get_run)

    @on_pool
    def update_run(self, run_id: str, **fields: Any) -> None:
        db = self._db()
        allowed = {k: v for k, v in fields.items() if k in TABLES["runs"].fields}
        if not allowed:
            return
        fields = TABLES["runs"].fields
        # An object field's value goes in as its JSON text.
        objects = {k for k in allowed if fields[k].type in ("object", "any")}
        values = {k: (json.dumps(v) if k in objects and v is not None else v) for k, v in allowed.items()}
        sets = ", ".join(f"{k} = ${k}{'::JSON' if k in objects else ''}" for k in allowed)
        self._commit(db, lambda: db.execute(f"UPDATE {RUN} SET {sets} WHERE id = $id", {**values, "id": run_id}))

    @on_pool
    def delete_run(self, run_id: str) -> None:
        db = self._db()
        self._get_run(run_id)

        def delete() -> None:
            db.execute(f"DELETE FROM {SCORE} WHERE run_id = $r", {"r": run_id})
            db.execute(f"DELETE FROM {ITEM} WHERE run_id = $r", {"r": run_id})
            db.execute(f"DELETE FROM {RUN} WHERE id = $r", {"r": run_id})

        self._commit(db, delete)

    def _item(self, run_id: str, example_id: str, output: Any, status: str, duration_ms: float, cold: bool,
              error: Optional[str] = None) -> Dict[str, Any]:
        out = compact(output, self.max_chars) if output is not None else None
        return {
            "id": new_id(), "run_id": run_id, "example_id": example_id, "created": time.time(),
            "output": as_map(out) if out is not None else None, "text": output_text(out) if out is not None else "",
            "status": status, "error": error, "duration_ms": float(duration_ms), "cold": bool(cold),
        }

    async def add_items(self, run_id: str, items: List[Dict[str, Any]]) -> None:
        """Store a page of the run's answers in one transaction (about 25 times faster than one each); each item is
        ``example_id``, ``output``, ``status``, ``duration_ms``, ``cold`` and ``error``."""
        if items:
            await self._run(self._add_items, run_id, items)

    def _add_items(self, run_id: str, items: List[Dict[str, Any]]) -> None:
        db = self._db()
        docs = [self._item(run_id, **item) for item in items]
        self._commit(db, lambda: self._t.write_rows(db, WORKSPACE, "items", docs))

    @on_pool
    def answered(self, run_id: str) -> set:
        """The examples the run has an item for (a resumed run skips them)."""
        return {r["example_id"] for r in self._select(f"SELECT example_id FROM {ITEM} WHERE run_id = $r", {"r": run_id})}

    @on_pool
    def items(self, run_id: str) -> List[Dict[str, Any]]:
        """The run's items with their example's input and reference, in the examples' order."""
        run = self._get_run(run_id)
        examples = {e["id"]: e for e in self._all_examples(run["dataset_id"])}
        items = [decoded(r) for r in self._select(f"SELECT * FROM {ITEM} WHERE run_id = $r", {"r": run_id})]
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

    # ---------------------------------------------------------------- scores
    @on_pool
    def replace_scores(self, run_id: str, metric: str, scores: List[Dict[str, Any]]) -> None:
        """Store ``scores`` for ``metric`` on the run, in place of any it had."""
        db = self._db()
        docs = [{
            "id": new_id(), "run_id": run_id, "item_id": s["item_id"], "example_id": s["example_id"], "metric": metric,
            "value": None if s.get("value") is None else float(s["value"]), "label": s.get("label"),
            "rationale": s.get("rationale"), "judge": s.get("judge"), "against": s.get("against"), "created": time.time(),
        } for s in scores]
        def work():
            db.execute(f"DELETE FROM {SCORE} WHERE run_id = $r AND metric = $m", {"r": run_id, "m": metric})
            self._t.write_rows(db, WORKSPACE, "scores", docs)

        self._commit(db, work)

    @on_pool
    def scores(self, run_id: str) -> List[Dict[str, Any]]:
        return [decoded(r) for r in self._select(f"SELECT * FROM {SCORE} WHERE run_id = $r", {"r": run_id})]
