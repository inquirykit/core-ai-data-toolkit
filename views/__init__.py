"""
Views and dashboards: saved reads over a workspace's data (SQL, a table's
rows, a vector search) with parameters and a display, and pages of them that
share their parameters. spec.py says what they are; store.py keeps them
beside the workspaces, checks them, and runs a view within its time limit
and row cap, keeping its answer for its refresh interval. The admin writes
them and a client key runs them (api/view_routes.py); the Data section of
/admin draws them.
"""

from .spec import DashboardSpec, ViewSpec
from .store import ViewTimedOut, Views

__all__ = ["DashboardSpec", "ViewSpec", "ViewTimedOut", "Views"]
