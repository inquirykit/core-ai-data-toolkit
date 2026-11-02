"""
Document collections stored in the embedded ArcadeDB database (engine.py).

A collection is an ArcadeDB document type ``tk_<name>`` with a fixed ``id``
(unique), ``text`` and ``embedding`` (an LSM_VECTOR index, cosine), plus the
fields its creator declared, each indexed so filters on it stay fast. The
type ``tk_collection`` records each collection's embedding model and fields.

Search embeds nothing itself: the caller passes the query vector (see
api/collection_routes.py, which embeds with the collection's model). A
client's ``where`` is ArcadeDB SQL run through the read-only query path, so it
can filter but never write; it selects which records the vector search may
return.
"""

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from core.errors import NotFound, Refused
from .engine import Database, DocumentStore, on_pool

# Declarable field types, with the Python values each accepts.
FIELD_TYPES: Dict[str, tuple] = {
    "STRING": (str,),
    "INTEGER": (int,),
    "LONG": (int,),
    "FLOAT": (int, float),
    "DOUBLE": (int, float),
    "BOOLEAN": (bool,),
    "LIST": (list,),
}
NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,62}$")
# Fields every collection has, and the column search adds.
BUILT_IN = ("id", "text", "embedding", "distance")
META = "tk_collection"


@dataclass(frozen=True)
class Collection:
    name: str
    model: str
    dimensions: int
    fields: Dict[str, str] = field(default_factory=dict)
    quantization: str = "NONE"

    @property
    def type_name(self) -> str:
        return f"tk_{self.name}"

    @property
    def columns(self) -> str:
        return ", ".join(["id", "text", *self.fields])

    def check_values(self, values: Dict[str, Any]) -> None:
        """Refused unless every value is a declared field of the right type."""
        for name, value in values.items():
            kind = self.fields.get(name)
            if kind is None:
                raise Refused(f"{name!r} is not a field of {self.name}; its fields: {', '.join(self.fields) or 'none'}")
            if value is None:
                continue
            accepted = FIELD_TYPES[kind]
            if not isinstance(value, accepted) or (kind != "BOOLEAN" and isinstance(value, bool)):
                raise Refused(f"{name!r} must be {kind}, not {type(value).__name__}")


