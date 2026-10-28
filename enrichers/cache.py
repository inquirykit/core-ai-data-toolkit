"""
Cache of enricher results, one row per (enricher, type, value): a /db table
in the ``enrichers`` namespace (vectorstore/tables.py), which the admin's Data
section shows read-only. MemoryCache keeps the rows in this process instead,
for a service without a database (ARCADEDB_PATH empty) and for tests. Their
methods are async: ResultCache's run on the database's thread pool
(vectorstore/engine.py).

A result is served from here until its enricher's TTL has passed, so a value
looked up twice costs one request against the source's quota. Rows hold the
source's reply as it came, which stays inside the service: the API serves
it to the toolkit's own clients only. Expired rows go when ``prune`` runs
(api/app.py schedules it hourly).
"""

import hashlib
import json
import time
from typing import Any, Dict, Optional, Tuple

from vectorstore import Database, DocumentStore, on_pool
from vectorstore.tables import TableSchema, TableStore, schema_of, table_type

NAMESPACE = "enrichers"
TABLES: Dict[str, TableSchema] = {
    # raw is the source's reply as JSON text: as a map, ArcadeDB would read a top-level "@type" (JSON-LD) as one
    # of its type names, and refuse an integer of 2^63 or more.
    "results": schema_of("enricher:string+", "type:string", "value:string", "status:string", "http_status:integer",
                         "outputs:object", "raw:string", "observed_at:number", "expires_at:number+"),
}
RESULT = table_type(NAMESPACE, "results")


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
    def __init__(self, database: Database, tables: TableStore):
        super().__init__(database)
        self._t = tables
        tables.manage(NAMESPACE, "the enrichers' result cache: it fills itself", TABLES)

    def _ensure_schema(self, db) -> None:
        super()._ensure_schema(db)
        self._t.ensure_managed(NAMESPACE)

    @on_pool
    def get(self, enricher: str, type_: str, value: str) -> Optional[Dict[str, Any]]:
        rows = self._select(f"SELECT FROM {RESULT} WHERE id = :id", {"id": _id(enricher, type_, value)})
        return _result(rows[0] if rows else None)

    @on_pool
    def put(self, enricher: str, type_: str, value: str, result: Dict[str, Any], ttl_s: int) -> None:
        row = _row(enricher, type_, value, result, ttl_s)
        self._command(f"UPDATE {RESULT} CONTENT :row UPSERT WHERE id = :id", {"row": row, "id": row["id"]})

    @on_pool
    def forget(self, enricher: Optional[str] = None) -> int:
        """Drop every cached result, or one enricher's. Returns how many."""
        if enricher is None:
            return self._command(f"DELETE FROM {RESULT}")
        return self._command(f"DELETE FROM {RESULT} WHERE enricher = :enricher", {"enricher": enricher})

    @on_pool
    def prune(self) -> int:
        """Delete the expired rows; how many went."""
        return self._delete_older(RESULT, "expires_at", time.time())

    def close(self) -> None:
        """Nothing to close: the database is shared (api/app.py closes it)."""


class MemoryCache:
    """The same cache in a dict, for a service without a database."""

    def __init__(self) -> None:
        # Read and written on the event loop only, never across an await: no lock.
        self._rows: Dict[Tuple[str, str, str], Dict[str, Any]] = {}

    async def get(self, enricher: str, type_: str, value: str) -> Optional[Dict[str, Any]]:
        return _result(self._rows.get((enricher, type_, value)))

    async def put(self, enricher: str, type_: str, value: str, result: Dict[str, Any], ttl_s: int) -> None:
        self._rows[(enricher, type_, value)] = _row(enricher, type_, value, result, ttl_s)

    async def forget(self, enricher: Optional[str] = None) -> int:
        keys = [k for k in self._rows if enricher is None or k[0] == enricher]
        for k in keys:
            del self._rows[k]
        return len(keys)

    async def prune(self) -> int:
        now = time.time()
        expired = [k for k, row in self._rows.items() if row["expires_at"] <= now]
        for k in expired:
            del self._rows[k]
        return len(expired)

    def close(self) -> None:
        pass
