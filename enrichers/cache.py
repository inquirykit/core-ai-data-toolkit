"""
Cache of enricher results, one row per (enricher, type, value): a /db table
in the ``enrichers`` workspace (db/tables.py), which the admin's Data
section shows read-only. Its methods are async and run on the database's
thread pool (db/engine.py).

A result is served from here until its enricher's TTL has passed, so a value
looked up twice costs one request against the source's quota. Rows hold the
source's reply as it came, which stays inside the service: the API serves
it to the toolkit's own clients only. Expired rows go when ``prune`` runs
(api/app.py schedules it hourly).
"""

import hashlib
import json
import time
from typing import Any, Dict, Optional

from db import Database, DocumentStore, on_pool
from db.tables import TableSchema, TableStore, schema_of, table_type

WORKSPACE = "enrichers"
TABLES: Dict[str, TableSchema] = {
    # raw is the source's reply as JSON text, kept as it came (an integer of 2^63 or more too).
    "results": schema_of("enricher:string+", "type:string", "value:string", "status:string", "http_status:integer",
                         "outputs:object", "raw:string", "observed_at:number", "expires_at:number+"),
}
RESULT = table_type(WORKSPACE, "results")


def _id(enricher: str, type_: str, value: str) -> str:
    """The row's id: a hash, since a value (a URL) may hold what an id may not."""
    return hashlib.sha256(f"{enricher}\n{type_}\n{value}".encode()).hexdigest()


def _row(enricher: str, type_: str, value: str, result: Dict[str, Any], ttl_s: int) -> Dict[str, Any]:
    now = time.time()
    return {
        "id": _id(enricher, type_, value), "enricher": enricher, "type": type_, "value": value,
        "status": result["status"], "http_status": result.get("http_status"),
        "outputs": result.get("outputs") or {},
        "raw": None if result.get("raw") is None else json.dumps(result["raw"], ensure_ascii=False),
        "observed_at": now, "expires_at": now + ttl_s,
    }


def _result(row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The cached result a row holds, or None when there is none or it has expired."""
    if row is None or row["expires_at"] <= time.time():
        return None
    found = {key: row.get(key) for key in ("status", "http_status", "outputs", "raw", "observed_at", "expires_at")}
    found["raw"] = None if found["raw"] is None else json.loads(found["raw"])
    return found


class ResultCache(DocumentStore):
    WORKSPACE = WORKSPACE
    OWNER = "the enrichers' result cache: it fills itself"
    TABLES = TABLES

    def __init__(self, database: Database, tables: TableStore):
        super().__init__(database, tables)

    @on_pool
    def get(self, enricher: str, type_: str, value: str) -> Optional[Dict[str, Any]]:
        return _result(self._one(f"SELECT * FROM {RESULT} WHERE id = $id", {"id": _id(enricher, type_, value)}))

    @on_pool
    def put(self, enricher: str, type_: str, value: str, result: Dict[str, Any], ttl_s: int) -> None:
        row = _row(enricher, type_, value, result, ttl_s)
        db = self._db()
        self._commit(db, lambda: self._t.write_row(db, WORKSPACE, "results", row))

    @on_pool
    def forget(self, enricher: Optional[str] = None) -> int:
        """Drop every cached result, or one enricher's. Returns how many."""
        if enricher is None:
            return self._command(f"DELETE FROM {RESULT}")
        return self._command(f"DELETE FROM {RESULT} WHERE enricher = $enricher", {"enricher": enricher})

    @on_pool
    def prune(self) -> int:
        """Delete the expired rows; how many went."""
        return self._delete_older(RESULT, "expires_at", time.time())

    def close(self) -> None:
        """Nothing to close: the database is shared (api/app.py closes it)."""