class CollectionStore(DocumentStore):
    SCHEMA = {META: {"props": {"name": "STRING"}, "indexes": [("name", True)]}}

    def __init__(self, database: Database):
        super().__init__(database)
        self._collections: Optional[Dict[str, Collection]] = None

    def _user_query(self, sql: str, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Run SQL holding a client's ``where``: read-only, and its errors are the client's."""
        try:
            return self._select(sql, params)
        except Exception as e:
            if type(e).__name__ != "ArcadeDBError":
                raise
            raise Refused(f"The query failed: {str(e).split(': ', 1)[-1][:500]}") from None

    # ----------------------------------------------------------- collections
    def _load_collections(self) -> Dict[str, Collection]:
        if self._collections is None:
            rows = self._select(f"SELECT name, model, dimensions, fields, quantization FROM {META}")
            self._collections = {
                r["name"]: Collection(r["name"], r["model"], r["dimensions"], json.loads(r["fields"] or "{}"),
                                      r.get("quantization") or "NONE")
                for r in rows
            }
        return self._collections

    async def collections(self) -> Dict[str, Collection]:
        return dict(await self._run(self._load_collections))

    async def get(self, name: str) -> Collection:
        collection = (await self.collections()).get(name)
        if collection is None:
            raise NotFound(f"No collection named {name!r}")
        return collection

    @on_pool
    def create(self, c: Collection) -> None:
        db = self._db()
        if c.name in self._load_collections():
            raise Refused(f"A collection named {c.name!r} already exists")
        t = c.type_name
        quantization = f", quantization: '{c.quantization}'" if c.quantization != "NONE" else ""
        statements = [
            f"CREATE DOCUMENT TYPE {t}",
            f"CREATE PROPERTY {t}.id STRING",
            f"CREATE PROPERTY {t}.text STRING",
            f"CREATE PROPERTY {t}.embedding ARRAY_OF_FLOATS",
            *(f"CREATE PROPERTY {t}.{name} {kind}" for name, kind in c.fields.items()),
            f"CREATE INDEX ON {t} (id) UNIQUE",
            # Declared fields are there to filter on: index each.
            *(f"CREATE INDEX ON {t} ({name}) NOTUNIQUE" for name, kind in c.fields.items() if kind != "LIST"),
            f"CREATE INDEX ON {t} (embedding) LSM_VECTOR METADATA "
            f"{{dimensions: {c.dimensions}, similarity: 'COSINE'{quantization}}}",
        ]
        try:
            for statement in statements:
                db.command("sql", statement)
            # Schema changes need no transaction; records do.
            with db.transaction():
                db.command("sql", f"INSERT INTO {META} CONTENT {{name: :name, model: :model, dimensions: :dimensions, "
                                  f"fields: :fields, quantization: :quantization}}",
                           {"name": c.name, "model": c.model, "dimensions": c.dimensions,
                            "fields": json.dumps(c.fields), "quantization": c.quantization})
        except Exception:
            # Leave nothing half made.
            if self.database.has_type(t):
                db.command("sql", f"DROP TYPE {t} UNSAFE")
            raise
        self._collections = None

    @on_pool
    def drop(self, name: str) -> None:
        db = self._db()
        collection = self._load_collections().get(name)
        if collection is None:
            raise NotFound(f"No collection named {name!r}")
        db.command("sql", f"DROP TYPE {collection.type_name} UNSAFE")
        with db.transaction():
            db.command("sql", f"DELETE FROM {META} WHERE name = :name", {"name": name})
        self._collections = None

    @on_pool
    def count(self, c: Collection) -> int:
        rows = self._select(f"SELECT count(*) AS n FROM {c.type_name}")
        return rows[0]["n"] if rows else 0

    # ----------------------------------------------------------------- items
    @on_pool
    def upsert(self, c: Collection, items: List[Dict[str, Any]]) -> None:
        """Insert ``items`` (each with id, text, embedding and declared fields), replacing any with the same id."""
        db = self._db()
        with db.transaction():
            for item in items:
                names = ["id", "text", "embedding", *c.fields]
                content = ", ".join(f"{n}: :{n}" for n in names)
                params = {n: item.get(n) for n in names}
                db.command("sql", f"UPDATE {c.type_name} CONTENT {{{content}}} UPSERT WHERE id = :id", params)

    @on_pool
    def item(self, c: Collection, item_id: str) -> Dict[str, Any]:
        rows = self._select(f"SELECT {c.columns} FROM {c.type_name} WHERE id = :id", {"id": item_id})
        if not rows:
            raise NotFound(f"No item {item_id!r} in {c.name}")
        return rows[0]

    @on_pool
    def delete(self, c: Collection, item_id: str) -> None:
        db = self._db()
        with db.transaction():
            rows = self.database.rows(db.command("sql", f"DELETE FROM {c.type_name} WHERE id = :id", {"id": item_id}))
        if not rows or not rows[0].get("count"):
            raise NotFound(f"No item {item_id!r} in {c.name}")

    # ---------------------------------------------------------------- search
    @staticmethod
    def _checked(where: Optional[str], params: Dict[str, Any]) -> None:
        if where is not None and ";" in where:
            raise Refused("where is one SQL condition: no ';'")
        reserved = [p for p in params if p.startswith("_tk")]
        if reserved:
            raise Refused(f"Parameter names starting with _tk are reserved: {', '.join(reserved)}")

    async def search(
        self, collection: Collection, vector: List[float], k: int, where: Optional[str] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """The ``k`` nearest items to ``vector``, nearest first, among those ``where`` selects."""
        params = dict(params or {})
        self._checked(where, params)
        t = collection.type_name
        options = f", {{filter: (SELECT @rid FROM {t} WHERE {where})}}" if where else ""
        sql = (f"SELECT {collection.columns}, distance FROM "
               f"(SELECT expand(vector.neighbors('{t}[embedding]', :_tk_vector, :_tk_k{options})))")
        params.update(_tk_vector=vector, _tk_k=k)
        return await self._run(self._user_query, sql, params)

    async def query(
        self, collection: Collection, where: Optional[str] = None, params: Optional[Dict[str, Any]] = None,
        limit: int = 100, skip: int = 0,
    ) -> List[Dict[str, Any]]:
        """Items ``where`` selects, in insertion order, without vector search."""
        params = dict(params or {})
        self._checked(where, params)
        condition = f" WHERE {where}" if where else ""
        sql = f"SELECT {collection.columns} FROM {collection.type_name}{condition} SKIP :_tk_skip LIMIT :_tk_limit"
        params.update(_tk_skip=skip, _tk_limit=limit)
        return await self._run(self._user_query, sql, params)
