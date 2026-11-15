"""
/workflows: workflows, rows of a table taken through a graph of nodes, and
their runs (see workflows/). The admin's, but for an invoke, which is the
client's: a workflow runs models and enrichers and writes tables as the
admin, and an invoke writes nothing.

    GET    /workflows?limit=&cursor=        {workflows, next}: by name
    POST   /workflows  spec                  201: made, checked
    GET    /workflows/nodes                  {nodes}: the node types, each with its settings' JSON Schema
    POST   /workflows/dry-run  {spec, limit} the first rows through a spec, nothing written
    GET    /workflows/runs/{run}             a run: its status and counts
    POST   /workflows/runs/{run}/cancel      cancel it
    GET    /workflows/{id}                   its spec
    PUT    /workflows/{id}  spec             replace it, checked; its trigger's schedule follows
    DELETE /workflows/{id}                   204, its schedule and runs too
    POST   /workflows/{id}/run  {priority}   202: a run queued (the one waiting, if one is)
    GET    /workflows/{id}/runs?limit=&before=   {runs}: newest first
    POST   /workflows/{id}/reset             runs with only_new read the whole table again
    POST   /workflows/{id}/invoke  {rows}    the rows through it at once, nothing written: each with its results
"""

from typing import Any, Dict, List

from fastapi import APIRouter, Path, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from db.tables import decode_cursor
from core.priorities import Priority
from workflows import WorkflowSpec
from workflows.runner import DRY_RUN_ROWS, INVOKE_ROWS
from workflows.nodes import node_types
from workflows.store import MAX_PAGE
from . import audit, deps
from . import responses

router = APIRouter(prefix="/workflows", tags=["workflows"], dependencies=deps.CLIENT)
# This module's routes that only the admin's key opens.
admin_router = APIRouter(prefix="/workflows", tags=["workflows"], dependencies=deps.ADMIN)

WorkflowId = Path(..., pattern=r"^[0-9a-f]{8,32}$")
RunId = Path(..., pattern=r"^[0-9a-f]{8,32}$")


class DryRun(BaseModel):
    model_config = ConfigDict(extra="forbid")

    spec: WorkflowSpec
    limit: int = Field(20, ge=1, le=DRY_RUN_ROWS, description="The source's first rows to take through")


class Invoke(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rows: List[Dict[str, Any]] = Field(..., min_length=1, max_length=INVOKE_ROWS,
                                       description="The rows, each a JSON object as a row of the workflow's table is")


class StartRun(BaseModel):
    model_config = ConfigDict(extra="forbid")

    priority: Priority = Field("low", description="Among the other work for the models: a "
                                                                          "request someone waits on is high")


@admin_router.get("", **responses.answering(responses.WorkflowList))
async def list_workflows(workflows: deps.Workflows, limit: int = Query(100, ge=1, le=MAX_PAGE),
                         cursor: deps.Cursor = None):
    """A page of the workflows, by name: each its spec, with its id and when it was made and changed."""
    return responses.page("workflows", *await workflows.list(limit, decode_cursor(cursor) if cursor else 0))


@admin_router.post("", **responses.answering(responses.WorkflowInfo), status_code=201)
async def create_workflow(workflows: deps.Workflows, spec: WorkflowSpec):
    """Make a workflow. 400, saying what, for a spec whose nodes, edges or JMESPaths are wrong, a task that is not
    known or inputs it does not take; 403 for a model outside ALLOWED_MODELS; 404 for a workspace that is not there.
    A trigger makes its schedule."""
    return await workflows.create(spec)


@admin_router.get("/nodes", **responses.answering(responses.WorkflowNodeTypes))
async def list_node_types():
    """The node types a workflow is made of, each with the JSON Schema of its settings and its outputs."""
    return {"nodes": node_types()}


@admin_router.post("/dry-run", **responses.answering(responses.WorkflowDryRun))
@audit.reads
async def dry_run(workflows: deps.Workflows, body: DryRun):
    """Take the source's first ``limit`` rows through a spec, saved or not, writing nothing: the rows along each edge,
    each node's rows in, out and failed (with its first error) and a few rows as they left it. Checked as a save is
    (400, 403, 404)."""
    return await workflows.dry_run(body.spec, body.limit)


@admin_router.get("/runs/{run_id}", **responses.answering(responses.WorkflowRun))
async def get_run(workflows: deps.Workflows, run_id: str = RunId):
    """A run: its status, when it was queued, started and finished, its error, and its rows along each edge and
    through each node so far."""
    return await workflows.run(run_id)


@admin_router.post("/runs/{run_id}/cancel", **responses.answering(responses.WorkflowRun))
async def cancel_run(workflows: deps.Workflows, run_id: str = RunId):
    """Cancel a queued run, or stop a running one after the rows it is on; a finished one is left as it is."""
    return await workflows.cancel(run_id)


@admin_router.get("/{id}", **responses.answering(responses.WorkflowInfo))
async def get_workflow(workflows: deps.Workflows, id: str = WorkflowId):
    """The workflow's spec, with its id and when it was made and changed."""
    return await workflows.get(id)


@admin_router.put("/{id}", **responses.answering(responses.WorkflowInfo))
async def update_workflow(workflows: deps.Workflows, spec: WorkflowSpec, id: str = WorkflowId):
    """Replace the workflow's spec, checked as when it was made; its trigger's schedule is made, changed or deleted to
    match. A run under way keeps the spec it started with."""
    return await workflows.update(id, spec)


@admin_router.delete("/{id}", status_code=204, response_class=Response)
async def delete_workflow(workflows: deps.Workflows, id: str = WorkflowId):
    """Delete the workflow, its schedule and its runs; runs under way are cancelled first."""
    await workflows.delete(id)


@admin_router.post("/{id}/run", **responses.answering(responses.WorkflowRun), status_code=202)
async def start_run(workflows: deps.Workflows, body: StartRun, id: str = WorkflowId):
    """Queue a run of the workflow, which runs it as it is saved when the run starts. Runs of a workflow never
    overlap: one may wait while one runs, and while one waits, this answers with it. Checked again first (a model may have left ALLOWED_MODELS since:
    403)."""
    return await workflows.start(id, body.priority)


@admin_router.get("/{id}/runs", **responses.answering(responses.WorkflowRuns))
async def list_runs(workflows: deps.Workflows, id: str = WorkflowId, limit: int = Query(20, ge=1, le=100),
                    cursor: deps.Cursor = None):
    """The workflow's runs, newest first, each with its counts, a page at a time."""
    return responses.page("runs", *await workflows.runs(id, limit, cursor))


@admin_router.post("/{id}/reset", **responses.answering(responses.WorkflowInfo))
async def reset_workflow(workflows: deps.Workflows, id: str = WorkflowId):
    """Forget how far runs that read only new rows have read: the next reads the whole table."""
    return await workflows.reset(id)


@router.post("/{id}/invoke", **responses.answering(responses.WorkflowInvoke))
@audit.reads
async def invoke_workflow(workflows: deps.Workflows, body: Invoke, id: str = WorkflowId):
    """Take ``rows`` through the saved workflow at once, in place of its table's, writing nothing: each row as it came
    out, in the order given, with every node's result it got under the node's id and ``_error`` where a node failed
    it, and the rows along each edge and through each node. A client's: composite inference in one call, of a
    workflow the admin made ``invokable`` (403 otherwise). Checked as a run is (400, 403 for a model outside
    ALLOWED_MODELS, 404)."""
    return await workflows.invoke(id, body.rows)
