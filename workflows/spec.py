"""
What a workflow is: nodes joined by edges, as JSON, checked before it is saved or run.

Rows of a table flow from the one source node (``table.read``) along the
edges, each node's result under its id, read by a JMESPath on the row; a
node's outputs are named, ``error`` for the rows it could not do. A node with
several edges in gets the rows of all of them. The node types, what each one
takes, gives and does, are in nodes.py. A spec's ``ui`` (each node's place on
the canvas) is the editor's: nothing here reads it.

``check`` is what can be told without running: ids, settings, edges and their
outputs, one source, no cycle, every node reached from the source, every
JMESPath compiled, no run reading back its own writes, and a trigger its
schedule takes. What needs the
service (a task's model, the workspaces) each node type checks itself, when
the runner asks (runner.py).
"""

from typing import Any, Dict, List, Literal, Optional

import jmespath
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from core.errors import Refused
from tasks.schedule import check_cadence

from .nodes import NODE_TYPES, Settings, reads

NODE_ID = r"^[A-Za-z][A-Za-z0-9_]{0,39}$"
MAX_NODES = 50


class Node(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., pattern=NODE_ID, description="Unique in the workflow; the key its result goes under")
    type: Literal[tuple(NODE_TYPES)]  # type: ignore[valid-type]
    settings: Dict[str, Any] = Field(default_factory=dict, description="As the node type's settings schema says")
    ui: Optional[Dict[str, Any]] = Field(None, description="The editor's: where the node is drawn")


