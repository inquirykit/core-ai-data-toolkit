"""
What a client key may reach: its grants, set by the admin when the key is made or later. The client guard
(api/deps.py) checks them once a request, from the route it matched alone: no body is read, no route does anything
of its own, and nothing is read from the database (the grants are kept with the key, in memory).

``tables`` maps ``workspace.table``, ``workspace.*`` or ``*.*`` to the actions a key may take there: read, write,
delete. Entries add up: a key may do what any entry naming the table, its workspace or every workspace allows, and a
table no entry names is shut. A request on one table (its rows, a bucket's files) needs its action there. One that
reaches every table of a workspace (a listing, a transaction, edges, the graph, a row's walks, a view, ``_expand``)
needs it on ``workspace.*`` or ``*.*``. Evidence is ``workspace._evidence``. Deleting a row that other rows cascade
from is the schema's: the admin chose that, as PostgreSQL runs a foreign key's actions with the owner's rights.

``models`` narrows ALLOWED_MODELS for the key (api/deps.py's model_allowed), for its own requests: a workflow it
invokes runs the admin's. ``manage_models`` opens the admin's routes that load, unload and pin models to the key, for
its models only.

Each field left out (null) leaves that alone: a key without grants reaches every table and model a client key does,
and manages none.
"""

import re
from typing import Annotated, Dict, List, Literal, Mapping, Optional, Tuple

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints

from core.errors import Forbidden
from db.tables import DEFAULT, MAX_NAME, NAME

Action = Literal["read", "write", "delete"]
# The pseudo-table of a workspace's evidence (names never start with _, so it is no table's).
EVIDENCE = "_evidence"
_NAME = NAME.pattern.strip("^$")
_ENTRY = re.compile(rf"^(?:\*\.\*|{_NAME}\.(?:\*|{_NAME}|{EVIDENCE}))$")


def _entry(value: str) -> str:
    if not _ENTRY.match(value):
        raise ValueError(f"{value!r} is not workspace.table, workspace.* or *.* (workspace._evidence for its evidence)")
    return value


Entry = Annotated[str, StringConstraints(max_length=2 * MAX_NAME + 1), AfterValidator(_entry)]


class Grants(BaseModel):
    """What a client key may reach, as it is made or changed with and as the list shows it."""
    model_config = ConfigDict(extra="forbid", frozen=True)

    tables: Optional[Dict[Entry, List[Action]]] = Field(
        None, max_length=200, description="workspace.table, workspace.* or *.* (workspace._evidence for its evidence) "
                                          "to the actions the key may take there; entries add up, and a table no "
                                          "entry names is shut. Null: every table")
    models: Optional[List[Annotated[str, StringConstraints(min_length=1, max_length=256)]]] = Field(
        None, max_length=500, description="The only models the key may use, within ALLOWED_MODELS. Null: any it allows")
    manage_models: bool = Field(False, description="The key may load, unload and pin models (its own, with models)")


# A route takes its method's action, GET read, DELETE delete, any other write, but these.
NEEDS: Dict[str, List[Action]] = {
    "POST /db/{workspace}/{table}/_bulk-get": ["read"],
    "POST /db/{workspace}/{table}/_search": ["read"],
    "POST /db/{workspace}/_views/{name}": ["read"],
    "POST /db/{workspace}/{table}/_bulk-delete": ["delete"],
    "POST /db/{workspace}/_edges/_delete": ["delete"],
    "POST /db/{workspace}/_transaction": ["write", "delete"],
    "PATCH /db/{workspace}/{table}/{row_id}": ["read", "write"],  # it answers the whole row
}
BY_METHOD: Dict[str, List[Action]] = {"GET": ["read"], "DELETE": ["delete"]}
# The admin's routes manage_models opens.
MANAGE_MODELS = frozenset({"/models/load", "/models/{model_id:path}/{task}", "/models/{model_id:path}/{task}/pin"})
# A row's walks, which reach other tables.
WALKS = ("/_edges", "/_neighbors", "/_traverse")


def target(route: str, params: Mapping[str, str], query: Mapping[str, str]) -> Optional[Tuple[str, str]]:
    """(workspace, table) a request to ``route`` reaches, "*" for every one; None for a route of no table."""
    if route == "/evidence/public-key":  # the signing key's public half, of no workspace
        return None
    if route.startswith("/evidence"):
        return query.get("workspace", DEFAULT), EVIDENCE
    if route != "/db" and not route.startswith("/db/"):
        return None
    table = params.get("table") or params.get("bucket") or "*"
    if route.endswith(WALKS) or "_expand" in query:
        table = "*"
    return params.get("workspace", "*"), table


def check(grants: Grants, method: str, route: str, params: Mapping[str, str], query: Mapping[str, str]) -> None:
    """Forbidden unless ``grants`` let a client request through: ``route`` is the template it matched."""
    tables = grants.tables
    reached = target(route, params, query) if tables is not None else None
    if tables is None or reached is None:
        return
    workspace, table = reached
    for action in NEEDS.get(f"{method} {route}") or BY_METHOD.get(method, ["write"]):
        if not allows(tables, workspace, table, action):
            raise Forbidden(f"This key may not {action} {workspace}.{table}")


def allows(tables: Mapping[str, List[str]], workspace: str, table: str, action: str) -> bool:
    """Whether any entry naming ``workspace.table``, its workspace or every one allows ``action``. A table of "*" is
    the whole workspace, which only ``workspace.*`` or ``*.*`` grants (a workspace of "*", every one: ``*.*``)."""
    return any(action in tables.get(n, ()) for n in (f"{workspace}.{table}", f"{workspace}.*", "*.*"))


def manages_models(grants: Grants, route: str) -> bool:
    """Whether ``grants`` open the admin's ``route``: one that loads, unloads or pins a model, to a key granted it."""
    return grants.manage_models and route in MANAGE_MODELS
