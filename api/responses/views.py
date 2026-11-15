"""Views and dashboards of a workspace (api/view_routes.py)."""

from typing import List, Optional

from pydantic import ConfigDict

from views import DashboardSpec, ViewSpec
from .common import OPEN, Json, Out


class ViewInfo(ViewSpec):
    """A view: its spec, as PUT takes it, with when it was made and last changed (Unix time)."""
    # Every field of the spec is in the answer, defaults included: required in the schema.
    model_config = ConfigDict(**OPEN, json_schema_serialization_defaults_required=True)

    created: Optional[float]
    updated: Optional[float]


class ViewList(Out):
    """A page of a workspace's views, by name; ``next`` is the next page's cursor, absent on the last."""
    views: List[ViewInfo]
    next: Optional[str] = None


class ViewResult(Out):
    """A view's answer: at most its row cap of rows (for a graph, rows and edges as _query answers them)."""
    rows: List[Json]
    # The rows' fields, in the order first seen
    columns: List[str]
    # Whether the row cap cut rows (only one more was read, never the rest)
    truncated: bool
    # When the database was asked (Unix time)
    ran_at: float
    # Whether this answer is one kept from a run with the same parameters, younger than the view's refresh_s, or
    # one asked by a run that was under way
    cached: bool


class DashboardInfo(DashboardSpec):
    """A dashboard: its spec, as PUT takes it, with when it was made and last changed (Unix time)."""
    model_config = ConfigDict(**OPEN, json_schema_serialization_defaults_required=True)

    created: Optional[float]
    updated: Optional[float]


class DashboardList(Out):
    """A page of a workspace's dashboards, by name; ``next`` is the next page's cursor, absent on the last."""
    dashboards: List[DashboardInfo]
    next: Optional[str] = None
