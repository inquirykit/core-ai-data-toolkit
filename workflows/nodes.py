"""
The node types a workflow is made of, each one entry of ``NODE_TYPES``.

A node type is data and two functions: its label, its settings (a Pydantic
model, so a JSON Schema the editor draws a form from), its outputs, the
JMESPaths its settings hold (compiled when a spec is checked), what it checks
against the service before a spec is saved or run (``check``), and what it
makes of a page's rows (``run``). Both functions get a ``Step``: the node's
id and settings, the graph, the runner (its manager, enrichers and tables),
whether the run writes, and the node's counts to add to. So a new node type
is one entry here, and spec.py and runner.py stay as they are.

A row is a JSON object: the source table's row, its key in ``_row``, and
each node's result under the node's id (``B.label``), read by a JMESPath on
the row (``B.score > `0.8```). ``run`` answers the rows by output name:
``out``, ``true``/``false``, a case's number or ``default``, and ``error``
for the rows it could not do, each with ``_error`` saying why.
"""

import asyncio
import functools
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

import jmespath
from pydantic import BaseModel, ConfigDict, Field

from core.errors import Forbidden, NotFound, Refused, ServiceError, Unavailable, describe
from models import contracts, registry

logger = logging.getLogger(__name__)

Row = Dict[str, Any]
Outputs = Dict[str, List[Row]]
ERROR_CHARS = 500
# What a SQL node's query may give for a page, and take.
SQL_ROWS = 10_000
SQL_TIME_MS = 30_000
# A JMESPath, as a setting's schema says it.
Path = Field(..., min_length=1, max_length=2000, description="A JMESPath on the row")
# An enricher's result status (enrichers/runner.py, Status, which this cannot import; tests/test_vocabularies.py ties
# the two): it answered, it could not be asked, or it is switched off.
ANSWERED = {"ok", "not_found"}
UNASKED = {"error", "rate_limited", "no_key"}
OFF = {"disabled", "not_allowed"}


class RowFailed(Refused):
    """A row a node could not do, saying why in words for whoever reads the rows, an invoke's client among them."""


class NodeFailed(Refused):
    """A node that cannot go on: a row it could not do with no edge from its error output, a write refused, a SQL that
    failed. A run fails with it; a dry run or an invoke answers 400 with it."""

    def __init__(self, node: str, message: str):
        super().__init__(f"Node {node!r}: {message}")
        self.node = node


# ------------------------------------------------------------- rows
def truthy(value: Any) -> bool:
    """JMESPath's truth: false, null, and an empty string, list or object are false; every number is true."""
    return not (value is None or value is False or value in ("", [], {}))


@functools.lru_cache(maxsize=1024)
def _compiled(expression: str) -> Any:
    return jmespath.compile(expression)


def search(expression: str, row: Row) -> Any:
    """``expression`` (a JMESPath) on ``row``, compiled once."""
    return _compiled(expression).search(row)


# Where an expression's names are read from the element at hand, not the row: the right of these.
_ELEMENTWISE = {"subexpression", "index_expression", "projection", "filter_projection", "value_projection", "flatten",
                "pipe"}


def reads(expression: str) -> List[Tuple[str, Optional[str]]]:
    """The names ``expression`` reads off the row itself, each with the name it reads under that one when it does
    (``B.label > `1` || body``: B and label, and body and None). Names in a filter, a projection's right or a
    function's ``&expression`` are read off each element, not the row, and are left out."""
    def walk(node: Dict[str, Any]) -> List[Tuple[str, Optional[str]]]:
        kind, children = node["type"], node.get("children", [])
        if kind == "field":
            return [(node["value"], None)]
        if kind == "subexpression" and children[0]["type"] == "field":
            under = children[1]["value"] if children[1]["type"] == "field" else None
            return [(children[0]["value"], under)]
        if kind in _ELEMENTWISE:
            return walk(children[0])
        if kind == "expref":
            return []
        return [name for child in children for name in walk(child)]

    return walk(_compiled(expression).parsed)


def fields_of(row: Row) -> Dict[str, Any]:
    """The row's fields that are not the workflow's own (``_row``, ``_error``)."""
    return {k: v for k, v in row.items() if not k.startswith("_")}


def described(e: BaseException) -> str:
    return f"{type(e).__name__}: {e}"[:ERROR_CHARS]


def _plain(value: Any) -> Any:
    """A task's result as JSON: its output model dumped."""
    return value.model_dump(mode="json") if hasattr(value, "model_dump") else value


# --------------------------------------------------------- node types
class Settings(BaseModel):
    """A node type's settings: unknown fields are an error."""

    model_config = ConfigDict(extra="forbid")


