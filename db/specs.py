"""
SpecStore: what an admin saves as a JSON spec (a view, a dashboard, a workflow, a remote), one row each.

A store of specs declares its ``TABLE`` (a ``tk_`` name) and, if it keeps
more of a spec than the spec itself, the ``EXTRA`` columns (a workflow's
schedule and how far its runs read). A row is a spec's ``scope`` (a
workspace, or "" for the service's own), its ``name`` (the key: a view's
name, a workflow's id), the ``spec`` as JSON, when it was made and changed,
and the extras. Replacing a spec keeps when it was made and its extras.

Every method blocks: a store's own async methods run them on the database's
pool (``on_pool``), under the locks they need (a workspace that must be
there, a name a dashboard uses).
"""

import json
import time
from typing import Any, Dict, List, Optional, Tuple

import duckdb

from .engine import Clash, DocumentStore
from .schema import encode_cursor


class SpecStore(DocumentStore):
    """Specs by scope and name, in ``TABLE``; see the module docstring."""

    TABLE = ""
    EXTRA: Dict[str, str] = {}

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if cls.TABLE:
            cls.SCHEMA = {cls.TABLE: {
                "props": {"scope": "VARCHAR", "name": "VARCHAR", "spec": "JSON", "created": "DOUBLE",
                          "updated": "DOUBLE", **cls.EXTRA},
                "indexes": [("scope, name", True)],
            }}

    def _spec(self, row: Dict[str, Any]) -> Dict[str, Any]:
        """A row as the store hands it out: the spec, with when it was made and changed and its extras."""
        return {**row["spec"], "created": row["created"], "updated": row["updated"],
                **{column: row.get(column) for column in self.EXTRA}}

    def _find(self, scope: str, name: str) -> Optional[Dict[str, Any]]:
        row = self._one(f"SELECT * FROM {self.TABLE} WHERE scope = $s AND name = $n", {"s": scope, "n": name})
        return self._spec(row) if row else None

    def _all(self, scope: str) -> List[Dict[str, Any]]:
        """Every spec of the scope, by the name it gives itself."""
        rows = self._select(f"SELECT * FROM {self.TABLE} WHERE scope = $s ORDER BY spec->>'name', name", {"s": scope})
        return [self._spec(r) for r in rows]

    def _page(self, scope: str, limit: int, offset: int) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        """A page of the scope's specs by the name each gives itself, and the next page's cursor (None on the last)."""
        rows = self._select(f"SELECT * FROM {self.TABLE} WHERE scope = $s ORDER BY spec->>'name', name "
                            "LIMIT $l OFFSET $o", {"s": scope, "l": limit + 1, "o": offset})
        found = [self._spec(r) for r in rows[:limit]]
        return found, (encode_cursor(offset + limit) if len(rows) > limit else None)

    def _save(self, scope: str, name: str, spec: Dict[str, Any], create: bool = True) -> Optional[Dict[str, Any]]:
        """Create or replace the spec, keeping when it was made and its extras; the spec as stored. With ``create``
        False, only one that is there is replaced: None when it is not."""
        now = time.time()
        db = self._db()
        params = {"s": scope, "n": name, "spec": json.dumps(spec), "now": now}

        def save() -> bool:
            if db.execute(f"UPDATE {self.TABLE} SET spec = $spec::JSON, updated = $now WHERE scope = $s AND "
                          "name = $n", params):
                return True
            if create:
                try:
                    db.execute(f"INSERT INTO {self.TABLE} (scope, name, spec, created, updated) "
                               "VALUES ($s, $n, $spec::JSON, $now, $now)", params)
                except duckdb.ConstraintException as e:
                    # Another save made it meanwhile, which this one could not see: run again, and replace it.
                    raise Clash(f"{name} was saved at the same time") from e
            return create

        return self._find(scope, name) if self._commit(db, save) else None

    def _set(self, scope: str, name: str, **extras: Any) -> bool:
        """Set extras of a spec; whether it is there."""
        sets = ", ".join(f"{column} = ${column}" for column in extras)
        return bool(self._command(f"UPDATE {self.TABLE} SET {sets} WHERE scope = $s AND name = $n",
                                  {**extras, "s": scope, "n": name}))

    def _remove(self, scope: str, name: str) -> bool:
        """Delete the spec; whether it was there."""
        return bool(self._command(f"DELETE FROM {self.TABLE} WHERE scope = $s AND name = $n", {"s": scope, "n": name}))

    def _forget(self, scope: str) -> int:
        """Delete every spec of the scope (a workspace dropped); how many."""
        return self._command(f"DELETE FROM {self.TABLE} WHERE scope = $s", {"s": scope})
