"""
Which remote each bucket names, and which of its files are copied there.

A bucket names a remote (remotes.py) or none, in the service's own type
``tk_bucket_remote``: a record per bucket that ever named one, with the
remote's name (null for none) and the number of its naming, which goes up by
one each time the bucket names another remote, none, or the same one again.
A bucket's files say when each reached its remote (``copied``, a field of the
bucket's rows, db/schema.py's BUCKET): naming another remote, or none, takes
``copied`` from every file, so each is copied again to the new one, and a
file with no ``copied`` waits to be (``?copied=null`` lists them).

A copy (copies.py) marks its files against the naming it began with
(``mark_copied``), under the same lock a naming takes: so one begun before
the bucket's remote was named again (another, none, or the same after its
place was changed meanwhile) never marks a file after it, and nothing is
marked as copied to a place it is not at. The number is kept with the
naming, so it holds across restarts.

A bucket's record goes when the bucket does (``forget``: its files' code
tells copies.py, which tells this), and one left from a bucket of the same
name is forgotten before a bucket is made, so a new bucket names no remote.

The rows are read and written here in SQL, as the bucket's rows' writer
(their file routes, buckets/files.py, the only other one).
"""

import threading
from typing import Dict, List, Optional, Sequence, Tuple

from core.errors import NotFound
from db import Database, DocumentStore, on_pool
from db.tables import SCAN_BATCH, TableStore, table_type

NAMING = "tk_bucket_remote"

# A bucket's remote and the number of its naming.
Naming = Tuple[str, int]


class Namings(DocumentStore):
    """The buckets' remotes, in ``tk_bucket_remote``, and their files' ``copied``; ``tables`` says which buckets
    there are."""

    # A bucket's record, by its workspace and its name.
    SCHEMA = {NAMING: {"props": {"workspace": "VARCHAR", "name": "VARCHAR", "remote": "VARCHAR", "version": "BIGINT"},
                       "indexes": [("workspace, name", True), ("remote", False)]}}

    def __init__(self, database: Database, tables: TableStore):
        super().__init__(database)
        self.tables = tables
        # Held by a naming and by a mark, so no copy marks a file against a naming that has changed.
        self._lock = threading.Lock()

    def _record(self, workspace: str, bucket: str) -> Optional[dict]:
        return self._one(f"SELECT remote, version FROM {NAMING} WHERE workspace = $w AND name = $b",
                         {"w": workspace, "b": bucket})

    def _named(self, workspace: str, bucket: str) -> Optional[Naming]:
        """The bucket's remote and the number of its naming; None without a remote."""
        row = self._record(workspace, bucket)
        return (row["remote"], int(row["version"])) if row and row.get("remote") else None

    named = on_pool(_named)

    async def remote_of(self, workspace: str, bucket: str) -> Optional[str]:
        """The bucket's remote, by name; None without one."""
        naming = await self.named(workspace, bucket)
        return naming[0] if naming else None

    def _name(self, workspace: str, bucket: str, remote: Optional[str]) -> bool:
        """Name the bucket's remote (None: none); whether that changed it. A change takes ``copied`` from every file
        first, a batch per transaction, and numbers the naming anew. NotFound for a bucket that is not there (dropped
        since the caller checked it); that the remote exists is the caller's to check."""
        with self._lock:
            if not self.tables.has_table(workspace, bucket):
                raise NotFound(f"No bucket named {bucket!r} in {workspace!r}")
            row = self._record(workspace, bucket)
            if (row.get("remote") if row else None) == remote:
                return False
            t = table_type(workspace, bucket)
            while ids := [r["id"] for r in self._select(f"SELECT id FROM {t} WHERE copied IS NOT NULL LIMIT $n",
                                                        {"n": SCAN_BATCH})]:
                self._command(f"UPDATE {t} SET copied = NULL WHERE id = ANY($ids)", {"ids": ids})
            version = int(row["version"]) + 1 if row else 1
            db = self._db()
            self._commit(db, lambda: (
                db.execute(f"DELETE FROM {NAMING} WHERE workspace = $w AND name = $b", {"w": workspace, "b": bucket}),
                self._insert(db, NAMING, {"workspace": workspace, "name": bucket, "remote": remote, "version": version})))
            return True

    name = on_pool(_name)

    def _mark_copied(self, workspace: str, bucket: str, ids: List[str], naming: Naming, when: float) -> bool:
        """Say that the files with these ids reached the remote of ``naming`` (``named``, as the copy began) at
        ``when``; False, marking none, when the bucket's remote has been named again since (or it is gone)."""
        with self._lock:
            if self._named(workspace, bucket) != tuple(naming) or not self.tables.has_table(workspace, bucket):
                return False
            self._command(f"UPDATE {table_type(workspace, bucket)} SET copied = $copied WHERE id = ANY($ids)",
                          {"copied": when, "ids": ids})
            return True

    mark_copied = on_pool(_mark_copied)

    def _uncopied(self, workspace: str, bucket: str, after: str = "", limit: int = SCAN_BATCH) -> List[dict]:
        """The bucket's files not copied to its remote yet (``id``, ``size``, ``sha256``), by id, from the first
        after ``after``: a page of at most ``limit``; none for a bucket that is not there."""
        if not self.tables.has_table(workspace, bucket):
            return []
        return self._select(f"SELECT id, size, sha256 FROM {table_type(workspace, bucket)} WHERE copied IS NULL "
                            f"AND id > $last ORDER BY id LIMIT $n", {"last": after, "n": limit})

    uncopied = on_pool(_uncopied)

    def _buckets(self) -> Dict[str, List[str]]:
        """Each remote a bucket names -> those buckets, as ``workspace/bucket``."""
        named: Dict[str, List[str]] = {}
        for r in self._select(f"SELECT workspace, name, remote FROM {NAMING} WHERE remote IS NOT NULL "
                              "ORDER BY workspace, name"):
            named.setdefault(r["remote"], []).append(f"{r['workspace']}/{r['name']}")
        return named

    buckets = on_pool(_buckets)

    def forget(self, workspace: str, buckets: Sequence[str]) -> None:
        """Forget these buckets' remotes: dropped, or about to be made anew. Under the lock, so a naming that found
        the bucket still there, just before its drop, is forgotten after it, not left behind. Blocking."""
        if buckets:
            # Taken while the table store's schema lock is held (a bucket is made under it, buckets/files.py's
            # check_new), and a naming does not take that one: it reads which tables there are from memory.
            with self._lock:
                self._command(f"DELETE FROM {NAMING} WHERE workspace = $w AND name = ANY($b)",
                              {"w": workspace, "b": list(buckets)})
