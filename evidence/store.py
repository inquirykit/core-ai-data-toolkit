"""
The capture ledgers in the embedded DuckDB database every store shares
(db/engine.py), plus the files on disk (files.py).

Evidence is kept by workspace, the /db workspaces (db/tables.py): the unit
that is isolated, verified, exported and, one day, deleted whole. Each
workspace has its own ledger (``Ledger``): its own five tables (``TABLES``),
its own hash chain and its own files. The tables are evidence_cases,
evidence_captures, evidence_annotations, evidence_todos and evidence_search,
kept for the ledger in every workspace (``TableStore.keep``): browsable in
/admin's Data section with the admin key, read-only there, written only here.
They are made on the workspace's first evidence write (a capture, a case,
todos), never by a read: until then the workspace answers as an empty ledger
(the inbox and nothing else), since a workspace that holds kept tables cannot
be dropped and looking at one must not keep it. For the same reason only the
admin key makes them in a workspace other than the default one, by such a
write; a client key writes in workspaces that hold their ledger already
(api/evidence_routes.py).

A workspace is named by its /db name, everywhere: a request names it so
(``EvidenceStore.ledger``), its records name it so as their workspace, which
``verify`` checks them against, and its files are under
DATA_DIR/evidence/workspaces/<name>/ (the default workspace's are
DATA_DIR/evidence's own). /db refuses two names that differ only by case, so
no two workspaces share a folder, even on a disk that ignores case.

Annotations (``evidence_annotations``: tags and analyst notes added to a
capture after it was taken) and todos (``evidence_todos``: a case's to-do
list) are the mutable tables: a row per annotated capture, keyed by its
capture id, and a row per case with a list, keyed by its case id, apart from
the signed records. They are never part of a record, its hash, the chain or
``verify``.

``evidence_search`` is what the workspace's search reads: a row per capture
with the words of its pages (text.py), its address and title, and its tags
and notes, in one full-text index (db/tables.py), written with the capture
and again with each change of its annotations. It is derived, never part of a
record either.

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

from core.errors import NotFound
from db import SEQUENCE, Database, DocumentStore, Where, on_long_pool, on_pool
from db.tables import (DEFAULT, TableSchema, TableStore, decode_cursor, encode_cursor, schema_of, search_query,
                       table_type)

from .files import CaptureFiles
from .record import (
    DEFAULT_CASE_ID,
    DEFAULT_CASE_NAME,
    GENESIS_HASH,
    VerifiedArtifact,
    build_record,
    canonical,
    new_id,
    now_iso,
    sha256_hex,
)
from .signing import Signer
from .text import capture_text, snippet

logger = logging.getLogger(__name__)

OWNER = "the evidence ledger: records change only through /evidence"
# The start of the ledger's table names, kept for it in every workspace (TableStore.keep).
KEPT = "evidence_"
# Every workspace's ledger (each table has a unique string id besides; a capture's is its capture_id).
TABLES: Dict[str, TableSchema] = {
    "evidence_cases": schema_of("name:string", "created_at:string"),
    "evidence_captures": schema_of(
        "seq:integer!", "capture_id:string!", "case_id:string+", "received_at:string", "record_json:string",
        "record_hash:string", "signature:string", "tsr:string", "tsa_gen_time:string", "tsa_error:string",
        "url:string", "title:string",
    ),
    # Mutable, apart from the records: one row per annotated capture (its id is the capture's).
    "evidence_annotations": schema_of("capture_id:string!", "case_id:string+", "tags:string[]", "notes:object[]",
                                      "updated_at:string"),
    # Mutable too: one row per case with a to-do list (its id is the case's).
    "evidence_todos": schema_of("case_id:string!", "items:object[]", "updated_at:string"),
    # Derived: what the search finds a capture by (its id is the capture's); notes holds its tags and notes as text.
    "evidence_search": schema_of("capture_id:string!", "case_id:string+", "received_at:string+", "seq:integer+",
                                 "url:string~", "title:string~", "text:string~", "notes:string~", "tags:string[]+"),
}
SEARCH = "evidence_search"
# How a search's results go: the best match first (with words to find), else the newest or the oldest first.
SEARCH_ORDER = {"relevance": "_score DESC NULLS FIRST, seq DESC", "newest": "seq DESC", "oldest": "seq"}
CAPTURE_COLUMNS = "seq, capture_id, case_id, received_at, record_json, record_hash, signature, tsr, tsa_gen_time, tsa_error, url, title"


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


def notes_text(tags: List[str], notes: List[Dict[str, Any]]) -> Optional[str]:
    """A capture's notes and tags as the text its search row holds."""
    return "\n".join([*(n["text"] for n in notes), *tags]) or None


