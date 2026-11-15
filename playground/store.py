"""
The playground's saved conversations and prompt presets (the routes are api/playground_routes.py), and the eval
example a saved conversation becomes.

They live in the ``playground`` workspace of the /db tables (db/tables.py), so they survive the browser and are
browsable in the dashboard's Data section, admin-only and read-only there, so a row written through /db cannot
break the lists: the table store takes writes there only from this store (``owner``). A row is what the page
sent, plus its id and when it was made and last replaced.
"""

import time
import uuid
from typing import Any, Dict, List, Optional

from core.errors import NotFound, Refused
from db import Database, DocumentStore
from db.tables import TableSchema, TableStore
from models.chat import ChatMessage, build_prompt

WORKSPACE = "playground"
CONVERSATIONS = "conversations"
PRESETS = "presets"
TABLES = {CONVERSATIONS: TableSchema(), PRESETS: TableSchema()}
# The most rows of a table a list reads.
MAX_SAVED = 1000


def _check_id(value: str) -> str:
    if not value or len(value) > 64 or "/" in value:
        raise Refused("bad id")
    return value


def _summary(row: Dict[str, Any]) -> Dict[str, Any]:
    """A saved conversation as the list shows it: its title and models, and how many turns the first column has."""
    columns = row.get("columns") or []
    first = columns[0]["log"] if columns else []
    return {
        "id": row["id"], "title": row["title"], "created": row["created"], "updated": row["updated"],
        "models": [c.get("model") or "" for c in columns],
        "turns": sum(1 for e in first if e.get("role") == "user"),
    }


class PlaygroundStore(DocumentStore):
    WORKSPACE = WORKSPACE
    OWNER = "saved chats and presets: change them in the playground"
    TABLES = TABLES

    def __init__(self, database: Database, tables: TableStore):
        super().__init__(database, tables)

    async def _rows(self, table: str) -> List[Dict[str, Any]]:
        try:
            rows, _ = await self._t.list(self.workspace, table, limit=MAX_SAVED)
        except NotFound:
            return []
        return rows

    async def conversations(self) -> List[Dict[str, Any]]:
        """Saved conversations, most recently updated first, summed up (``get`` one for its turns)."""
        rows = await self._rows(CONVERSATIONS)
        rows.sort(key=lambda r: r.get("updated") or 0, reverse=True)
        return [_summary(r) for r in rows]

    async def presets(self) -> List[Dict[str, Any]]:
        """Saved prompt presets, by name."""
        rows = await self._rows(PRESETS)
        rows.sort(key=lambda r: (r.get("name") or "").lower())
        return rows

    async def get(self, table: str, row_id: str) -> Dict[str, Any]:
        try:
            return await self._t.get(self.workspace, table, _check_id(row_id))
        except NotFound:
            raise NotFound(f"No saved {table[:-1]} {row_id!r}")

    async def create(self, table: str, fields: Dict[str, Any]) -> Dict[str, Any]:
        now = time.time()
        row = {"id": uuid.uuid4().hex[:16], "created": now, "updated": now, **fields}
        await self._t.upsert(self.workspace, table, row, owner=self.OWNER)
        return row

    async def replace(self, table: str, row_id: str, fields: Dict[str, Any]) -> Dict[str, Any]:
        """Replace a saved row's fields with ``fields``; 404 if it was deleted."""
        current = await self.get(table, row_id)
        row = {**current, **fields, "id": row_id, "updated": time.time()}
        await self._t.upsert(self.workspace, table, row, owner=self.OWNER)
        return row

    async def delete(self, table: str, row_id: str) -> None:
        await self._t.delete(self.workspace, table, [_check_id(row_id)], owner=self.OWNER)


def example_from(conversation: Dict[str, Any], column: int, reference: Optional[str]) -> Dict[str, Any]:
    """An eval example from one column: the chat up to its last reply as the input, that reply as the reference.

    The input holds the chat as ``messages`` and as the plain transcript
    (``text``), the two forms the text-generation task takes, so any model
    can run it. Without a reply, the whole chat is the input and the
    reference is what the caller gave.
    """
    columns = conversation.get("columns") or []
    if not 0 <= column < len(columns):
        raise Refused(f"column must be 0 to {len(columns) - 1}")
    log = [e for e in columns[column].get("log") or [] if e.get("content")]
    replies = [i for i, e in enumerate(log) if e.get("role") == "assistant"]
    turns, default_reference = (log[: replies[-1]], log[replies[-1]]["content"]) if replies else (log, None)
    system = (conversation.get("settings") or {}).get("system") or ""
    messages = ([{"role": "system", "content": system}] if system.strip() else []) \
        + [{"role": e["role"], "content": e["content"]} for e in turns]
    if not messages or messages[-1]["role"] != "user":
        raise Refused("The column needs a user turn for the model to answer")
    return {
        "input": {"text": build_prompt([ChatMessage(**m) for m in messages]), "messages": messages},
        "reference": reference if reference is not None else default_reference,
        "task": "text-generation",
        "tags": [t for t in ("playground", columns[column].get("model") or "") if t][:16],
    }
