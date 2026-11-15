"""
/db/{workspace}/_views and _dashboards: a workspace's views, saved reads over
its data (SQL, a table's rows, a vector search) with parameters and a
display, and its dashboards, pages of views sharing their parameters (see
views/). They are kept beside the workspace, not as tables in it, and go
when it is dropped.

The admin writes them, and reads their specs (a view's SQL can show what the
admin did not mean to share); a client key runs a view with its parameters,
which is the point: a view is the admin's choice of what a client may read,
and its SQL, like ``_query``'s, is not confined to its workspace. A run is
held to the view's time limit (VIEW_TIME_LIMIT_MS, or less: 503 past it) and
row cap (VIEW_MAX_ROWS, or less: ``truncated``), and its answer is kept for
the view's ``refresh_s`` per set of parameters. These routes come before the
file and table routes, which would take ``_views`` for a table's name (a
table's name starts with a letter, so none is ``_views``).

    GET    /db/{ws}/_views?_limit=&_cursor=       {views, next}: their specs (admin)
    POST   /db/{ws}/_views/_try  {spec, params}   run a spec not saved yet, never kept (admin)
    GET    /db/{ws}/_views/{name}                 its spec (admin)
    PUT    /db/{ws}/_views/{name}  spec           create or replace it, checked (admin)
    DELETE /db/{ws}/_views/{name}                 204; 409 while a dashboard shows it (admin)
    POST   /db/{ws}/_views/{name}  {params}       {rows, columns, truncated, ran_at, cached}: run it
    GET    /db/{ws}/_dashboards?_limit=&_cursor=  {dashboards, next} (admin)
    GET    /db/{ws}/_dashboards/{name}            its spec (admin)
    PUT    /db/{ws}/_dashboards/{name}  spec      create or replace it; 400 unless its views are there (admin)
    DELETE /db/{ws}/_dashboards/{name}            204 (admin)
"""

from typing import Any, Dict, Optional

from fastapi import APIRouter, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from core.errors import Refused
from db.tables import decode_cursor
from views import DashboardSpec, ViewSpec
from views.store import MAX_PAGE
from . import audit, deps
from . import responses

router = APIRouter(prefix="/db", tags=["views"], dependencies=deps.CLIENT)
# This module's routes that only the admin's key opens.
admin_router = APIRouter(prefix="/db", tags=["views"], dependencies=deps.ADMIN)

LIMIT = Query(100, alias="_limit", ge=1, le=MAX_PAGE, description="Per page")
CURSOR = Query(None, alias="_cursor", description="The previous page's 'next'")


class RunView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    params: Dict[str, Any] = Field(default_factory=dict, description="The view's parameters by name; one it does not "
                                                                     "declare is refused, defaults fill the rest")


class TryView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    spec: ViewSpec
    params: Dict[str, Any] = Field(default_factory=dict, description="Its parameters, as a run takes them")


def _named(path: str, body: str) -> None:
    if body != path:
        raise Refused(f"The body names {body!r}, the path {path!r}")


# ------------------------------------------------------------------ views
@admin_router.get("/{workspace}/_views", **responses.answering(responses.ViewList))
async def list_views(views: deps.Views, workspace: str, limit: int = LIMIT, cursor: Optional[str] = CURSOR):
    """A page of the workspace's views, by name: each its spec, with when it was made and changed. 404 for a
    workspace that is not there."""
    return responses.page("views", *await views.list_views(workspace, limit, decode_cursor(cursor) if cursor else 0))


@admin_router.post("/{workspace}/_views/_try", **responses.answering(responses.ViewResult))
@audit.reads
async def try_view(views: deps.Views, workspace: str, body: TryView):
    """Run a view's spec that is not saved, with these params, as a run of it saved would answer (checked as a save
    is): what the editor previews. Never kept, so ``cached`` is false."""
    return await views.try_view(workspace, body.spec, body.params)


@admin_router.get("/{workspace}/_views/{name}", **responses.answering(responses.ViewInfo))
async def get_view(views: deps.Views, workspace: str, name: str):
    """The view's spec, with when it was made and changed. Needs the admin key: its SQL may show more than the
    admin means a client to read."""
    return await views.get_view(workspace, name)


@admin_router.put("/{workspace}/_views/{name}", **responses.answering(responses.ViewInfo))
async def put_view(views: deps.Views, workspace: str, name: str, spec: ViewSpec):
    """Create or replace a view. Its params are a JSON Schema of an object; a ``$param`` in a search or rows source
    names one of them (422 otherwise). Its SQL is one SELECT that DuckDB's parser takes, so it changes nothing,
    with no $name its params do not declare (400 otherwise, saying why). ``max_rows`` and ``time_limit_ms`` may lower
    VIEW_MAX_ROWS and VIEW_TIME_LIMIT_MS, not raise them (400). 404 for a workspace that is not there. What was kept
    of its runs is never answered again."""
    _named(name, spec.name)
    return await views.put_view(workspace, spec)


@admin_router.delete("/{workspace}/_views/{name}", status_code=204, response_class=Response)
async def delete_view(views: deps.Views, workspace: str, name: str):
    """Delete the view; 409, naming them, while dashboards show it. One that is not there is no error."""
    await views.delete_view(workspace, name)


@router.post("/{workspace}/_views/{name}", **responses.answering(responses.ViewResult))
@audit.reads
async def run_view(views: deps.Views, workspace: str, name: str, body: RunView):
    """Run the view with these params (the client key may): its rows (at most its row cap; ``truncated`` when the cap
    cut them), their ``columns`` in the order first seen, when the database was asked (``ran_at``) and whether the
    answer was kept from a run with the same params within the view's ``refresh_s`` (``cached``). 400 for a param
    the view does not declare, a required one missing or one of the wrong type, naming it, and for SQL the database
    refuses, with its message; 503 when the view takes longer than its time limit."""
    return await views.run(workspace, name, body.params)


# ------------------------------------------------------------- dashboards
@admin_router.get("/{workspace}/_dashboards", **responses.answering(responses.DashboardList))
async def list_dashboards(views: deps.Views, workspace: str, limit: int = LIMIT, cursor: Optional[str] = CURSOR):
    """A page of the workspace's dashboards, by name: each its spec, with when it was made and changed."""
    return responses.page("dashboards", *await views.list_dashboards(workspace, limit, decode_cursor(cursor) if cursor else 0))


@admin_router.get("/{workspace}/_dashboards/{name}", **responses.answering(responses.DashboardInfo))
async def get_dashboard(views: deps.Views, workspace: str, name: str):
    """The dashboard's spec, with when it was made and changed."""
    return await views.get_dashboard(workspace, name)


@admin_router.put("/{workspace}/_dashboards/{name}", **responses.answering(responses.DashboardInfo))
async def put_dashboard(views: deps.Views, workspace: str, name: str, spec: DashboardSpec):
    """Create or replace a dashboard: its views in order, each ``width`` columns of 12, sharing ``params`` (a JSON
    Schema; each view takes the values of its own params' names), refreshed every ``refresh_s`` seconds (0 for
    none, else at least 5: 422 below). 400 unless every item names a view of the workspace."""
    _named(name, spec.name)
    return await views.put_dashboard(workspace, spec)


@admin_router.delete("/{workspace}/_dashboards/{name}", status_code=204, response_class=Response)
async def delete_dashboard(views: deps.Views, workspace: str, name: str):
    """Delete the dashboard; its views stay. One that is not there is no error."""
    await views.delete_dashboard(workspace, name)