@dataclass
class Step:
    """A node at work: its id and checked settings, the graph it is in, the runner whose services it uses (``manager``,
    ``enrich``, ``tables``, ``store``), whether the run writes (a dry run does not), the run's id, whether its rows go
    back to a client (an invoke), and its counts, which ``run`` may add to (a Table write's ``written``, a Task's
    ``revisions``)."""

    id: str
    settings: Any
    graph: Any
    runner: Any
    write: bool = False
    # The run the rows are of; None for a dry run or an invoke.
    run: Optional[str] = None
    client: bool = False
    stats: Dict[str, Any] = field(default_factory=dict)

    def told(self, e: BaseException) -> str:
        """What a row says of ``e``, the error it met here: its type and text, but to a client the type alone of one
        nobody expected (not a refusal, made to be read), unless DEBUG: what the service answers any request with
        (core.errors.describe), so a path or a key in it stays in the log."""
        if not self.client or isinstance(e, ServiceError) or getattr(self.runner, "debug", False):
            return described(e)
        logger.warning("Workflow node %s could not do a row of an invoke: %s", self.id, described(e))
        return describe(e, "The node could not do this row", False)


async def _nothing(step: Step) -> None:
    return None


@dataclass(frozen=True)
class NodeType:
    """What a node type is: the editor's palette is made of these (GET /workflows/nodes)."""

    type: str
    label: str
    description: str
    settings: type
    run: Callable[[Step, List[Row]], Awaitable[Outputs]]
    # The outputs of a node of this type, from its settings; None to the palette when they depend on them.
    outputs: Callable[[Any], List[str]] = lambda s: ["out"]
    fixed_outputs: bool = True
    expressions: Callable[[Any], List[str]] = lambda s: []
    check: Callable[[Step], Awaitable[None]] = _nothing
    # A node of this type puts its result on the row under its id; ``gives`` names the result's fields, None when
    # they are not known before it runs.
    result: bool = False
    gives: Callable[[Any], Optional[List[str]]] = lambda s: None
    # The source takes no edge in.
    source: bool = False


async def each(step: Step, items: List[Row], work: Callable[[Row], Awaitable[Any]]) -> Outputs:
    """``work`` on every row at once, its result under the node's id; a row it fails goes to ``error``.
    Unavailable and a refused model are the run's, not a row's: they stop the rest."""
    failed: List[bool] = [False] * len(items)

    async def one(i: int, row: Row) -> None:
        try:
            row[step.id] = await work(row)
        except (Unavailable, Forbidden):
            raise
        except Exception as e:
            row["_error"] = {"node": step.id, "message": step.told(e)}
            failed[i] = True

    try:
        async with asyncio.TaskGroup() as group:
            for i, row in enumerate(items):
                group.create_task(one(i, row))
    except BaseExceptionGroup as stopped:
        raise stopped.exceptions[0] from None
    return {"out": [r for r, f in zip(items, failed) if not f], "error": [r for r, f in zip(items, failed) if f]}


async def _workspace_there(step: Step) -> None:
    if not await step.runner.tables.workspace_there(step.settings.workspace):
        raise NotFound(f"Node {step.id!r}: no workspace named {step.settings.workspace!r}")


# Table read ----------------------------------------------------------
class TableRead(Settings):
    workspace: str = Field(..., min_length=1, description="The workspace the table is in")
    table: str = Field(..., min_length=1, description="The table whose rows go through the workflow")
    only_new: bool = Field(False, description="Each run reads only the rows written since the last run finished")


async def _read(step: Step, items: List[Row]) -> Outputs:
    # The runner reads the page; the source hands it on.
    return {"out": items}


# Task ----------------------------------------------------------------
class TaskNode(Settings):
    task: str = Field(..., min_length=1, description="The task, as GET /tasks lists it")
    model: str = Field(..., min_length=1, description="The model that does it")
    inputs: Dict[str, str] = Field(..., min_length=1, description="Each of the task's inputs, from a JMESPath on the row")
    params: Dict[str, Any] = Field(default_factory=dict, description="Other inputs of the task, the same for every row")


async def _check_task(step: Step) -> None:
    s = step.settings
    known = set(await asyncio.to_thread(registry.known_tasks)) | set(contracts.defined())
    if s.task not in known:
        raise Refused(f"Node {step.id!r}: no task {s.task!r} (GET /tasks lists them)")
    step.runner.manager.check_allowed(s.model)
    contract = contracts.get(s.task)
    if contract is not None:
        fields = contract.input.model_fields
        unknown = sorted((set(s.inputs) | set(s.params)) - set(fields))
        if unknown:
            raise Refused(f"Node {step.id!r}: {s.task} takes no {', '.join(unknown)}")
        missing = [name for name, f in fields.items() if f.is_required() and name not in s.inputs and name not in s.params]
        if missing:
            raise Refused(f"Node {step.id!r}: {s.task} needs {', '.join(missing)}: give each in inputs or params")


