"""
What a /db table is, apart from the database: names, ids, schemas (fields
with a type, maybe required, indexed or unique), rows checked against them,
edge references, filters, cursors and the observed shape of rows. Every check
raises TableError (or NotFound, Conflict), which the routes answer with its
status. tables.py keeps them in ArcadeDB; graph.py adds edges.
"""

import base64
import binascii
import json
import math
import re
from collections import Counter
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Dict, Optional, Sequence, Tuple

import errors
from .engine import StoreError

NAMESPACES = "tk__namespace"
TABLES = "tk__table"
PREFIX = "ns_"
EDGE_PREFIX = "ne_"
SEP = "__"
NAME = re.compile(r"^[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)*$")
MAX_NAME = 48
FIELD = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
# Declared fields: also filterable, so not starting with _ (the API's parameters do).
DECLARED_FIELD = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,127}$")
MAX_ID = 256
MAX_FIELDS = 256
LONG = 2 ** 63

TYPES = ("string", "integer", "number", "boolean", "object", "any")
# The ArcadeDB property type of each type that can be indexed.
INDEXABLE: Dict[str, str] = {"string": "STRING", "integer": "LONG", "number": "DOUBLE", "boolean": "BOOLEAN"}
FROM_ARCADE = {"STRING": "string", "LONG": "integer", "INTEGER": "integer", "SHORT": "integer",
                "DOUBLE": "number", "FLOAT": "number", "BOOLEAN": "boolean"}

DIRECTIONS = ("out", "in", "both")
MAX_DEPTH = 10
# A graph view (``graph()``): rows and the edges among them, at most.
GRAPH_MAX_NODES = 5000
GRAPH_MAX_EDGES = 20000
QUERY_LANGUAGES = ("sql", "cypher")


class TableError(StoreError, ValueError):
    """A request the store refuses (a client error)."""


class NotFound(TableError, errors.NotFound):
    """No such namespace, table or row; an errors.NotFound too, so one ``except`` catches every store's."""

    status = 404  # before StoreError's 400 in the bases' order


class Conflict(TableError, errors.Conflict):
    """The namespace, table or row already exists, or a unique value is taken."""

    status = 409


def kind_of(value: Any) -> str:
    """A value's JSON type, as schemas and the observed fields name it."""
    if value is None:
        return "null"
    return {bool: "boolean", int: "integer", float: "number", str: "string",
            dict: "object", list: "list"}.get(type(value), "object")


def _fits(kind: str, value: Any) -> bool:
    if kind == "any":
        return True
    if isinstance(value, bool):
        return kind == "boolean"
    if kind == "string":
        return isinstance(value, str)
    if kind == "integer":
        return isinstance(value, int) and -LONG <= value < LONG
    if kind == "number":
        return isinstance(value, (int, float)) and math.isfinite(value)
    if kind == "object":
        return isinstance(value, dict)
    return False


def _json(raw: str) -> Any:
    try:
        return json.loads(raw)
    except ValueError:
        return None


@dataclass(frozen=True)
class Field:
    name: str
    type: str = "any"
    list: bool = False
    required: bool = False
    indexed: bool = False
    unique: bool = False

    @property
    def index_key(self) -> Optional[Tuple[str, bool, bool]]:
        """What its index depends on; None when it has none."""
        return (self.type, self.list, self.unique) if self.indexed else None

    def check(self, row: Dict[str, Any]) -> None:
        """TableError unless the row's value fits: the type (or a list of it), or absent/null unless required."""
        value = row.get(self.name)
        if value is None:
            if self.required:
                raise TableError(f"{self.name!r} is required")
            return
        what = f"a list of {self.type}" if self.list else self.type
        if self.list:
            if not isinstance(value, list):
                raise TableError(f"{self.name!r} is {what}, not {kind_of(value)}")
            for item in value:
                if (item is None and self.type != "any") or not _fits(self.type, item):
                    raise TableError(f"{self.name!r} is {what}: {item!r} is {kind_of(item)}")
        elif not _fits(self.type, value):
            raise TableError(f"{self.name!r} is {what}: {value!r} is {kind_of(value)}")

    def parse(self, raw: str) -> Any:
        """A filter's text as this field's type (an item's, for a list)."""
        if raw == "null":
            return None
        if self.type == "string":
            quoted = _json(raw)
            return quoted if isinstance(quoted, str) else raw
        if self.type in ("integer", "number", "boolean"):
            value = _json(raw)
            if value is None or isinstance(value, str) or not _fits(self.type, value):
                raise TableError(f"Filter {self.name}={raw}: the field is {self.type}")
            return value
        return filter_value(raw)


