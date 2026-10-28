"""
The capture ledgers in the embedded ArcadeDB database every store shares
(vectorstore/engine.py), plus the files on disk (files.py).

Evidence is kept by workspace: the unit that is isolated, verified, exported
and, one day, deleted whole. Each workspace has its own ledger (``Ledger``):
its own /db namespace (vectorstore/tables.py) with the same two tables, cases
and captures (``TABLES``, the template), its own hash chain and its own files.
The default workspace is the ``evidence`` namespace, which held every capture
before there were workspaces; another one is ``evidence_<its id, - as _>``,
under EVIDENCE_DIR/workspaces/<id>/. The namespaces are browsable in /admin's
Data section with the admin key, read-only there, written only here. The
workspaces themselves are rows of ``tk_evidence_workspace``; the application
they stand for may name them, by its own ids.

Annotations (``annotations``: tags and analyst notes added to a capture after
it was taken) and todos (``todos``: a case's to-do list) are the mutable
tables: a row per annotated capture, keyed by its capture id, and a row per
case with a list, keyed by its case id, apart from the signed records. They are
never part of a record, its hash, the chain or ``verify``.

Each capture row keeps the record's canonical bytes verbatim (``record_json``),
their hash, the signature and the time stamp token. A workspace's records form
a hash chain: each one names the hash of the previous, so editing, reordering
or deleting an earlier record breaks every link after it. Appends take the
workspace's lock across reading the chain head, writing the files and
committing the row, so two uploads can never build on the same head; the
unique index on ``seq`` is the backstop. Workspaces append side by side.

Every method here blocks: the ``async`` ones run the sync ones on the
database's thread pool, never on the event loop.
"""

import base64
import json
import logging
import threading
from typing import Any, Dict, List, Optional

from errors import Conflict, NotFound
from vectorstore import Database, DocumentStore, on_pool
from vectorstore.tables import TableSchema, TableStore, schema_of, table_type

from .files import CaptureFiles
from .record import (
    DEFAULT_CASE_ID,
    DEFAULT_CASE_NAME,
    DEFAULT_WORKSPACE_ID,
    DEFAULT_WORKSPACE_NAME,
    GENESIS_HASH,
    VerifiedArtifact,
    build_record,
    canonical,
    check_workspace_id,
    new_id,
    now_iso,
    sha256_hex,
)
from .signing import Signer

logger = logging.getLogger(__name__)

# The default workspace's namespace, and the start of every other one's.
NAMESPACE = "evidence"
WORKSPACE_PREFIX = "evidence_"
OWNER = "the evidence ledger: records change only through /evidence"
WORKSPACES = "tk_evidence_workspace"
# Every workspace's tables (each has a unique string id besides; a capture's is its capture_id).
TABLES: Dict[str, TableSchema] = {
    "cases": schema_of("name:string", "created_at:string"),
    "captures": schema_of(
        "seq:integer!", "capture_id:string!", "case_id:string+", "received_at:string", "record_json:string",
        "record_hash:string", "signature:string", "tsr:string", "tsa_gen_time:string", "tsa_error:string",
        "url:string", "title:string",
    ),
    # Mutable, apart from the records: one row per annotated capture (its id is the capture's).
    "annotations": schema_of("capture_id:string!", "case_id:string+", "tags:string[]", "notes:object[]", "updated_at:string"),
    # Mutable too: one row per case with a to-do list (its id is the case's).
    "todos": schema_of("case_id:string!", "items:object[]", "updated_at:string"),
}
# The default workspace's tables.
CASE, CAPTURE = table_type(NAMESPACE, "cases"), table_type(NAMESPACE, "captures")
CAPTURE_COLUMNS = "seq, capture_id, case_id, received_at, record_json, record_hash, signature, tsr, tsa_gen_time, tsa_error, url, title"


