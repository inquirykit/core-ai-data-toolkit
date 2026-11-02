"""
The audit log: every change made through the admin's routes (and the setup
wizard's), and every delete, whoever makes it, in the ArcadeDB database. Which
requests those are is api/audit.py's to say; this keeps them.

An entry is the request's method, route and path (with its query), the key it
carried (``actor``: "admin", "client", "none", "wrong" for a key that is
neither, "open" while no key is set), where it came from (the client's
address, X-Forwarded-For, the User-Agent), its status and how long it took.
A refused attempt is kept too. Bodies are not, so no secret an admin saves
(an enricher's key, the wizard's keys) lands here.

Recording never blocks: entries go onto a queue that one background thread
writes (db.BatchWriter). They are kept AUDIT_RETENTION_DAYS, then
pruned hourly.
"""

from typing import Any, Dict, List, Optional

from db import Database, QueuedStore, Where, on_pool

AUDIT = "tk_audit"
SCHEMA: Dict[str, Dict[str, Any]] = {
    AUDIT: {
        "props": {
            "id": "LONG", "ts": "DOUBLE",
            "method": "STRING",
            "route": "STRING",       # the route's path, /db/{namespace}/{table}
            "path": "STRING",        # the path asked for, with its query
            "status": "INTEGER", "duration_ms": "DOUBLE",
            "actor": "STRING",       # admin | client | none | wrong | open
            "ip": "STRING", "forwarded": "STRING", "agent": "STRING",
        },
        "indexes": [("id", True), ("ts", False)],
    },
}
# Outcomes the list filters by: the status classes.
OUTCOMES = {"ok": (200, 400), "refused": (400, 500), "failed": (500, 600)}
MAX_TEXT = 2000


class AuditLog(QueuedStore):
    """The entries; the list pages back by their ids."""

    SCHEMA = SCHEMA
    RETAINED = ((AUDIT, "ts"),)

    def __init__(self, database: Database, retention_days: float = 365.0, max_queue: int = 10_000):
        super().__init__(database, "audit-writer", retention_days, max_queue)

    def record(self, *, ts: float, method: str, route: str, path: str, status: int, duration_ms: float, actor: str,
               ip: Optional[str] = None, forwarded: Optional[str] = None, agent: Optional[str] = None) -> None:
        self.writer.put((AUDIT, {
            "id": self._next_id(), "ts": ts, "method": method, "route": route, "path": path[:MAX_TEXT],
            "status": int(status), "duration_ms": float(duration_ms), "actor": actor, "ip": ip,
            "forwarded": forwarded[:200] if forwarded else None, "agent": agent[:200] if agent else None,
        }))

    @on_pool
    def entries(self, limit: int = 100, before_id: Optional[int] = None, method: Optional[str] = None,
                route: Optional[str] = None, actor: Optional[str] = None, outcome: Optional[str] = None,
                since: Optional[float] = None, until: Optional[float] = None) -> List[Dict[str, Any]]:
        """Newest first; with ``before_id``, only those older than that one. ``route`` is a case-insensitive
        substring of the route or the path; ``outcome`` is ok (2xx, 3xx), refused (4xx) or failed (5xx)."""
        low, high = OUTCOMES[outcome] if outcome else (None, None)
        where = (Where("id IS NOT NULL").add("id", "<", before_id).add("method", "=", method).add("actor", "=", actor)
                 .add("status", ">=", low).add("status", "<", high).add("ts", ">=", since).add("ts", "<=", until)
                 .like(["route", "path"], route))
        return self._select(f"SELECT FROM {AUDIT}{where} ORDER BY id DESC LIMIT {int(limit)}", where.params)