def _task_gives(settings: TaskNode) -> Optional[List[str]]:
    contract = contracts.get(settings.task)
    return list(contract.output.model_fields) if contract is not None else None


async def _task(step: Step, items: List[Row]) -> Outputs:
    s, manager = step.settings, step.runner.manager
    # One gate for the page's rows: one at a time until the model is loaded, then as wide as it takes.
    width = manager.width(s.model, s.task)
    # The Hub commits of the weights that answered: which model did the rows, for good (usually one).
    revisions: List[str] = step.stats.setdefault("revisions", [])

    async def one(row: Row) -> Any:
        request = {**s.params, **{name: search(path, row) for name, path in s.inputs.items()}}
        async with width:
            answer = await manager.run_inference(s.model, s.task, request)
        if answer.get("revision") and answer["revision"] not in revisions:
            revisions.append(answer["revision"])
        return _plain(answer["result"])

    return await each(step, items, one)


# Enrich --------------------------------------------------------------
class EnrichNode(Settings):
    value: str = Path
    enrichers: List[str] = Field(default_factory=list, description="The enrichers to ask; none: every one that takes "
                                                                   "the value's type")


async def _enrich(step: Step, items: List[Row]) -> Outputs:
    s = step.settings
    # A lookup that could not be made (an error, a rate limit, no key; or, for one named, switched off) is the
    # row's error, even when another enricher found something: else the row would go on half done, for good.
    unasked = UNASKED | (OFF if s.enrichers else set())

    async def one(row: Row) -> Dict[str, Any]:
        value = search(s.value, row)
        if value is None or value == "":
            raise RowFailed(f"{s.value} is empty on this row")
        found = await step.runner.enrich(str(value), names=s.enrichers or None)
        failed = [r for r in found["results"] if r.get("status") in unasked]
        if failed:
            raise RowFailed(f"{failed[0].get('enricher')}: {failed[0].get('error') or failed[0].get('status')}")
        if not any(r.get("status") in ANSWERED for r in found["results"]):
            raise RowFailed(f"No enricher looked {value!r} up")
        return found["merged"]

    return await each(step, items, one)


# IF and Switch -------------------------------------------------------
class IfNode(Settings):
    condition: str = Path


async def _if(step: Step, items: List[Row]) -> Outputs:
    held = [truthy(search(step.settings.condition, row)) for row in items]
    return {"true": [r for r, h in zip(items, held) if h], "false": [r for r, h in zip(items, held) if not h]}


class SwitchNode(Settings):
    cases: List[str] = Field(..., min_length=1, max_length=10, description="JMESPaths tried in order; a row takes the "
                                                                           "first that is true, else default")


def _cases(settings: SwitchNode) -> List[str]:
    return [str(i) for i in range(len(settings.cases))] + ["default"]


async def _switch(step: Step, items: List[Row]) -> Outputs:
    outputs: Outputs = {name: [] for name in _cases(step.settings)}
    for row in items:
        case = next((str(i) for i, c in enumerate(step.settings.cases) if truthy(search(c, row))), "default")
        outputs[case].append(row)
    return outputs


# Set and Merge -------------------------------------------------------
class SetNode(Settings):
    fields: Dict[str, str] = Field(..., min_length=1, description="Fields of the row to set, each from a JMESPath")


async def _set(step: Step, items: List[Row]) -> Outputs:
    for row in items:
        for name, expression in step.settings.fields.items():
            row[name] = search(expression, row)
    return {"out": items}


class MergeNode(Settings):
    pass


async def _merge(step: Step, items: List[Row]) -> Outputs:
    merged: Dict[Any, Row] = {}
    for row in items:
        merged.setdefault(row["_row"], {}).update(row)
    return {"out": list(merged.values())}


# SQL -----------------------------------------------------------------
class SqlNode(Settings):
    workspace: str = Field(..., min_length=1, description="The workspace whose tables the SQL reads, by their names")
    query: str = Field(..., min_length=1, max_length=20_000, json_schema_extra={"contentMediaType": "application/sql"},
                       description="One SELECT over rows (the rows that reach this node, each field a column, a nested "
                                   "one JSON) and the workspace's tables. The rows it gives go on, in place of those; a "
                                   "row keeps its place (for a Merge, an invoke) by keeping _row")


async def _check_sql(step: Step) -> None:
    await _workspace_there(step)
    try:
        step.runner.tables.check_sql(step.settings.query)
    except Refused as e:
        raise Refused(f"Node {step.id!r}: {e}") from None