@dataclass(frozen=True)
class TableSchema:
    fields: Dict[str, Field] = field(default_factory=dict)
    strict: bool = False
    # A vertex type, whose rows can be linked by edges; set when the table is made.
    graph: bool = False

    def check(self, row: Dict[str, Any]) -> None:
        for f in self.fields.values():
            f.check(row)
        if self.strict:
            extra = [k for k in row if k != "id" and k not in self.fields]
            if extra:
                raise TableError(f"Not fields of this table (its schema is strict): {', '.join(extra)}")

    def to_dict(self) -> Dict[str, Any]:
        return {"strict": self.strict, "graph": self.graph,
                "fields": [asdict(f) for _, f in sorted(self.fields.items())]}


EMPTY = TableSchema()


def check_field_spec(f: Field) -> Field:
    if f.name == "id":
        raise TableError("id is built in: a string, always indexed and unique")
    if not DECLARED_FIELD.match(f.name):
        raise TableError(f"Bad field name {f.name!r}: letters, digits and underscores, starting with a letter")
    if f.type not in TYPES:
        raise TableError(f"{f.name}: type is one of {', '.join(TYPES)}")
    if f.unique:
        f = replace(f, indexed=True)
    if f.indexed and f.type not in INDEXABLE:
        raise TableError(f"{f.name}: only {', '.join(INDEXABLE)} fields can be indexed")
    if f.list and f.unique:
        raise TableError(f"{f.name}: a list field cannot be unique")
    return f


def make_schema(fields: Sequence[Field], strict: bool = False, graph: bool = False) -> TableSchema:
    checked = [check_field_spec(f) for f in fields]
    if len({f.name for f in checked}) != len(checked):
        raise TableError("Each field is declared once")
    if len(checked) > MAX_FIELDS:
        raise TableError(f"At most {MAX_FIELDS} fields")
    return TableSchema({f.name: f for f in checked}, strict, graph)


def schema_of(*specs: str) -> TableSchema:
    """A schema from "name:type" specs, for the service's own tables: "+" after the type indexes the field,
    "!" makes it unique, "[]" makes it a list of the type ("tags:string[]", "run_id:string+", "seq:integer!")."""
    fields = []
    for spec in specs:
        name, kind = spec.split(":")
        fields.append(Field(name, kind.rstrip("+![]"), list="[]" in kind, indexed="+" in kind or "!" in kind,
                            unique="!" in kind))
    return make_schema(fields)


def check_name(kind: str, name: str) -> str:
    if len(name) > MAX_NAME or not NAME.match(name):
        raise TableError(f"Bad {kind} name {name!r}: up to {MAX_NAME} letters, digits and single underscores "
                         "inside, starting with a letter")
    return name


def check_field(name: str) -> str:
    if not FIELD.match(name):
        raise TableError(f"Bad field name {name!r}: letters, digits and underscores, not starting with a digit")
    return name


