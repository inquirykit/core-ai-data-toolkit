"""
Client keys: the API keys an admin makes in /admin, one for each server or machine that calls the service, so each
can expire or be revoked on its own. They open the client routes only; the admin's routes stay with ADMIN_API_KEY,
the superadmin key in the settings, which cannot be revoked from here (api/deps.py has the rule).

A key is ``aitk_<id>_<secret>``: the prefix lets a secret scanner find a leaked one, and the id is the key's row,
which the list and the audit log show. The token is answered once, when it is made. Only its SHA-256 is kept: the
secret is 32 random bytes, so no slow password hash is needed, and none would cost each request milliseconds.

Every row is kept in memory once read, by its hash, so checking a request costs one hash and one lookup, never a
database call. One process opens the database, so making or revoking a key changes both at once, and a revoke holds
from the next request. A key's last use is noted in memory and written by ``flush`` (each minute, and at shutdown).

A revoked key keeps its row, so the audit log can still name it, and a revoke cannot be undone: make a new key.
A key's grants (access/grants.py) say which tables and models it reaches; they change from the next request too.
"""

import hashlib
import secrets
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Literal, Optional, Tuple, get_args

from core.errors import NotFound
from db import Database, DocumentStore, on_pool

from .grants import Grants

TABLE = "tk_client_keys"
SCHEMA: Dict[str, Dict[str, Any]] = {
    TABLE: {
        "props": {
            "id": "VARCHAR", "hash": "VARCHAR", "name": "VARCHAR",
            "seq": "INTEGER",  # 1, 2, 3... in the order they were made: the list pages by it
            "created_at": "DOUBLE", "expires_at": "DOUBLE", "revoked_at": "DOUBLE", "last_used_at": "DOUBLE",
            "grants": "JSON",  # access/grants.py's Grants
        },
        "indexes": [("id", True), ("hash", True)],
    },
}
PREFIX = "aitk_"
# A key's state, as the API declares it too (api/responses/keys.py).
State = Literal["live", "expired", "revoked"]
STATES = get_args(State)


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass
class ClientKey:
    id: str
    hash: str
    name: str
    seq: int
    created_at: float
    expires_at: Optional[float] = None
    revoked_at: Optional[float] = None
    last_used_at: Optional[float] = None
    grants: Grants = field(default_factory=Grants)

    def state(self, now: Optional[float] = None) -> str:
        """live, expired or revoked (a revoked key that has expired since is still revoked)."""
        if self.revoked_at is not None:
            return "revoked"
        if self.expires_at is not None and (now or time.time()) >= self.expires_at:
            return "expired"
        return "live"

    def row(self) -> Dict[str, Any]:
        """The key as its table keeps it."""
        return {**asdict(self), "grants": self.grants.model_dump()}

    def public(self) -> Dict[str, Any]:
        """The key as the list shows it: everything but its hash and place in the order, and its state."""
        row = self.row()
        del row["hash"], row["seq"]
        return {**row, "state": self.state()}


class ClientKeys(DocumentStore):
    SCHEMA = SCHEMA

    def __init__(self, database: Database):
        super().__init__(database)
        self._by_hash: Optional[Dict[str, ClientKey]] = None
        self._lock = threading.Lock()
        # Last uses not yet written: key id -> time.
        self._used: Dict[str, float] = {}

    def _load(self) -> Dict[str, ClientKey]:
        with self._lock:
            if self._by_hash is None:
                rows = self._select(f"SELECT * FROM {TABLE}")
                self._by_hash = {r["hash"]: ClientKey(**{**r, "grants": Grants.model_validate(r["grants"] or {})})
                                 for r in rows}
            return self._by_hash

    # Read at startup (api/app.py), so a request never waits on the database for it.
    load = on_pool(_load)

    def _keys(self) -> Dict[str, ClientKey]:
        return self._by_hash if self._by_hash is not None else self._load()

    def made_any(self) -> bool:
        """Whether a key was ever made, live or not: from the first, the client routes need a key."""
        return bool(self._keys())

    def find(self, token: str) -> Optional[ClientKey]:
        """The key ``token`` is, whatever its state; None for a token that is not one. A live one is noted as used."""
        if not token.startswith(PREFIX):
            return None
        key = self._keys().get(digest(token))
        if key is not None and key.state() == "live":
            key.last_used_at = self._used[key.id] = time.time()
        return key

    def _make(self, name: str, expires_in_days: Optional[float] = None,
              grants: Optional[Grants] = None) -> Tuple[ClientKey, str]:
        """A new live key and its token, the only time the token is seen."""
        keys = self._keys()
        with self._lock:
            ids = {k.id for k in keys.values()}
            key_id = secrets.token_hex(4)
            while key_id in ids:
                key_id = secrets.token_hex(4)
            token = f"{PREFIX}{key_id}_{secrets.token_urlsafe(32)}"
            now = time.time()
            seq = max((k.seq for k in keys.values()), default=0) + 1
            key = ClientKey(id=key_id, hash=digest(token), name=name, seq=seq, created_at=now,
                            expires_at=now + expires_in_days * 86400 if expires_in_days else None, grants=grants or Grants())
            db = self._db()
            self._commit(db, lambda: self._insert(db, TABLE, key.row()))
            keys[key.hash] = key
        return key, token

    make = on_pool(_make)

    def _by_id(self, key_id: str) -> ClientKey:
        key = next((k for k in self._keys().values() if k.id == key_id), None)
        if key is None:
            raise NotFound(f"No client key {key_id!r}")
        return key

    def _revoke(self, key_id: str) -> ClientKey:
        """Revoke a key from its next request on; one revoked already stays as it was. 404 for an unknown id."""
        key = self._by_id(key_id)
        with self._lock:
            if key.revoked_at is None:
                now = time.time()
                self._command(f"UPDATE {TABLE} SET revoked_at = $now WHERE id = $id", {"now": now, "id": key_id})
                key.revoked_at = now
        return key

    revoke = on_pool(_revoke)

    def _grant(self, key_id: str, grants: Grants) -> ClientKey:
        """Replace a key's grants, from its next request on. 404 for an unknown id."""
        key = self._by_id(key_id)
        with self._lock:
            value = self._values(TABLE, {"grants": grants.model_dump()})["grants"]
            self._command(f"UPDATE {TABLE} SET grants = $grants WHERE id = $id", {"grants": value, "id": key_id})
            key.grants = grants
        return key

    grant = on_pool(_grant)

    def _listed(self, state: Optional[str] = None, before: Optional[int] = None) -> List[ClientKey]:
        """Every key, or those in ``state``, newest first; with ``before``, those made before the one of that seq."""
        now = time.time()
        found = [k for k in self._keys().values()
                 if (state is None or k.state(now) == state) and (before is None or k.seq < before)]
        return sorted(found, key=lambda k: k.seq, reverse=True)

    listed = on_pool(_listed)

    def _flush(self) -> None:
        """Write the last uses noted since the last flush."""
        if not self._used:
            return
        used, self._used = self._used, {}
        db = self._db()

        def work() -> None:
            for key_id, at in used.items():
                db.execute(f"UPDATE {TABLE} SET last_used_at = $at WHERE id = $id", {"at": at, "id": key_id})

        self._commit(db, work)

    flush = on_pool(_flush)