async def _sql(step: Step, items: List[Row]) -> Outputs:
    if not items:
        return {"out": []}
    s = step.settings
    try:
        rows, more = await step.runner.tables.timed_read_over(s.workspace, items, s.query, SQL_ROWS, SQL_TIME_MS)
    except (Refused, NotFound, Unavailable) as e:
        raise NodeFailed(step.id, str(e)) from None
    if more:
        raise NodeFailed(step.id, f"The SQL gave more than {SQL_ROWS} rows for {len(items)}")
    for i, row in enumerate(rows):
        if row.get("_row") is None:  # a row it made: one of its own
            row["_row"] = f"{step.id}#{i}"
    return {"out": rows}


# Table write ---------------------------------------------------------
class TableWrite(Settings):
    workspace: str = Field(..., min_length=1, description="The workspace the table is in")
    table: str = Field(..., min_length=1, description="The table the rows are written to, each merged into the row of "
                                                      "its id (the fields written set, its others kept) or added")
    id: str = Field("id", min_length=1, max_length=2000, description="The row's id, from a JMESPath on the row")
    fields: Dict[str, str] = Field(default_factory=dict, description="The fields to write, each from a JMESPath; none: "
                                                                     "every field of the row not starting with _")
    stamp: bool = Field(True, description="Each row a run writes says which run wrote it, in _run: the run keeps the "
                                          "spec it ran and its models' revisions (GET /workflows/runs/{run})")


async def _write(step: Step, items: List[Row]) -> Outputs:
    s = step.settings
    docs, failed = [], []
    for row in items:
        row_id = search(s.id, row)
        if row_id is None or row_id == "":
            row["_error"] = {"node": step.id, "message": f"{s.id} is empty on this row: it is the row's id"}
            failed.append(row)
            continue
        doc = {name: search(path, row) for name, path in s.fields.items()} if s.fields else fields_of(row)
        if s.stamp and step.run:
            doc["_run"] = step.run
        docs.append({**doc, "id": str(row_id)})
    if step.write and docs:
        try:
            # Merged, not replaced: a run has the rows without their vectors, and writes only the fields it says.
            await step.runner.tables.bulk_merge(s.workspace, s.table, docs)
        except (ValueError, NotFound, Forbidden) as e:
            raise NodeFailed(step.id, described(e)) from e
    step.stats["written"] = len(docs)
    return {"error": failed}


NODE_TYPES: Dict[str, NodeType] = {t.type: t for t in (
    NodeType(type="table.read", label="Table read", settings=TableRead, run=_read, check=_workspace_there, source=True,
             description="The rows of a table, a page at a time"),
    NodeType(type="task", label="Task", settings=TaskNode, run=_task, check=_check_task, result=True, gives=_task_gives,
             outputs=lambda s: ["out", "error"], expressions=lambda s: list(s.inputs.values()),
             description="A model task on each row; its result goes under the node's id"),
    NodeType(type="enrich", label="Enrich", settings=EnrichNode, run=_enrich, result=True,
             outputs=lambda s: ["out", "error"], expressions=lambda s: [s.value],
             description="Look a value of the row up with the enrichers; what they found goes under the node's id"),
    NodeType(type="if", label="IF", settings=IfNode, run=_if, outputs=lambda s: ["true", "false"],
             expressions=lambda s: [s.condition],
             description="Rows the condition holds for go one way, the rest the other"),
    NodeType(type="switch", label="Switch", settings=SwitchNode, run=_switch, outputs=_cases, fixed_outputs=False,
             expressions=lambda s: list(s.cases),
             description="Each row takes the first case that holds, else default"),
    NodeType(type="set", label="Set", settings=SetNode, run=_set, expressions=lambda s: list(s.fields.values()),
             description="Set fields of the row"),
    NodeType(type="merge", label="Merge", settings=MergeNode, run=_merge,
             description="Put back together the rows of one source row that took different ways"),
    NodeType(type="sql", label="SQL", settings=SqlNode, run=_sql, check=_check_sql,
             description="A SELECT over the rows and the workspace's tables: filter, join, reshape, count"),
    NodeType(type="table.write", label="Table write", settings=TableWrite, run=_write, check=_workspace_there,
             outputs=lambda s: [], expressions=lambda s: [s.id, *s.fields.values()],
             description="Write the rows to a table"),
)}


def node_types() -> List[Dict[str, Any]]:
    """The palette: each node type with its settings' JSON Schema and, where they are fixed, its outputs."""
    return [{"type": t.type, "label": t.label, "description": t.description, "source": t.source, "result": t.result,
             "outputs": t.outputs(None) if t.fixed_outputs else None, "settings": t.settings.model_json_schema()}
            for t in NODE_TYPES.values()]


__all__ = ["NODE_TYPES", "NodeFailed", "NodeType", "RowFailed", "Settings", "Step", "each", "fields_of", "node_types",
           "reads", "search", "truthy"]
