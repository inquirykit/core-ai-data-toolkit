"""
Runs workflows: a kind of task (tasks/runner.py), one page of rows at a time.

A run reads its source table a page (``page_size`` rows) at a time, oldest
write first, up to the table's newest write when it started, so rows it
writes to that table itself are not read again in the same run. Each page
goes through the graph in memory, the nodes in order (spec.py), each doing
what its type does (nodes.py): a Task node sends each row as its own request
through the model manager, as wide as the
model takes them at once (``ModelManager.width``), so the batcher forms full
batches, a row that fails fails alone, and requests someone waits on (HIGH)
go first; the run's are its task's priority, low unless said, and leave no
traces. After each page the run keeps how far it read and its counts on its
task (``checkpoint``), so a run the service stopped under goes on from its last page
(``on_interrupt="resume"``); a page cut short is done again, which writes the
same rows again (a Table write merges by id) and asks the enrichers' cache
again. Runs of a workflow never overlap (``one_per_ref``): one may wait while
one runs, and asking for another while one waits gives that one.

A row a Task or Enrich node could not do goes to the node's ``error`` output
with ``_error`` saying why; with no edge from there, the run fails at its last
page. A refusal for want of room for the model (Unavailable) is no row's
fault: the run fails, saying so. A trigger is a schedule of the "workflow"
kind (tasks/schedule.py), which this runner keeps as the workflow is saved.
A finished run, a task, is kept ``keep_days`` (WORKFLOW_RUN_DAYS), then pruned.

A dry run (``dry_run``) takes the first rows of the source through a spec,
saved or not, writing nothing: its counts on each edge, each node's rows in
and out, and a few rows as they left each node, for the editor. An invoke
(``invoke``) takes a caller's rows through a saved workflow in place of its
table's, at once, and answers each row with every result it got: composite
inference (classify, branch, rerank) as one call. It writes nothing either,
so a client may call it, of a workflow the admin made ``invokable``: a
workflow writes as the admin, into tables a client may only read, and its SQL
nodes read as the admin, tables a client key may not among them.

This never reads the settings: api/app.py passes what it needs.
"""

import copy
import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from core.errors import Forbidden, NotFound, Refused, Unavailable
from db.graph import GraphStore
from models import ModelManager
from models.events import RequestContext, request_context
from core.priorities import HIGH, LOW, PRIORITIES
from tasks.runner import Kind, TaskFailed, TaskRunner
from tasks.schedule import ScheduleSpec, Schedulable, Scheduler

from . import spec as specs
from .nodes import NODE_TYPES, NodeFailed, Step
from .spec import Graph, WorkflowSpec
from .store import WorkflowStore

logger = logging.getLogger(__name__)

KIND = "workflow"
# Runs of different workflows at once.
CONCURRENCY = 2
# Rows a dry run takes through, at most, and the rows it shows of each node.
DRY_RUN_ROWS = 50
SAMPLES = 3
# Rows an invoke takes, at most.
INVOKE_ROWS = 100


