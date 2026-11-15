"""Workflows and their runs (api/workflow_routes.py)."""

from typing import Dict, List, Optional

from pydantic import ConfigDict

from workflows import WorkflowSpec
from .common import OPEN, Json, Out
from .tasks import TaskStatus


class WorkflowInfo(WorkflowSpec):
    """A workflow: its spec, as PUT takes it, with its id, the schedule its trigger made, how far runs that read only
    new rows have read, and when it was made and last changed (Unix time)."""
    model_config = ConfigDict(**OPEN, json_schema_serialization_defaults_required=True)

    id: str
    schedule_id: Optional[str]
    # The source table's write number runs with only_new have read up to (0: none yet)
    last_read: int
    created: float
    updated: float


class WorkflowList(Out):
    """A page of the workflows, by name; ``next`` is the next page's cursor, absent on the last."""
    workflows: List[WorkflowInfo]
    next: Optional[str] = None


class WorkflowNodeType(Out):
    """A node type of the editor's palette."""
    type: str
    label: str
    description: str
    # Reads the table: no edge goes into it
    source: bool
    # Puts its result on the row under its id, for the nodes after it to read (a Task's: its task's output)
    result: bool
    # Its outputs; null when its settings decide them (a Switch: a case's number each, and default)
    outputs: Optional[List[str]]
    # The JSON Schema of its settings
    settings: Json


class WorkflowNodeTypes(Out):
    nodes: List[WorkflowNodeType]


class WorkflowEdgeCount(Out):
    """The rows that went along an edge."""
    source: str
    output: str
    target: str
    rows: int


class WorkflowNodeCount(Out):
    """A node's rows: in, out (along its outputs but error), failed (to its error output) with the first error."""
    rows_in: int
    rows_out: int
    failed: int
    error: Optional[str]
    # A Table write's rows written (or, in a dry run, that it would write)
    written: Optional[int] = None
    # A dry run's: a few rows as they left the node
    samples: Optional[List[Json]] = None
    # A Task's: the Hub commits of the weights that answered (usually one; none when not from the Hub cache)
    revisions: Optional[List[str]] = None


class WorkflowRun(Out):
    """A run: its task's status and times, and how far it got."""
    id: str
    workflow_id: str
    status: TaskStatus
    priority: str
    created: float
    started: Optional[float]
    finished: Optional[float]
    error: Optional[str]
    # The schedule that started it, if one did
    schedule_id: Optional[str]
    # Rows read and pages done so far
    rows: int
    pages: int
    edges: List[WorkflowEdgeCount]
    nodes: Dict[str, WorkflowNodeCount]
    # The spec it runs, as the workflow was saved when it started; none while it waits (one run's, not in a list)
    spec: Optional[WorkflowSpec] = None


class WorkflowRuns(Out):
    """A page of a workflow's runs, newest first; ``next`` is the next page's cursor."""
    runs: List[WorkflowRun]
    next: Optional[str] = None


class WorkflowInvoke(Out):
    """A caller's rows through a workflow, nothing written: each as it came out, with every node's result it got under
    the node's id (and ``_error`` where one failed it), in the order given; and the counts."""
    rows: List[Json]
    edges: List[WorkflowEdgeCount]
    nodes: Dict[str, WorkflowNodeCount]


class WorkflowDryRun(Out):
    """The first rows of the source through a spec, nothing written."""
    rows: int
    edges: List[WorkflowEdgeCount]
    nodes: Dict[str, WorkflowNodeCount]
