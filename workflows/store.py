"""
The workflows, in the service's own table.

``tk_workflow`` (a SpecStore: db/specs.py) is a row per workflow, by id: its spec as JSON, the schedule its
trigger made (tasks/schedule.py), and ``last_read``, the write number of the
source table a run with ``only_new`` read up to, so the next one reads only
what came after. A run is a task (tasks/store.py), which keeps how far it got
as its state: the spec it runs (the workflow as saved when it started), the
write number it has read up to, the one it stops at, and its counts.
"""

import uuid
from typing import Any, Dict, List, Optional, Tuple

from core.errors import NotFound
from db import on_pool
from db.specs import SpecStore

WORKFLOW = "tk_workflow"
# A page of the list of workflows, at most.
MAX_PAGE = 200


class WorkflowStore(SpecStore):
    """The workflows, by id (db/specs.py: the service's own, scope ""), with their schedule and ``last_read``."""

    TABLE = WORKFLOW
    EXTRA = {"schedule_id": "VARCHAR", "last_read": "BIGINT"}

    def _spec(self, row: Dict[str, Any]) -> Dict[str, Any]:
        return {**super()._spec(row), "id": row["name"], "last_read": row.get("last_read") or 0}

    # ------------------------------------------------------------ workflows
    async def get(self, id: str) -> Dict[str, Any]:
        found = await self._run(self._find, "", id)
        if found is None:
            raise NotFound(f"No workflow {id!r}")
        return found

    @on_pool
    def list(self, limit: int = 100, offset: int = 0) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        """A page of the workflows by name, and the next page's cursor (None on the last)."""
        return self._page("", min(limit, MAX_PAGE), offset)

    @on_pool
    def create(self, spec: Dict[str, Any]) -> Dict[str, Any]:
        return self._save("", uuid.uuid4().hex[:12], spec)

    @on_pool
    def update(self, id: str, spec: Dict[str, Any]) -> Dict[str, Any]:
        saved = self._save("", id, spec, create=False)
        if saved is None:
            raise NotFound(f"No workflow {id!r}")
        return saved

    @on_pool
    def set_schedule(self, id: str, schedule_id: Optional[str]) -> bool:
        """Note the workflow's schedule; whether the workflow is there."""
        return self._set("", id, schedule_id=schedule_id)

    @on_pool
    def set_last_read(self, id: str, last_read: int) -> None:
        self._set("", id, last_read=last_read)

    @on_pool
    def delete(self, id: str) -> None:
        self._remove("", id)