class Edge(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str = Field(..., pattern=NODE_ID)
    output: str = Field("out", min_length=1, max_length=40, description="Which output of the source node")
    target: str = Field(..., pattern=NODE_ID)


class Trigger(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cron: Optional[str] = Field(None, max_length=120, description="Five fields, server local time")
    interval_s: Optional[float] = Field(None, allow_inf_nan=False, description="Seconds between runs, at least 60")


class WorkflowSpec(BaseModel):
    """A workflow, as it is saved: what the editor sends and gets back."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=200)
    description: str = Field("", max_length=2000)
    nodes: List[Node] = Field(..., min_length=1, max_length=MAX_NODES)
    edges: List[Edge] = Field(default_factory=list)
    page_size: int = Field(100, ge=1, le=1000, description="Rows read and taken through the graph at a time")
    trigger: Optional[Trigger] = Field(None, description="Runs it on a cron expression or every interval_s seconds")
    invokable: bool = Field(False, description="A client key may take rows through it at once (POST "
                                               "/workflows/{id}/invoke), reading what its nodes read as the admin")


class Graph:
    """A checked spec: each node's settings parsed, its edges in and out, and the order the nodes run in."""

    def __init__(self, spec: WorkflowSpec):
        self.spec = spec
        self.nodes: Dict[str, Node] = {}
        for node in spec.nodes:
            if node.id in self.nodes:
                raise Refused(f"Two nodes are called {node.id!r}")
            self.nodes[node.id] = node
        self.settings: Dict[str, Settings] = {}
        for node in spec.nodes:
            try:
                self.settings[node.id] = NODE_TYPES[node.type].settings.model_validate(node.settings)
            except ValidationError as e:
                raise Refused(f"Node {node.id!r}: {_first_error(e)}") from e
        self.into: Dict[str, List[Edge]] = {id: [] for id in self.nodes}
        self.out_of: Dict[str, List[Edge]] = {id: [] for id in self.nodes}
        for edge in spec.edges:
            for end in (edge.source, edge.target):
                if end not in self.nodes:
                    raise Refused(f"An edge names {end!r}, which is no node")
            outputs = self.outputs(edge.source)
            if edge.output not in outputs:
                raise Refused(f"Node {edge.source!r} has no output {edge.output!r}"
                              + (f": its outputs are {', '.join(outputs)}" if outputs else ": it gives none"))
            if NODE_TYPES[self.nodes[edge.target].type].source:
                raise Refused(f"Node {edge.target!r} reads a table: no edge goes into it")
            if any(e.source == edge.source and e.output == edge.output and e.target == edge.target
                   for e in self.out_of[edge.source]):
                raise Refused(f"The edge from {edge.source!r} ({edge.output}) to {edge.target!r} is there twice")
            self.into[edge.target].append(edge)
            self.out_of[edge.source].append(edge)
        sources = [id for id, node in self.nodes.items() if NODE_TYPES[node.type].source]
        if len(sources) != 1:
            raise Refused(f"A workflow reads one table: it has {len(sources)} Table read nodes")
        self.source = sources[0]
        read = self.settings[self.source]
        if read.only_new:
            # Each write gives a row a new write number, so the next run would read back what this one wrote.
            for id, node in self.nodes.items():
                into = self.settings[id]
                if node.type == "table.write" and (into.workspace, into.table) == (read.workspace, read.table):
                    raise Refused(f"Node {id!r} writes to {read.table}, which is read for its new rows only: each "
                                  "run would read back the rows the last one wrote and do them again. Write to "
                                  "another table, or read every row and let an IF pass only the rows not done yet")
        self.order = self._ordered()
        unreached = [id for id in self.nodes if id != self.source and not self.into[id]]
        if unreached:
            raise Refused(f"Nothing leads to {', '.join(repr(id) for id in unreached)}")
        for node in spec.nodes:
            for expression in NODE_TYPES[node.type].expressions(self.settings[node.id]):
                try:
                    jmespath.compile(expression)
                except jmespath.exceptions.JMESPathError as e:
                    raise Refused(f"Node {node.id!r}: {expression!r} is not a JMESPath ({e})") from e
        if spec.trigger is not None:
            # As its schedule would refuse it, but before the workflow is saved.
            try:
                check_cadence(spec.trigger.cron, spec.trigger.interval_s)
            except ValueError as e:
                raise Refused(f"The trigger: {e}") from None

    def check_wiring(self) -> None:
        """Refused for an expression that reads a node's result where it is not there: the node is not on a way to
        the node reading it, or its result has no such field (``B.lable`` of a classifier). Asked by the runner once
        the node types checked themselves, so a task a model file defines has its contract by then."""
        for id, node in self.nodes.items():
            before = self.before(id)
            for expression in NODE_TYPES[node.type].expressions(self.settings[id]):
                for name, under in reads(expression):
                    if name not in self.nodes or not NODE_TYPES[self.nodes[name].type].result:
                        continue  # a field of the source's row
                    if name not in before:
                        raise Refused(f"Node {id!r}: {expression!r} reads the result of {name!r}, which is on no way "
                                      f"to {id!r}")
                    gives = NODE_TYPES[self.nodes[name].type].gives(self.settings[name])
                    if under is not None and gives is not None and under not in gives:
                        raise Refused(f"Node {id!r}: {expression!r} reads {name}.{under}, but {name!r} gives "
                                      f"{', '.join(gives)}")

    def before(self, id: str) -> set:
        """The nodes on a way to ``id``."""
        seen: set = set()
        waiting = [e.source for e in self.into[id]]
        while waiting:
            source = waiting.pop()
            if source not in seen:
                seen.add(source)
                waiting.extend(e.source for e in self.into[source])
        return seen

    def outputs(self, id: str) -> List[str]:
        node = self.nodes[id]
        return NODE_TYPES[node.type].outputs(self.settings[node.id])

    def _ordered(self) -> List[str]:
        """The nodes, each after every node with an edge into it (Kahn's); Refused for a cycle."""
        waiting = {id: len({e.source for e in edges}) for id, edges in self.into.items()}
        ready = [id for id, n in waiting.items() if n == 0]
        order: List[str] = []
        while ready:
            id = ready.pop(0)
            order.append(id)
            for target in dict.fromkeys(e.target for e in self.out_of[id]):
                waiting[target] -= 1
                if waiting[target] == 0:
                    ready.append(target)
        if len(order) != len(self.nodes):
            looped = sorted(id for id in self.nodes if id not in order)
            raise Refused(f"The edges go round in a circle through {', '.join(repr(id) for id in looped)}")
        return order


def _first_error(e: ValidationError) -> str:
    error = e.errors()[0]
    where = ".".join(str(part) for part in error["loc"])
    return f"{where}: {error['msg']}" if where else error["msg"]


def check(spec: WorkflowSpec) -> Graph:
    """The spec as a graph to run; Refused, saying what, for what is wrong with it."""
    return Graph(spec)


__all__ = ["Edge", "Graph", "Node", "Trigger", "WorkflowSpec", "check"]