class Ledger(DocumentStore):
    """One workspace's cases and captures: its tables, its hash chain and its files.

    Made by ``EvidenceStore.ledger``, once per workspace, so its locks are the only ones. ``workspace`` is the
    workspace's name, which its records name. Its tables are made by its first write (``_make``); whether it holds
    them yet is ``opened``, which the routes ask before a write with a client key, as making them makes the
    workspace one that cannot be dropped (api/evidence_routes.py).
    """

    def __init__(self, database: Database, tables: TableStore, workspace: str, files: CaptureFiles,
                 signer: Signer, version: str):
        super().__init__(database, tables, workspace)
        self._cases, self._captures = table_type(workspace, "evidence_cases"), table_type(workspace, "evidence_captures")
        self._annotations = table_type(workspace, "evidence_annotations")
        self._todos = table_type(workspace, "evidence_todos")
        self._search = table_type(workspace, SEARCH)
        self.files = files
        self.signer = signer
        self.version = version
        # Whether the workspace holds the ledger's tables: made on its first write (``_make``).
        self._made = False
        self._make_lock = threading.Lock()
        self._append_lock = threading.Lock()

    # --------------------------------------------------------------- plumbing
    def _ensure_schema(self, db) -> None:
        """On first use, a ledger the workspace holds (made earlier) is brought up to date; a workspace without one
        stays without until its first write."""
        if any(self._t.has_table(self.workspace, table) for table in TABLES):
            self._make(db)

    def _make(self, db) -> None:
        """The ledger's tables where missing, kept for it (and a schema made earlier brought up to date), and its
        inbox: on its first write, or its first use when the workspace holds it. Once, whoever comes first."""
        with self._make_lock:
            if self._made:
                return
            self._t.ensure_tables(self.workspace, TABLES, owner=OWNER)
            if not self._rows(db, f"SELECT id FROM {self._cases} WHERE id = $id", {"id": DEFAULT_CASE_ID}):
                doc = {"id": DEFAULT_CASE_ID, "name": DEFAULT_CASE_NAME, "created_at": now_iso()}
                self._commit(db, lambda: self._t.write_row(db, self.workspace, "evidence_cases", doc))
            self._made = True

    def _opened(self) -> bool:
        """Whether the workspace holds the ledger yet; before its first write a read answers as an empty one."""
        self._db()
        return self._made

    opened = on_pool(_opened)

    def _search_doc(self, row: Dict[str, Any], text: str, tags: List[str], notes: List[Dict[str, Any]]) -> Dict[str, Any]:
        """The search row of a capture (its row in captures, or the doc about to be), its pages' text and annotations."""
        return {"id": row["capture_id"], "capture_id": row["capture_id"], "case_id": row["case_id"],
                "received_at": row["received_at"], "seq": int(row["seq"]), "url": row.get("url"),
                "title": row.get("title"), "text": text or None, "notes": notes_text(tags, notes), "tags": list(tags)}

    # ------------------------------------------------------------------ cases
    @staticmethod
    def _case(row: Dict[str, Any]) -> Dict[str, Any]:
        return {"id": row["id"], "name": row["name"], "createdAt": row["created_at"]}

    @on_pool
    def list_cases(self) -> List[Dict[str, Any]]:
        if not self._opened():
            return [self._case({"id": DEFAULT_CASE_ID, "name": DEFAULT_CASE_NAME, "created_at": now_iso()})]
        sql = f"SELECT id, name, created_at FROM {self._cases} ORDER BY created_at, id"
        return [self._case(r) for r in self._select(sql)]

    @on_pool
    def create_case(self, name: str) -> Dict[str, Any]:
        db = self._db()
        self._make(db)
        doc = {"id": new_id(), "name": name, "created_at": now_iso()}
        self._commit(db, lambda: self._t.write_row(db, self.workspace, "evidence_cases", doc))
        return self._case(doc)

    def _case_exists(self, case_id: str) -> bool:
        if not self._opened():
            return case_id == DEFAULT_CASE_ID
        return bool(self._select(f"SELECT id FROM {self._cases} WHERE id = $id", {"id": case_id}))

    case_exists = on_pool(_case_exists)

    # --------------------------------------------------------------- captures
    def _head(self, db) -> Dict[str, Any]:
        rows = self._rows(db, f"SELECT seq, record_hash FROM {self._captures} ORDER BY seq DESC LIMIT 1")
        return rows[0] if rows else {"seq": 0, "record_hash": GENESIS_HASH}

    def _append(self, case_id: str, client: Any, artifacts: List[VerifiedArtifact], enrichment: Dict[str, Any]) -> Dict[str, Any]:
        """Build, sign and store the next record in the workspace's chain; its files first, then the row (and its
        search row)."""
        # Outside the lock: a big snapshot's text takes a moment to read.
        text = capture_text((a.name, a.media_type, a.data) for a in artifacts)
        with self._append_lock:
            db = self._db()
            if not self._case_exists(case_id):
                raise NotFound("unknown case")
            self._make(db)
            head = self._head(db)
            seq = int(head["seq"]) + 1
            capture_id = new_id()
            received_at = now_iso()
            record = build_record(
                seq=seq, capture_id=capture_id, workspace_id=self.workspace, case_id=case_id,
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
            searched = self._search_doc(doc, text, [], [])
            try:
                self._commit(db, lambda: (self._t.write_row(db, self.workspace, "evidence_captures", doc),
                                          self._t.write_row(db, self.workspace, SEARCH, searched)))
            except Exception:
                self.files.remove(capture_id)
                raise
            return {
                "captureId": capture_id, "seq": seq, "receivedAt": received_at,
                "recordHash": record_hash, "signature": signature.hex(), "recordJson": data,
            }

    append = on_pool(_append)

    @on_pool
    def set_timestamp(self, capture_id: str, response_der: bytes, gen_time: str) -> None:
        db = self._db()
        self.files.write_timestamp(capture_id, response_der)
        params = {"id": capture_id, "tsr": base64.b64encode(response_der).decode(), "g": gen_time}
        self._commit(db, lambda: db.execute(
            f"UPDATE {self._captures} SET tsr = $tsr, tsa_gen_time = $g, tsa_error = null WHERE capture_id = $id",
            params,
        ))

    @on_pool
    def set_timestamp_error(self, capture_id: str, error: str) -> None:
        db = self._db()
        params = {"id": capture_id, "e": error[:2000]}
        self._commit(db, lambda: db.execute(
            f"UPDATE {self._captures} SET tsa_error = $e WHERE capture_id = $id", params
        ))

    @on_pool
    def pending_timestamps(self) -> List[Dict[str, Any]]:
        if not self._opened():
            return []
        sql = f"SELECT capture_id, record_json, record_hash FROM {self._captures} WHERE tsr IS NULL ORDER BY seq"
        return self._select(sql)

    def _get(self, capture_id: str) -> Optional[Dict[str, Any]]:
        if not self._opened():
            return None
        return self._one(f"SELECT {CAPTURE_COLUMNS} FROM {self._captures} WHERE capture_id = $id", {"id": capture_id})

    get = on_pool(_get)

    @staticmethod
    def summary(row: Dict[str, Any]) -> Dict[str, Any]:
        """A capture as GET /evidence/cases/{id}/captures lists it."""
        return {
            "seq": int(row["seq"]), "captureId": row["capture_id"], "receivedAt": row["received_at"],
            "url": row.get("url"), "title": row.get("title"), "recordHash": row["record_hash"],
            "timestampedAt": row.get("tsa_gen_time"),
        }

    @on_pool
    def list_by_case(self, case_id: str) -> List[Dict[str, Any]]:
        if not self._case_exists(case_id):
            raise NotFound("unknown case")
        if not self._made:
            return []
        sql = (f"SELECT seq, capture_id, received_at, url, title, record_hash, tsa_gen_time FROM {self._captures} "
               f"WHERE case_id = $c ORDER BY seq DESC")
        return [self.summary(r) for r in self._select(sql, {"c": case_id})]

    # ----------------------------------------------------------------- search
    def _find(self, q: Optional[str] = None, case_id: Optional[str] = None, tag: Optional[str] = None,
              since: Optional[str] = None, until: Optional[str] = None, sort: Optional[str] = None,
              limit: int = 50, cursor: Optional[str] = None) -> Dict[str, Any]:
        """The workspace's captures, in every case unless ``case_id`` names one: those holding the words of ``q``
        (schema.search_query: in their pages' text, address, title, tags or notes), tagged ``tag``, received from
        ``since`` up to ``until`` (ISO 8601 prefixes, UTC: ``until`` 2026-10 takes all of October). The best match
        first unless ``sort`` says otherwise ("newest" without ``q``), each with the bit of text that matched.
        ``cursor`` is the previous page's next."""
        db = self._db()
        # Times are stored with a Z after the milliseconds, so a Z after a shorter prefix would sort after them; every
        # time that starts with ``until`` sorts before it with this character after it.
        where = (Where().add("case_id", "=", case_id).contains("tags", tag)
                 .add("received_at", ">=", since and since.rstrip("Z"))
                 .add("received_at", "<", until and until.rstrip("Z") + "\uffff"))
        found = search_query(q) if q else None
        by = (sort or "relevance") if found else "oldest" if sort == "oldest" else "newest"
        # The newest and oldest first go on after the last capture's seq, so captures that come in meanwhile neither
        # repeat a row nor hide one; the best match goes on by its place in the order.
        offset = decode_cursor(cursor) if cursor and by == "relevance" else 0
        after = decode_cursor(cursor, "s") if cursor and by != "relevance" else None
        if not self._made:
            return {"captures": [], "count": 0, "next": None}
        source = self._search
        if found:
            source, terms, params = self._t.searching(self.workspace, SEARCH, found)
            where.terms += terms
            where.params.update(params)
        count = self._rows(db, f"SELECT count(*) AS n FROM {source}{where}", where.params)
        where.add("seq", "<" if by == "newest" else ">", after)
        rows = self._rows(db, f"SELECT capture_id, case_id, seq, tags, title, url, text, notes FROM {source}{where} "
                              f"ORDER BY {SEARCH_ORDER[by]} LIMIT $_limit OFFSET $_skip",
                          {**where.params, "_skip": offset, "_limit": limit + 1})
        more, rows = len(rows) > limit, rows[:limit]
        sql = (f"SELECT seq, capture_id, received_at, url, title, record_hash, tsa_gen_time FROM {self._captures} "
               f"WHERE capture_id = ANY($ids)")
        summaries = {r["capture_id"]: self.summary(r) for r in self._rows(db, sql, {"ids": [r["capture_id"] for r in rows]})}
        terms = found.terms if found else ()
        hits = [{**summaries[r["capture_id"]], "caseId": r["case_id"], "tags": r.get("tags") or [],
                 "snippet": next((cut for part in ("text", "notes", "title", "url") if (cut := snippet(r.get(part), terms))), None)}
                for r in rows if r["capture_id"] in summaries]
        nxt = None
        if more:
            nxt = encode_cursor(offset + limit) if by == "relevance" else encode_cursor(int(rows[-1]["seq"]), "s")
        return {"captures": hits, "count": count[0]["n"] if count else 0, "next": nxt}

    # Its search may rebuild the full-text index first: a long call (db/engine.py, Database).
    find = on_long_pool(_find)

    # ------------------------------------------------------------ annotations
    @staticmethod
    def _annotation(row: Dict[str, Any]) -> Dict[str, Any]:
        notes = [{"id": n["id"], "text": n["text"], "createdAt": n["created_at"], "updatedAt": n.get("updated_at")}
                 for n in row.get("notes") or []]
        return {"captureId": row["capture_id"], "tags": list(row.get("tags") or []), "notes": notes,
                "updatedAt": row["updated_at"]}

    @on_pool
    def annotations_by_case(self, case_id: str) -> List[Dict[str, Any]]:
        """The annotations of the case's captures that have any, oldest capture first."""
        if not self._case_exists(case_id):
            raise NotFound("unknown case")
        if not self._made:
            return []
        sql = f"SELECT capture_id, tags, notes, updated_at FROM {self._annotations} WHERE case_id = $c"
        rows = self._select(sql, {"c": case_id})
        order = {r["capture_id"]: int(r["seq"]) for r in self._select(
            f"SELECT seq, capture_id FROM {self._captures} WHERE case_id = $c", {"c": case_id})}
        rows.sort(key=lambda r: order.get(r["capture_id"], 0))
        return [self._annotation(r) for r in rows]

    @on_pool
    def get_annotation(self, capture_id: str) -> Optional[Dict[str, Any]]:
        if not self._opened():
            return None
        sql = f"SELECT capture_id, tags, notes, updated_at FROM {self._annotations} WHERE capture_id = $id"
        rows = self._select(sql, {"id": capture_id})
        return self._annotation(rows[0]) if rows else None

    @on_pool
    def annotate(self, capture_id: str, tags: Optional[List[str]], notes: Optional[List[Dict[str, Any]]]) -> Dict[str, Any]:
        """Replace the capture's tags, its notes, or both (None keeps a list); 404 for an unknown capture.

        A note keeps its id and createdAt when ``notes`` names a known id; its updatedAt is set when its text
        changed. A note without an id (or with an unknown one) is new.
        """
        db = self._db()
        found = self._made and self._rows(db, f"SELECT case_id FROM {self._captures} WHERE capture_id = $id",
                                          {"id": capture_id})
        if not found:
            raise NotFound("unknown capture")

        def write() -> Dict[str, Any]:
            # Read in the transaction: another change to the annotation meanwhile makes this run again (_commit) on
            # what that one wrote, so a call that changes only the tags never puts back notes it read before.
            rows = self._rows(db, f"SELECT tags, notes FROM {self._annotations} WHERE capture_id = $id",
                              {"id": capture_id})
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
            doc = {"id": capture_id, "capture_id": capture_id, "case_id": found[0]["case_id"], "tags": kept,
                   "notes": merged, "updated_at": now}
            self._t.write_row(db, self.workspace, "evidence_annotations", doc)
            # A new write number, so the search reads the row as it is now until the index is built again.
            db.execute(f"UPDATE {self._search} SET tags = $tags, notes = $notes, _seq = nextval('{SEQUENCE}') "
                       "WHERE capture_id = $id", {"tags": kept, "notes": notes_text(kept, merged), "id": capture_id})
            return doc

        return self._annotation(self._commit(db, write))

    # ------------------------------------------------------------------ todos
    @staticmethod
    def _todo_list(case_id: str, row: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        row = row or {}
        todos = [{"id": t["id"], "text": t["text"], "done": bool(t.get("done")), "createdAt": t["created_at"],
                  "updatedAt": t.get("updated_at"), "doneAt": t.get("done_at")} for t in row.get("items") or []]
        return {"caseId": case_id, "todos": todos, "updatedAt": row.get("updated_at")}

    @on_pool
    def todos_of(self, case_id: str) -> Dict[str, Any]:
        """The case's to-do list, empty when it never had one; 404 for an unknown case."""
        if not self._case_exists(case_id):
            raise NotFound("unknown case")
        if not self._made:
            return self._todo_list(case_id, None)
        return self._todo_list(case_id, self._one(f"SELECT items, updated_at FROM {self._todos} WHERE case_id = $c",
                                                  {"c": case_id}))

    @on_pool
    def set_todos(self, case_id: str, todos: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Replace the case's to-do list with ``todos``, in their order; 404 for an unknown case.

        A todo keeps its id and createdAt when ``todos`` names a known id (once: a repeat is new); its updatedAt is
        set when its text changed, its doneAt when it was checked, cleared when unchecked. A todo without an id (or
        with an unknown one) is new.
        """
        db = self._db()
        if not self._case_exists(case_id):
            raise NotFound("unknown case")
        self._make(db)

        def write() -> Dict[str, Any]:
            # Read in the transaction, as annotate does: a run again reads what a change meanwhile wrote.
            rows = self._rows(db, f"SELECT items FROM {self._todos} WHERE case_id = $c", {"c": case_id})
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
            self._t.write_row(db, self.workspace, "evidence_todos", doc)
            return doc

        return self._todo_list(case_id, self._commit(db, write))

    def _all_records(self) -> List[Dict[str, Any]]:
        if not self._opened():
            return []
        return self._select(f"SELECT {CAPTURE_COLUMNS} FROM {self._captures} ORDER BY seq")

    @staticmethod
    def record_of(row: Dict[str, Any]) -> Any:
        return json.loads(row["record_json"])

    @staticmethod
    def tsr_of(row: Dict[str, Any]) -> Optional[bytes]:
        raw = row.get("tsr")
        return base64.b64decode(raw) if raw else None


class EvidenceStore(DocumentStore):
    """The evidence of every workspace, each kept by its ``Ledger``; see the module docstring."""

    def __init__(self, database: Database, tables: TableStore, files: CaptureFiles, signer: Signer,
                 version: str):
        """``files`` is the default workspace's; another one's are under ``files.root/workspaces/<its name>``."""
        super().__init__(database, tables)
        # The ledger's tables, in every workspace, are written by it alone.
        tables.keep(KEPT, OWNER)
        self.files = files
        self.signer = signer
        self.version = version
        self._ledgers: Dict[str, Ledger] = {}
        self._ledgers_lock = threading.Lock()

    # --------------------------------------------------------------- plumbing
    def _ledger_of(self, workspace: str) -> Ledger:
        """The workspace's one Ledger, made on first use (whether the workspace exists is the caller's to know)."""
        with self._ledgers_lock:
            ledger = self._ledgers.get(workspace)
            if ledger is None:
                files = self.files if workspace == DEFAULT else CaptureFiles(self.files.root / "workspaces" / workspace)
                ledger = Ledger(self.database, self._t, workspace, files, self.signer, self.version)
                self._ledgers[workspace] = ledger
            return ledger

    # ------------------------------------------------------------- workspaces
    def _records(self) -> List[Dict[str, Any]]:
        """The workspaces (not the service's own), the default one first, then by when they were made."""
        self._db()
        return sorted(self._t.records(), key=lambda r: (r["name"] != DEFAULT, r.get("created") or 0, r["name"]))

    def _ledger(self, workspace: str) -> Ledger:
        """The ledger of the workspace of that name; 404 for none (the service's own are none)."""
        self._db()
        if workspace not in self._t.names():
            raise NotFound("unknown workspace")
        return self._ledger_of(workspace)

    ledger = on_pool(_ledger)

    def _all_ledgers(self) -> List[Ledger]:
        """The ledgers the workspaces hold (made on a first write)."""
        return [self._ledger_of(r["name"]) for r in self._records() if self._t.has_table(r["name"], "evidence_captures")]

    ledgers = on_pool(_all_ledgers)