def check_id(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_ID or value.startswith("_") or "/" in value:
        raise TableError(f"Bad id {value!r}: a string of 1 to {MAX_ID} characters, without '/', not starting with '_'")
    return value


def check_row(row: Any, schema: TableSchema = EMPTY) -> Dict[str, Any]:
    if not isinstance(row, dict):
        raise TableError("A row is a JSON object")
    bad = [k for k in row if k.startswith("@")]
    if bad:
        raise TableError(f"Field names starting with @ are reserved: {', '.join(bad)}")
    check_id(row.get("id"))
    schema.check(row)
    return row


def check_ref(ref: Any) -> Tuple[str, str]:
    """``table/id`` as (table, id)."""
    if not isinstance(ref, str) or ref.count("/") != 1:
        raise TableError(f"Bad row reference {ref!r}: 'table/id'")
    table, row_id = ref.split("/")
    return check_name("table", table), check_id(row_id)


@dataclass(frozen=True)
class EdgeRef:
    type: str
    from_table: str
    from_id: str
    to_table: str
    to_id: str


def check_edge_ref(edge: Any) -> EdgeRef:
    if not isinstance(edge, dict):
        raise TableError("An edge is a JSON object with type, from and to")
    for key in ("type", "from", "to"):
        if key not in edge:
            raise TableError(f"An edge needs {key!r}")
    if not isinstance(edge["type"], str):
        raise TableError("An edge's type is a string")
    return EdgeRef(check_name("edge type", edge["type"]), *check_ref(edge["from"]), *check_ref(edge["to"]))


def check_edge(edge: Any) -> Tuple[EdgeRef, Dict[str, Any]]:
    """An edge body as its reference and its properties (everything but type, from and to)."""
    ref = check_edge_ref(edge)
    props = {k: v for k, v in edge.items() if k not in ("type", "from", "to")}
    bad = [k for k in props if k.startswith("@")]
    if bad:
        raise TableError(f"Field names starting with @ are reserved: {', '.join(bad)}")
    return ref, props


def check_direction(direction: str) -> str:
    if direction not in DIRECTIONS:
        raise TableError(f"_direction is one of {', '.join(DIRECTIONS)}")
    return direction


def filter_value(raw: str) -> Any:
    """A filter on an undeclared field: JSON numbers, true, false and null as such ("30" quoted is text), else the text."""
    value = _json(raw)
    if value is None and raw != "null":
        return raw
    if isinstance(value, float) and not math.isfinite(value):
        return raw
    return value if isinstance(value, (str, int, float, bool)) or value is None else raw


def merge_patch(target: Any, patch: Any) -> Any:
    """``patch`` applied to ``target`` by JSON Merge Patch (RFC 7396): null removes a field."""
    if not isinstance(patch, dict):
        return patch
    result = dict(target) if isinstance(target, dict) else {}
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        else:
            result[key] = merge_patch(result.get(key), value)
    return result


def encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(json.dumps({"o": offset}).encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> int:
    try:
        offset = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))["o"]
    except (binascii.Error, ValueError, KeyError, TypeError):
        raise TableError("Bad _cursor: pass the 'next' value of the previous page") from None
    if not isinstance(offset, int) or offset < 0:
        raise TableError("Bad _cursor: pass the 'next' value of the previous page")
    return offset


def observe(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Which fields the rows hold, how often, with which types: a list as ``list<item type>`` (``list<any>``
    when its items differ, ``list`` when it is empty)."""
    present: Counter = Counter()
    types: Dict[str, Counter] = {}
    for row in rows:
        for name, value in row.items():
            if name == "id":
                continue
            present[name] += 1
            kind = kind_of(value)
            if kind == "list":
                items = {kind_of(v) for v in value if v is not None}
                kind = "list" if not items else f"list<{items.pop() if len(items) == 1 else 'any'}>"
            types.setdefault(name, Counter())[kind] += 1
    return {"sampled": len(rows), "fields": [
        {"name": n, "present": present[n], "types": dict(types[n].most_common())} for n in sorted(present)]}


def row_type_name(namespace: str, table: str) -> str:
    return f"{PREFIX}{namespace}{SEP}{table}"


def row_type(namespace: str, table: str) -> str:
    return f"`{row_type_name(namespace, table)}`"


# The ArcadeDB type of a table, for stores that keep their data in /db tables (evals/, evidence/).
table_type = row_type


def edge_type_name_of(namespace: str, edge_type: str) -> str:
    return f"{EDGE_PREFIX}{namespace}{SEP}{edge_type}"


def edge_type_of(namespace: str, edge_type: str) -> str:
    return f"`{edge_type_name_of(namespace, edge_type)}`"


def split_type(name: str, prefix: str) -> Optional[Tuple[str, str]]:
    """(namespace, table or edge type) of an ArcadeDB type name with this prefix; None for other types."""
    if name.startswith(prefix) and SEP in name[len(prefix):]:
        namespace, rest = name[len(prefix):].split(SEP, 1)
        return namespace, rest
    return None


def ordered(row: Dict[str, Any]) -> Dict[str, Any]:
    """The row with ``id`` first, as clients expect to see it."""
    return {"id": row.get("id"), **{k: v for k, v in row.items() if k != "id"}}