class WorkflowRunner:
    """The workflows in ``store``, their runs on ``tasks`` and schedules on ``scheduler``. Rows are read from and
    written to ``tables`` (the TableStore, as an admin writes through /db: never a table the service keeps), models
    run through ``manager``, and ``enrich`` is the enrichers' ``EnrichService.enrich``."""

    def __init__(self, store: WorkflowStore, tables: GraphStore, manager: ModelManager, tasks: TaskRunner,
                 scheduler: Scheduler, enrich: Callable[..., Awaitable[Dict[str, Any]]], debug: bool = False,
                 keep_days: float = 0):
        self.store = store
        # DEBUG: an invoke's rows say the text of an error nobody expected, as the service's answers do.
        self.debug = debug
        self.tables = tables
        self.manager = manager
        self.tasks = tasks
        self.scheduler = scheduler
        self.enrich = enrich
        # A finished run is kept WORKFLOW_RUN_DAYS, then goes in the task runner's prune; 0: until its workflow is
        # deleted.
        # A cancel stops the page under way (its work goes through TaskRunner.stoppable).
        tasks.add(Kind(KIND, run=self._run, concurrency=CONCURRENCY, on_interrupt="resume",
                       one_per_ref=True, keep_s=keep_days * 86400 if keep_days > 0 else None))
        scheduler.add(Schedulable(KIND, check=lambda name, params: (name, {}), submit=self._scheduled))

    # ------------------------------------------------------------- checks
    async def check(self, spec: WorkflowSpec) -> Graph:
        """The spec as a graph, checked against the service too, by each node's type (nodes.py): a task is known and
        its model allowed (Forbidden otherwise, as for any request), the enrichers are on, the workspaces are there; then
        what each expression reads of a node's result (``Graph.check_wiring``). Refused or NotFound, saying what."""
        graph = specs.check(spec)
        for id, node in graph.nodes.items():
            await NODE_TYPES[node.type].check(Step(id, graph.settings[id], graph, self))
        graph.check_wiring()
        return graph

    # ----------------------------------------------------------- workflows
    async def list(self, limit: int = 100, offset: int = 0) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        return await self.store.list(limit, offset)

    async def get(self, id: str) -> Dict[str, Any]:
        return await self.store.get(id)

    async def create(self, spec: WorkflowSpec) -> Dict[str, Any]:
        await self.check(spec)
        row = await self.store.create(spec.model_dump(mode="json"))
        await self._schedule(row["id"], spec, None)
        return await self.store.get(row["id"])

    async def update(self, id: str, spec: WorkflowSpec) -> Dict[str, Any]:
        """Replace the spec, checked. One that reads another table forgets how far runs read (``last_read``): a write
        number of the table read before, which says nothing of another's rows."""
        current = await self.store.get(id)
        await self.check(spec)
        saved = spec.model_dump(mode="json")
        await self.store.update(id, saved)
        if _source_of(saved) != _source_of(current):
            await self.store.set_last_read(id, 0)
        await self._schedule(id, spec, current.get("schedule_id"))
        return await self.store.get(id)

    async def delete(self, id: str) -> None:
        """Delete the workflow, its schedule and its runs. Its runs under way are cancelled first: the
        schedule first, so it queues none meanwhile, then the run waiting, which would start as the one running
        ends, then that one, which a cancel waits for."""
        current = await self.store.get(id)
        if current.get("schedule_id"):
            try:
                await self.scheduler.delete(current["schedule_id"])
            except NotFound:
                pass
        for status in ("queued", "running"):
            for task in await self.tasks.list(10, kind=KIND, ref=id, status=status):
                await self.tasks.cancel(task["id"])
        await self.tasks.delete_of(KIND, id)
        await self.store.delete(id)

    async def reset(self, id: str) -> Dict[str, Any]:
        """Forget what runs with ``only_new`` read: the next one reads the whole table again."""
        await self.store.get(id)
        await self.store.set_last_read(id, 0)
        return await self.store.get(id)

    async def _schedule(self, id: str, spec: WorkflowSpec, schedule_id: Optional[str]) -> None:
        """Keep the workflow's schedule as its trigger says: made, changed or deleted."""
        if spec.trigger is None:
            if schedule_id:
                try:
                    await self.scheduler.delete(schedule_id)
                except NotFound:
                    pass
                await self.store.set_schedule(id, None)
            return
        body = ScheduleSpec(job=id, name=f"Workflow: {spec.name}", cron=spec.trigger.cron,
                            interval_s=spec.trigger.interval_s, priority="low")
        if schedule_id:
            try:
                await self.scheduler.update(schedule_id, body)
                return
            except NotFound:
                pass
        made = await self.scheduler.create(body, KIND)
        if not await self.store.set_schedule(id, made["id"]):
            # The workflow was deleted meanwhile: its schedule goes too, rather than run for nothing.
            await self.scheduler.delete(made["id"])

    # ---------------------------------------------------------------- runs
    async def start(self, id: str, priority: str = "low", schedule_id: Optional[str] = None) -> Dict[str, Any]:
        """Queue a run of the workflow; the one queued already, if one is. A run takes the workflow as it is saved
        when it starts, and keeps that copy. Checked again first: a model may have left ALLOWED_MODELS since it was
        saved."""
        await self.check(await self._saved(id))
        if priority not in PRIORITIES:
            raise Refused("priority must be high, normal or low")
        task = await self.tasks.submit(KIND, ref=id, params={}, priority=priority, schedule_id=schedule_id)
        return await self.run(task["id"])

    async def _saved(self, id: str) -> WorkflowSpec:
        return _spec_of(await self.store.get(id))

    async def _scheduled(self, name: str, params: Dict[str, Any], **fields: Any) -> Dict[str, Any]:
        """A schedule's run (tasks/schedule.py): its task's row. A workflow gone, or one a check now refuses, is a
        ValueError, which the scheduler logs and skips."""
        try:
            run = await self.start(name, priority=fields.get("priority") or "low", schedule_id=fields.get("schedule_id"))
        except (NotFound, Forbidden) as e:
            raise ValueError(str(e)) from e
        return await self.tasks.get(run["id"])

    async def run(self, run_id: str) -> Dict[str, Any]:
        """A run: its task (status, when, error) with how far it got, and the spec it ran (none before it starts):
        what a row it wrote (its ``_run``) was made by, with each Task node's revisions in its counts."""
        task = await self.tasks.lookup(run_id)
        if task is None or task["kind"] != KIND:
            raise NotFound(f"No run {run_id!r}")
        return {**self._run_of(task), "spec": (task["state"] or {}).get("spec")}

    async def runs(self, id: str, limit: int = 20, cursor: Optional[str] = None
                   ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        """A page of the workflow's runs, newest first, after the page whose ``next`` is ``cursor``; and its next."""
        await self.store.get(id)
        tasks, after = await self.tasks.page(limit, cursor, kind=KIND, ref=id)
        return [self._run_of(t) for t in tasks], after

    async def cancel(self, run_id: str) -> Dict[str, Any]:
        await self.run(run_id)
        await self.tasks.cancel(run_id)
        return await self.run(run_id)

    @staticmethod
    def _run_of(task: Dict[str, Any]) -> Dict[str, Any]:
        progress = task["state"] or {}
        return {"id": task["id"], "workflow_id": task["ref"], "status": task["status"], "priority": task["priority"],
                "created": task["created"], "started": task["started"], "finished": task["finished"],
                "error": task["error"], "schedule_id": task["schedule_id"], "rows": progress.get("rows", 0),
                "pages": progress.get("pages", 0), "edges": progress.get("edges", []),
                "nodes": progress.get("nodes", {})}

    async def _run(self, task: Dict[str, Any]) -> Dict[str, Any]:
        """A run's task: the source read a page at a time, each through the graph, the progress stored after each."""
        run_id, workflow_id = task["id"], task["ref"]
        request_context.set(RequestContext(skip=True, priority=PRIORITIES.get(task["priority"], LOW), source="workflow"))
        state = task["state"]
        if state is None:  # not a run resumed: it starts, with the workflow as saved now (NotFound: deleted)
            saved = await self.store.get(workflow_id)
            graph = specs.check(_spec_of(saved))
            source = graph.settings[graph.source]
            state = {"spec": graph.spec.model_dump(mode="json"), "cursor": saved["last_read"] if source.only_new else 0,
                     "rows": 0, "pages": 0, "edges": [], "nodes": {},
                     "upto": await self.tables.last_write(source.workspace, source.table)}
            await self.tasks.checkpoint(run_id, state)
        else:
            graph = specs.check(WorkflowSpec.model_validate(state["spec"]))
            source = graph.settings[graph.source]
        while True:
            rows, last = await self.tables.written_after(source.workspace, source.table, state["cursor"],
                                                         state["upto"], graph.spec.page_size)
            if not rows:
                break
            try:
                counts = await self.tasks.stoppable(task, self._page(graph, _keyed(rows), write=True, run=run_id))
            except NodeFailed as e:
                raise TaskFailed(f"{e}; the run stopped after {state['rows']} rows") from e
            except Unavailable as e:
                raise TaskFailed(f"The service was too busy for a model ({e}); the run stopped after "
                                 f"{state['rows']} rows") from e
            _add_counts(state, counts)
            state.update(cursor=last, rows=state["rows"] + len(rows), pages=state["pages"] + 1)
            await self.tasks.checkpoint(run_id, state)
            await self.tasks.progress(run_id, None, f"{state['rows']} rows")
        if source.only_new:
            await self.store.set_last_read(workflow_id, state["upto"])
        return {"rows": state["rows"], "pages": state["pages"]}

    # ------------------------------------------------------------ dry run
    async def dry_run(self, spec: WorkflowSpec, limit: int = 20) -> Dict[str, Any]:
        """The first ``limit`` rows of the source through the spec, writing nothing: for each edge the rows along
        it, for each node its rows in and out, the rows it failed (with the first error) and a few rows as they
        left it. A row a node fails is shown, not a failure of the dry run."""
        graph = await self.check(spec)
        source = graph.settings[graph.source]
        # Someone waits for it: its requests go first, and leave no traces.
        request_context.set(RequestContext(skip=True, priority=HIGH, source="workflow"))
        rows, _ = await self.tables.written_after(source.workspace, source.table, 0, None, min(limit, DRY_RUN_ROWS))
        counts = await self._page(graph, _keyed(rows), write=False, samples=SAMPLES)
        return {"rows": len(rows), **counts}

    async def invoke(self, id: str, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        """``rows`` through the saved workflow at once, in place of its table's, writing nothing: each row as it came
        out, in the order given (then any a SQL node made), with the result of every node it went through under the node's id (and ``_error``
        where one failed it), and the counts along each edge and through each node. Only a workflow the admin made
        ``invokable`` (Forbidden otherwise): its SQL nodes read tables a client key may not, the service's own among
        them. Checked as a run is (400, 403, 404); its requests go first, as someone waits, and leave no traces."""
        spec = await self._saved(id)
        if not spec.invokable:
            raise Forbidden(f"Workflow {id!r} is not invokable: an admin turns that on in its spec")
        # Its own context before its check: its models are the admin's, not held to the calling key's.
        request_context.set(RequestContext(skip=True, priority=HIGH, source="workflow"))
        graph = await self.check(spec)
        ended: Dict[Any, Dict[str, Any]] = {i: {} for i in range(len(rows))}
        counts = await self._page(graph, [{**row, "_row": i} for i, row in enumerate(rows)], write=False, ended=ended,
                                  client=True)
        out = [{k: v for k, v in row.items() if k != "_row"} for row in ended.values()]
        return {"rows": out, **counts}

    # --------------------------------------------------------------- a page
    async def _page(self, graph: Graph, rows: List[Dict[str, Any]], *, write: bool, samples: int = 0,
                    ended: Optional[Dict[Any, Dict[str, Any]]] = None, run: Optional[str] = None,
                    client: bool = False) -> Dict[str, Any]:
        """``rows`` (each with its key in ``_row``) through the graph: the rows along each edge, and each node's
        counts (and ``samples`` rows as they left it). Without ``write`` (a dry run, an invoke) nothing is written and
        the rows a node failed are shown instead of raising NodeFailed. ``ended`` gets each row by its key, every
        node's result it got gathered from all the ways it took. ``run`` is the run the page is of, which a Table write
        stamps on the rows it writes. ``client``: the rows go back to a client (``Step.told``)."""
        inbox: Dict[str, List[Dict[str, Any]]] = {id: [] for id in graph.nodes}
        inbox[graph.source] = rows
        nodes: Dict[str, Dict[str, Any]] = {}
        edges: List[Dict[str, Any]] = []
        for id in graph.order:
            items = inbox[id]
            outputs, more = await self._node(graph, id, items, write, run, client)
            failed = outputs.get("error", [])
            stats = {"rows_in": len(items), "rows_out": sum(len(v) for k, v in outputs.items() if k != "error"),
                     "failed": len(failed), "error": failed[0]["_error"]["message"] if failed else None}
            stats.update(more)
            if failed and write and not any(e.output == "error" for e in graph.out_of[id]):
                raise NodeFailed(id, stats["error"])
            if ended is not None:
                for row in (row for rows_out in outputs.values() for row in rows_out):
                    ended.setdefault(row["_row"], {}).update(row)
            if samples:
                stats["samples"] = copy.deepcopy([row for rows_out in outputs.values() for row in rows_out][:samples])
            nodes[id] = stats
            taken: Dict[str, int] = {}
            for edge in graph.out_of[id]:
                along = outputs.get(edge.output, [])
                # The first edge of an output takes its rows; each further one a copy, so the ways do not share rows.
                taken[edge.output] = taken.get(edge.output, 0) + 1
                if taken[edge.output] > 1:
                    along = copy.deepcopy(along)
                inbox[edge.target].extend(along)
                edges.append({"source": edge.source, "output": edge.output, "target": edge.target, "rows": len(along)})
        return {"edges": edges, "nodes": nodes}

    async def _node(self, graph: Graph, id: str, items: List[Dict[str, Any]], write: bool, run: Optional[str],
                    client: bool = False) -> Tuple[Dict[str, List[Dict[str, Any]]], Dict[str, Any]]:
        """What node ``id`` makes of ``items``: its rows by output, and the counts its type keeps (a Table write's rows
        written, or that a dry run would write; a Task's model revisions)."""
        step = Step(id, graph.settings[id], graph, self, write, run=run, client=client)
        outputs = await NODE_TYPES[graph.nodes[id].type].run(step, items)
        return outputs, step.stats


def _spec_of(workflow: Dict[str, Any]) -> WorkflowSpec:
    """A stored workflow's spec, without what the store adds (its id, when it was made...)."""
    return WorkflowSpec.model_validate({k: workflow[k] for k in WorkflowSpec.model_fields if k in workflow})


def _source_of(spec: Dict[str, Any]) -> Tuple[Any, Any]:
    """The workspace and table a spec (as saved) reads."""
    for node in spec.get("nodes", []):
        if NODE_TYPES[node["type"]].source:
            return node["settings"].get("workspace"), node["settings"].get("table")
    return None, None


def _keyed(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """A table's rows, each with its key (its id) in ``_row``, as ``_page`` takes them."""
    return [{**row, "_row": row["id"]} for row in rows]


def _add_counts(state: Dict[str, Any], page: Dict[str, Any]) -> None:
    """Fold a page's counts into the run's."""
    along = {(e["source"], e["output"], e["target"]): e for e in state["edges"]}
    for edge in page["edges"]:
        key = (edge["source"], edge["output"], edge["target"])
        if key in along:
            along[key]["rows"] += edge["rows"]
        else:
            along[key] = dict(edge)
    state["edges"] = list(along.values())
    for id, stats in page["nodes"].items():
        total = state["nodes"].setdefault(id, {"rows_in": 0, "rows_out": 0, "failed": 0, "error": None})
        for key in ("rows_in", "rows_out", "failed", "written"):
            if key in stats:
                total[key] = total.get(key, 0) + stats[key]
        if "revisions" in stats:
            seen = total.setdefault("revisions", [])
            seen.extend(r for r in stats["revisions"] if r not in seen)
        total["error"] = stats["error"] or total["error"]