def namespace_of(workspace_id: str) -> str:
    """The /db namespace of a workspace's ledger."""
    if workspace_id == DEFAULT_WORKSPACE_ID:
        return NAMESPACE
    return WORKSPACE_PREFIX + workspace_id.replace("-", "_")


def _page(client: Any) -> Dict[str, Any]:
    page = client.get("page") if isinstance(client, dict) else None
    return page if isinstance(page, dict) else {}


def page_url(client: Any) -> Optional[str]:
    page = _page(client)
    url = page.get("documentUrl") or page.get("tabUrl")
    return url if isinstance(url, str) else None


def page_title(client: Any) -> Optional[str]:
    title = _page(client).get("title")
    return title if isinstance(title, str) else None


class Ledger(DocumentStore):
    """One workspace's cases and captures: its namespace, its hash chain and its files.

    Made by ``EvidenceStore.ledger``, once per workspace, so its append lock is the only one.
    """

    def __init__(self, database: Database, tables: TableStore, workspace_id: str, files: CaptureFiles,
                 signer: Signer, version: str):
        super().__init__(database)
        self._t = tables
        self.workspace_id = workspace_id
        self.namespace = namespace_of(workspace_id)
        self._cases, self._captures = table_type(self.namespace, "cases"), table_type(self.namespace, "captures")
        self._annotations = table_type(self.namespace, "annotations")
        self._todos = table_type(self.namespace, "todos")
        self.files = files
        self.signer = signer
        self.version = version
        self._append_lock = threading.Lock()

    # --------------------------------------------------------------- plumbing
    def _ensure_schema(self, db) -> None:
        super()._ensure_schema(db)
        self._t.ensure_managed(self.namespace)
        if not self._rows(db, f"SELECT id FROM {self._cases} WHERE id = :id", {"id": DEFAULT_CASE_ID}):
            doc = {"id": DEFAULT_CASE_ID, "name": DEFAULT_CASE_NAME, "created_at": now_iso()}
            self._commit(db, lambda: self._insert(db, self._cases, doc))

    # ------------------------------------------------------------------ cases
    @staticmethod
    def _case(row: Dict[str, Any]) -> Dict[str, Any]:
        return {"id": row["id"], "name": row["name"], "createdAt": row["created_at"]}

    def _list_cases(self) -> List[Dict[str, Any]]:
        db = self._db()
        sql = f"SELECT id, name, created_at FROM {self._cases} ORDER BY created_at, id"
        return [self._case(r) for r in self._rows(db, sql)]

    async def list_cases(self) -> List[Dict[str, Any]]:
        return await self._run(self._list_cases)

    def _create_case(self, name: str) -> Dict[str, Any]:
        db = self._db()
        doc = {"id": new_id(), "name": name, "created_at": now_iso()}
        self._commit(db, lambda: self._insert(db, self._cases, doc))
        return self._case(doc)

    async def create_case(self, name: str) -> Dict[str, Any]:
        return await self._run(self._create_case, name)

    def _case_exists(self, case_id: str) -> bool:
        return bool(self._select(f"SELECT id FROM {self._cases} WHERE id = :id", {"id": case_id}))

    async def case_exists(self, case_id: str) -> bool:
        return await self._run(self._case_exists, case_id)

    # --------------------------------------------------------------- captures
    def _head(self, db) -> Dict[str, Any]:
        rows = self._rows(db, f"SELECT seq, record_hash FROM {self._captures} ORDER BY seq DESC LIMIT 1")
        return rows[0] if rows else {"seq": 0, "record_hash": GENESIS_HASH}

    def _append(self, case_id: str, client: Any, artifacts: List[VerifiedArtifact], enrichment: Dict[str, Any]) -> Dict[str, Any]:
        """Build, sign and store the next record in the workspace's chain; its files first, then the row."""
        with self._append_lock:
            db = self._db()
            if not self._case_exists(case_id):
                raise NotFound("unknown case")
            head = self._head(db)
            seq = int(head["seq"]) + 1
            capture_id = new_id()
            received_at = now_iso()
            record = build_record(
                seq=seq, capture_id=capture_id, workspace_id=self.workspace_id, case_id=case_id,
                received_at=received_at, prev_hash=head["record_hash"], version=self.version,
                signing_key_sha256=self.signer.fingerprint, artifacts=artifacts, client=client, enrichment=enrichment,
            )
            data = canonical(record)
            record_hash = sha256_hex(data)
            signature = self.signer.sign(data)
            doc = {
                "id": capture_id, "seq": seq, "capture_id": capture_id, "case_id": case_id, "received_at": received_at,
                "record_json": data.decode("utf-8"), "record_hash": record_hash, "signature": signature.hex(),
                "tsr": None, "tsa_gen_time": None, "tsa_error": None,
                "url": page_url(client), "title": page_title(client),
            }
            self.files.write_capture(capture_id, data, signature, artifacts)
            try:
                self._commit(db, lambda: self._insert(db, self._captures, doc))
            except Exception:
                self.files.remove(capture_id)
                raise
            return {
                "captureId": capture_id, "seq": seq, "receivedAt": received_at,
                "recordHash": record_hash, "signature": signature.hex(), "recordJson": data,
            }

    async def append(self, case_id: str, client: Any, artifacts: List[VerifiedArtifact], enrichment: Dict[str, Any]) -> Dict[str, Any]:
        return await self._run(self._append, case_id, client, artifacts, enrichment)

    def _set_timestamp(self, capture_id: str, response_der: bytes, gen_time: str) -> None:
        db = self._db()
        self.files.write_timestamp(capture_id, response_der)
        params = {"id": capture_id, "tsr": base64.b64encode(response_der).decode(), "g": gen_time}
        self._commit(db, lambda: db.command(
            "sql", f"UPDATE {self._captures} SET tsr = :tsr, tsa_gen_time = :g, tsa_error = null WHERE capture_id = :id",
            params,
        ))

    async def set_timestamp(self, capture_id: str, response_der: bytes, gen_time: str) -> None:
        await self._run(self._set_timestamp, capture_id, response_der, gen_time)

    def _set_timestamp_error(self, capture_id: str, error: str) -> None:
        db = self._db()
        params = {"id": capture_id, "e": error[:2000]}
        self._commit(db, lambda: db.command(
            "sql", f"UPDATE {self._captures} SET tsa_error = :e WHERE capture_id = :id", params
        ))

    async def set_timestamp_error(self, capture_id: str, error: str) -> None:
        await self._run(self._set_timestamp_error, capture_id, error)

    def _pending_timestamps(self) -> List[Dict[str, Any]]:
        db = self._db()
        sql = f"SELECT capture_id, record_json, record_hash FROM {self._captures} WHERE tsr IS NULL ORDER BY seq"
        return self._rows(db, sql)

    async def pending_timestamps(self) -> List[Dict[str, Any]]:
        return await self._run(self._pending_timestamps)

    def _get(self, capture_id: str) -> Optional[Dict[str, Any]]:
        sql = f"SELECT {CAPTURE_COLUMNS} FROM {self._captures} WHERE capture_id = :id"
        rows = self._select(sql, {"id": capture_id})
        return rows[0] if rows else None

    async def get(self, capture_id: str) -> Optional[Dict[str, Any]]:
        return await self._run(self._get, capture_id)

    @staticmethod
    def summary(row: Dict[str, Any]) -> Dict[str, Any]:
        """A capture as GET /evidence/cases/{id}/captures lists it."""
        return {
            "seq": int(row["seq"]), "captureId": row["capture_id"], "receivedAt": row["received_at"],
            "url": row.get("url"), "title": row.get("title"), "recordHash": row["record_hash"],
            "timestampedAt": row.get("tsa_gen_time"),
        }

    def _list_by_case(self, case_id: str) -> List[Dict[str, Any]]:
        db = self._db()
        if not self._case_exists(case_id):
            raise NotFound("unknown case")
        sql = (f"SELECT seq, capture_id, received_at, url, title, record_hash, tsa_gen_time FROM {self._captures} "
               f"WHERE case_id = :c ORDER BY seq DESC")
        return [self.summary(r) for r in self._rows(db, sql, {"c": case_id})]

    async def list_by_case(self, case_id: str) -> List[Dict[str, Any]]:
        return await self._run(self._list_by_case, case_id)

    # ------------------------------------------------------------ annotations
    @staticmethod
    def _annotation(row: Dict[str, Any]) -> Dict[str, Any]:
        notes = [{"id": n["id"], "text": n["text"], "createdAt": n["created_at"], "updatedAt": n.get("updated_at")}
                 for n in row.get("notes") or []]
        return {"captureId": row["capture_id"], "tags": list(row.get("tags") or []), "notes": notes,
                "updatedAt": row["updated_at"]}

    def _annotations_by_case(self, case_id: str) -> List[Dict[str, Any]]:
        """The annotations of the case's captures that have any, oldest capture first."""
        db = self._db()
        if not self._case_exists(case_id):
            raise NotFound("unknown case")
        sql = f"SELECT capture_id, tags, notes, updated_at FROM {self._annotations} WHERE case_id = :c"
        rows = self._rows(db, sql, {"c": case_id})
        order = {r["capture_id"]: int(r["seq"]) for r in self._rows(
            db, f"SELECT seq, capture_id FROM {self._captures} WHERE case_id = :c", {"c": case_id})}
        rows.sort(key=lambda r: order.get(r["capture_id"], 0))
        return [self._annotation(r) for r in rows]

    async def annotations_by_case(self, case_id: str) -> List[Dict[str, Any]]:
        return await self._run(self._annotations_by_case, case_id)

    def _get_annotation(self, capture_id: str) -> Optional[Dict[str, Any]]:
        sql = f"SELECT capture_id, tags, notes, updated_at FROM {self._annotations} WHERE capture_id = :id"
        rows = self._select(sql, {"id": capture_id})
        return self._annotation(rows[0]) if rows else None

    async def get_annotation(self, capture_id: str) -> Optional[Dict[str, Any]]:
        return await self._run(self._get_annotation, capture_id)

    def _annotate(self, capture_id: str, tags: Optional[List[str]], notes: Optional[List[Dict[str, Any]]]) -> Dict[str, Any]:
        """Replace the capture's tags, its notes, or both (None keeps a list); 404 for an unknown capture.

        A note keeps its id and createdAt when ``notes`` names a known id; its updatedAt is set when its text
        changed. A note without an id (or with an unknown one) is new.
        """
        db = self._db()
        found = self._rows(db, f"SELECT case_id FROM {self._captures} WHERE capture_id = :id", {"id": capture_id})
        if not found:
            raise NotFound("unknown capture")
        rows = self._rows(db, f"SELECT tags, notes FROM {self._annotations} WHERE capture_id = :id", {"id": capture_id})
        current = rows[0] if rows else {"tags": [], "notes": []}
        now = now_iso()
        kept = list(current.get("tags") or []) if tags is None else list(tags)
        if notes is None:
            merged = list(current.get("notes") or [])
        else:
            known = {n["id"]: n for n in current.get("notes") or []}
            merged = []
            for note in notes:
                before = known.get(note.get("id") or "")
                if before is None:
                    merged.append({"id": new_id(), "text": note["text"], "created_at": now, "updated_at": None})
                elif before["text"] == note["text"]:
                    merged.append(dict(before))
                else:
                    merged.append({**before, "text": note["text"], "updated_at": now})
        doc = {"id": capture_id, "capture_id": capture_id, "case_id": found[0]["case_id"], "tags": kept, "notes": merged,
               "updated_at": now}
        self._commit(db, lambda: db.command(
            "sql", f"UPDATE {self._annotations} CONTENT :doc UPSERT WHERE capture_id = :id", {"doc": doc, "id": capture_id}
        ))
        return self._annotation(doc)

    async def annotate(self, capture_id: str, tags: Optional[List[str]], notes: Optional[List[Dict[str, Any]]]) -> Dict[str, Any]:
        return await self._run(self._annotate, capture_id, tags, notes)

    # ------------------------------------------------------------------ todos
    @staticmethod
    def _todo_list(case_id: str, row: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        row = row or {}
        todos = [{"id": t["id"], "text": t["text"], "done": bool(t.get("done")), "createdAt": t["created_at"],
                  "updatedAt": t.get("updated_at"), "doneAt": t.get("done_at")} for t in row.get("items") or []]
        return {"caseId": case_id, "todos": todos, "updatedAt": row.get("updated_at")}

    def _todos_of(self, case_id: str) -> Dict[str, Any]:
        """The case's to-do list, empty when it never had one; 404 for an unknown case."""
        db = self._db()
        if not self._case_exists(case_id):
            raise NotFound("unknown case")
        rows = self._rows(db, f"SELECT items, updated_at FROM {self._todos} WHERE case_id = :c", {"c": case_id})
        return self._todo_list(case_id, rows[0] if rows else None)

    async def todos_of(self, case_id: str) -> Dict[str, Any]:
        return await self._run(self._todos_of, case_id)

    def _set_todos(self, case_id: str, todos: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Replace the case's to-do list with ``todos``, in their order; 404 for an unknown case.

        A todo keeps its id and createdAt when ``todos`` names a known id (once: a repeat is new); its updatedAt is
        set when its text changed, its doneAt when it was checked, cleared when unchecked. A todo without an id (or
        with an unknown one) is new.
        """
        db = self._db()
        if not self._case_exists(case_id):
            raise NotFound("unknown case")
        rows = self._rows(db, f"SELECT items FROM {self._todos} WHERE case_id = :c", {"c": case_id})
        known = {t["id"]: t for t in (rows[0].get("items") if rows else None) or []}
        now = now_iso()
        items = []
        for todo in todos:
            before = known.pop(todo.get("id") or "", None)
            done = todo["done"]
            if before is None:
                items.append({"id": new_id(), "text": todo["text"], "done": done, "created_at": now,
                              "updated_at": None, "done_at": now if done else None})
                continue
            item = dict(before)
            if item["text"] != todo["text"]:
                item.update(text=todo["text"], updated_at=now)
            if bool(item.get("done")) != done:
                item.update(done=done, done_at=now if done else None)
            items.append(item)
        doc = {"id": case_id, "case_id": case_id, "items": items, "updated_at": now}
        self._commit(db, lambda: db.command(
            "sql", f"UPDATE {self._todos} CONTENT :doc UPSERT WHERE case_id = :c", {"doc": doc, "c": case_id}
        ))
        return self._todo_list(case_id, doc)

    async def set_todos(self, case_id: str, todos: List[Dict[str, Any]]) -> Dict[str, Any]:
        return await self._run(self._set_todos, case_id, todos)

    def _all_records(self) -> List[Dict[str, Any]]:
        return self._select(f"SELECT {CAPTURE_COLUMNS} FROM {self._captures} ORDER BY seq")

    @staticmethod
    def record_of(row: Dict[str, Any]) -> Any:
        return json.loads(row["record_json"])

    @staticmethod
    def tsr_of(row: Dict[str, Any]) -> Optional[bytes]:
        raw = row.get("tsr")
        return base64.b64decode(raw) if raw else None


class EvidenceStore(DocumentStore):
    """The evidence workspaces, each kept by its ``Ledger``; see the module docstring."""

    SCHEMA = {WORKSPACES: {"props": {"id": "STRING", "name": "STRING", "created_at": "STRING"},
                           "indexes": [("id", True)]}}

    def __init__(self, database: Database, tables: TableStore, files: CaptureFiles, signer: Signer,
                 version: str):
        """``files`` is the default workspace's; another one's are under ``files.root/workspaces/<id>``."""
        super().__init__(database)
        self._t = tables
        tables.manage(NAMESPACE, OWNER, TABLES)
        tables.manage_prefix(WORKSPACE_PREFIX, OWNER, TABLES)
        self.files = files
        self.signer = signer
        self.version = version
        self._ledgers: Dict[str, Ledger] = {}
        self._ledgers_lock = threading.Lock()

    # --------------------------------------------------------------- plumbing
    def _ensure_schema(self, db) -> None:
        super()._ensure_schema(db)
        if not self._rows(db, f"SELECT id FROM {WORKSPACES} WHERE id = :id", {"id": DEFAULT_WORKSPACE_ID}):
            doc = {"id": DEFAULT_WORKSPACE_ID, "name": DEFAULT_WORKSPACE_NAME, "created_at": now_iso()}
            self._commit(db, lambda: self._insert(db, WORKSPACES, doc))

    def _ledger_of(self, workspace_id: str) -> Ledger:
        """The workspace's one Ledger, made on first use (whether the workspace exists is the caller's to know)."""
        with self._ledgers_lock:
            ledger = self._ledgers.get(workspace_id)
            if ledger is None:
                files = self.files if workspace_id == DEFAULT_WORKSPACE_ID else CaptureFiles(
                    self.files.root / "workspaces" / workspace_id)
                ledger = Ledger(self.database, self._t, workspace_id, files, self.signer, self.version)
                self._ledgers[workspace_id] = ledger
            return ledger

    # ------------------------------------------------------------- workspaces
    @staticmethod
    def _workspace(row: Dict[str, Any]) -> Dict[str, Any]:
        return {"id": row["id"], "name": row["name"], "createdAt": row["created_at"]}

    def _workspace_rows(self) -> List[Dict[str, Any]]:
        sql = f"SELECT id, name, created_at FROM {WORKSPACES} ORDER BY created_at, id"
        return self._select(sql)

    def _list_workspaces(self) -> List[Dict[str, Any]]:
        return [self._workspace(r) for r in self._workspace_rows()]

    list_workspaces = on_pool(_list_workspaces)

    def _create_workspace(self, workspace_id: str, name: str) -> Dict[str, Any]:
        """A new workspace with its namespace, tables and inbox; 409 if the id or its namespace is taken."""
        db = self._db()
        check_workspace_id(workspace_id)
        if self._rows(db, f"SELECT id FROM {WORKSPACES} WHERE id = :id", {"id": workspace_id}):
            raise Conflict(f"workspace {workspace_id} exists")
        namespace = namespace_of(workspace_id)
        if self._t.has_namespace(namespace):
            raise Conflict(f"the namespace {namespace} is taken")
        doc = {"id": workspace_id, "name": name, "created_at": now_iso()}
        try:
            self._commit(db, lambda: self._insert(db, WORKSPACES, doc))
        except Exception as e:
            if "Duplicated key" not in str(e):
                raise
            raise Conflict(f"workspace {workspace_id} exists") from None  # made at the same moment
        self._ledger_of(workspace_id)._db()
        logger.info("Evidence workspace %s (%s) made in the namespace %s", workspace_id, name, namespace)
        return self._workspace(doc)

    create_workspace = on_pool(_create_workspace)

    def _ledger(self, workspace_id: str) -> Ledger:
        """The workspace's ledger; 400 for an id that cannot be one, 404 for one that is not there."""
        check_workspace_id(workspace_id)
        if not self._select(f"SELECT id FROM {WORKSPACES} WHERE id = :id", {"id": workspace_id}):
            raise NotFound("unknown workspace")
        return self._ledger_of(workspace_id)

    ledger = on_pool(_ledger)

    def _all_ledgers(self) -> List[Ledger]:
        return [self._ledger_of(r["id"]) for r in self._workspace_rows()]

    ledgers = on_pool(_all_ledgers)
