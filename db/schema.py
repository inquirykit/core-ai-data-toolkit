"""
What a /db table is, apart from the database: names, ids, schemas (fields
with a type, maybe required, indexed, unique or full-text), rows checked
against them, edge references, filters, searches, cursors and the observed
shape of rows. Every check
raises TableError (or NotFound, Conflict), which the routes answer with its
status. tables.py keeps them in DuckDB; graph.py adds edges.

A *ref* field holds the id of a row of a table of the same workspace
(``to``), or with ``list`` a list of ids: a string column, indexed unless a
list. refs.py checks the ids and follows them.

A table may have one *vector* field: a list of ``dimensions`` numbers per
row (or null), compared by ``similarity``, searched for the rows nearest to
a vector (exactly: every row's distance is computed). With a
``model`` it is embedded from the string field ``from`` on every write that
sets that text (tables.py), so rows never send it. Such a model may want a
text put before what it embeds: ``document_prompt`` before a row's text,
``query_prompt`` before a search's (vectors.py fills them from the model
when the field is made without them). Its options are set when it is made
and never change, but for the query prompt, which only searches use:
removing the field and adding it again is how (its vectors go with it). It
is never a list, indexed, unique, full-text or a filter.

A *bucket* is a table of files (``bucket`` in its schema, chosen when it is
made and never changed): a plain table whose fields are fixed (``BUCKET``),
a row per file, its bytes on disk (buckets/, which writes its rows). It may
name a *remote*, a place outside the service its files are copied to
(buckets/naming.py); ``copied`` says when each file's were.
"""

import base64
import binascii
import json
import math
import re
import unicodedata
from collections import Counter
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Dict, List, Optional, Sequence, Tuple

from core.errors import Conflict, NotFound, Refused  # noqa: F401 (the tables' NotFound and Conflict, raised by tables.py)

from .engine import quote

WORKSPACES = "main.tk_workspace"
TABLES = "main.tk_table"
# The table of a workspace's edges, every edge type's (graph.py): a name no table can take, as theirs start with a
# letter.
EDGES = "_edges"
NAME = re.compile(r"^[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)*$")
# Workspace names DuckDB holds already (its own schemas; the database itself, named after its file in DATA_DIR,
# toolkit.duckdb) or keeps for the full-text indexes' schemas (fts_<workspace>_<table>), compared ignoring case as
# DuckDB compares names.
RESERVED = ("main", "temp", "system", "memory", "information_schema", "pg_catalog", "toolkit")
RESERVED_PREFIX = "fts_"
MAX_NAME = 48
FIELD = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
# Declared fields: also filterable, so not starting with _ (the API's parameters do).
DECLARED_FIELD = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,127}$")
MAX_ID = 256
MAX_FIELDS = 256
LONG = 2 ** 63

VECTOR = "vector"
# A field holding the id of a row of another table of the workspace (``to``), or a list of them: refs.py.
REF = "ref"
TYPES = ("string", "integer", "number", "boolean", "object", "any", REF, VECTOR)
# What deleting a row does to the rows whose ref field holds its id: refuse the delete while there are any, take the
# id out of the field (null; out of a list), or delete them too.
ON_DELETE = ("restrict", "unset", "cascade")
# The Field attributes only a ref field has.
REF_OPTIONS = ("to", "on_delete")
# A vector field's options: how a search compares two vectors, and how it stores them (as floats: NONE, the one way).
SIMILARITIES = ("COSINE", "DOT_PRODUCT", "EUCLIDEAN")
QUANTIZATIONS = ("NONE",)
MAX_DIMENSIONS = 4096
# The Field attributes only a vector field has.
VECTOR_OPTIONS = ("dimensions", "similarity", "quantization", "model", "from_", "query_prompt", "document_prompt")
# The longest prompt a vector field puts before a text, in characters.
MAX_PROMPT = 1000
# The nearest rows a vector search returns at most.
MAX_K = 100
# The DuckDB column type of each field type; an object or any value is JSON, a vector FLOAT[dimensions].
COLUMN: Dict[str, str] = {"string": "VARCHAR", "integer": "BIGINT", "number": "DOUBLE", "boolean": "BOOLEAN",
                          "object": "JSON", "any": "JSON", REF: "VARCHAR"}
# The types that can be indexed (not as a list: DuckDB indexes no list column).
INDEXABLE = ("string", "integer", "number", "boolean", REF)

DIRECTIONS = ("out", "in", "both")
MAX_DEPTH = 10
# A graph view (``graph()``): rows and the edges among them, at most.
GRAPH_MAX_NODES = 5000
GRAPH_MAX_EDGES = 20000
# The longest _search text.
MAX_SEARCH = 500
SEARCH_TERM = re.compile(r'(-?)(?:"([^"]*)"?|(\S+))')
# What the full-text index leaves out between words, lowercased and without accents (DuckDB's fts ``ignore``):
# anything but letters, digits and dots, and a dot that ends a word (today.), so a domain, version or address
# (site.example.com, v1.2, 10.0.0.1) is one word. Searches split their own text so, and rows not yet in the index
# are split so in SQL (rows.py), so both find the same words.
IGNORE = r"(\.$|\.[^a-z0-9]|[^a-z0-9.])+"
# The words of a text that ranks rows (any_words) used at most.
MAX_ANY_WORDS = 64


class TableError(Refused):
    """A request the /db store refuses: a bad name, row or schema."""


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
    if kind in ("string", REF):
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
    # Its words are searchable (_search): a string field in the table's full-text index.
    fulltext: bool = False
    # A vector field's options (only its): how many numbers, how its index compares them, how it stores them, and the
    # embedding model that makes them from the string field ``from_`` (the API's "from") on every write.
    dimensions: Optional[int] = None
    similarity: Optional[str] = None
    quantization: Optional[str] = None
    model: Optional[str] = None
    from_: Optional[str] = None
    # With a model, the texts put before a search's text and a row's ("" for none); None until the table store has
    # taken them from the model (vectors.py's ``_probed``).
    query_prompt: Optional[str] = None
    document_prompt: Optional[str] = None
    # A ref field's (only its): the table of the workspace whose row ids it holds, and what deleting one of those rows
    # does here (ON_DELETE; restrict unless said).
    to: Optional[str] = None
    on_delete: Optional[str] = None

    @property
    def index_key(self) -> Optional[Tuple[str, bool, bool]]:
        """What its index depends on; None when it has none."""
        return (self.type, self.list, self.unique) if self.indexed else None

    @property
    def vector_options(self) -> Tuple[Any, ...]:
        """What a vector field's index and values depend on, so none of it changes once the field is made (the
        query prompt is not among them: only searches use it)."""
        return (self.type, self.dimensions, self.similarity, self.quantization, self.model, self.from_,
                self.document_prompt)

    def to_dict(self) -> Dict[str, Any]:
        """As the API shows it and the table's schema record keeps it: a vector field's options only on a vector
        field (model, from and the prompts only when it has a model, and the prompts not for a field made before
        them), ``from_`` as "from"; a ref field's options only on a ref field."""
        out = {k: v for k, v in asdict(self).items() if k not in VECTOR_OPTIONS and k not in REF_OPTIONS}
        if self.type == REF:
            out.update(to=self.to, on_delete=self.on_delete)
        if self.type == VECTOR:
            out.update(dimensions=self.dimensions, similarity=self.similarity, quantization=self.quantization)
            if self.model is not None:
                out.update({"model": self.model, "from": self.from_})
                out.update({k: v for k, v in (("query_prompt", self.query_prompt),
                                              ("document_prompt", self.document_prompt)) if v is not None})
        return out

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Field":
        """What ``to_dict`` gave (a record saved before vector fields lacks their options)."""
        return cls(**{("from_" if k == "from" else k): v for k, v in d.items()})

    def check(self, row: Dict[str, Any]) -> None:
        """TableError unless the row's value fits: the type (or a list of it), or absent/null unless required."""
        value = row.get(self.name)
        if value is None:
            if self.required:
                raise TableError(f"{self.name!r} is required")
            return
        if self.type == VECTOR:
            self._check_vector(value)
            return
        what = f"a list of {self.type}" if self.list else self.type
        if self.type == REF:
            what = f"a list of ids of {self.to}" if self.list else f"an id of {self.to}"
        if self.list:
            if not isinstance(value, list):
                raise TableError(f"{self.name!r} is {what}, not {kind_of(value)}")
            for item in value:
                if (item is None and self.type != "any") or not _fits(self.type, item):
                    raise TableError(f"{self.name!r} is {what}: {item!r} is {kind_of(item)}")
        elif not _fits(self.type, value):
            raise TableError(f"{self.name!r} is {what}: {value!r} is {kind_of(value)}")

    def _check_vector(self, value: Any) -> None:
        what = f"a vector of {self.dimensions} numbers"
        if not isinstance(value, list):
            raise TableError(f"{self.name!r} is {what}, not {kind_of(value)}")
        if len(value) != self.dimensions:
            raise TableError(f"{self.name!r} is {what}, not {len(value)}")
        for item in value:
            if not _fits("number", item):
                raise TableError(f"{self.name!r} is {what}: {item!r} is not a finite number")

    def parse(self, raw: str) -> Any:
        """A filter's text as this field's type (an item's, for a list)."""
        if self.type == VECTOR:
            raise TableError(f"Filter {self.name}: a vector field is searched (POST .../_search), not filtered")
        if raw == "null":
            return None
        if self.type in ("string", REF):
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
    # A bucket: a table of files, its fields BUCKET's; set when the table is made.
    bucket: bool = False

    def check(self, row: Dict[str, Any]) -> None:
        for f in self.fields.values():
            f.check(row)
        if self.strict:
            extra = [k for k in row if k != "id" and k not in self.fields]
            if extra:
                raise TableError(f"Not fields of this table (its schema is strict): {', '.join(extra)}")

    @property
    def fulltext(self) -> Tuple[str, ...]:
        """The full-text fields, by name: one index covers them all, so a search finds its words across them."""
        return tuple(sorted(n for n, f in self.fields.items() if f.fulltext))

    @property
    def vector(self) -> Optional[Field]:
        """The vector field, if the table has one (it has one at most)."""
        return next((f for f in self.fields.values() if f.type == VECTOR), None)

    def to_dict(self) -> Dict[str, Any]:
        return {"strict": self.strict, "graph": self.graph, "bucket": self.bucket,
                "fields": [f.to_dict() for _, f in sorted(self.fields.items())]}


EMPTY = TableSchema()

# The longest name a file in a bucket may have, in characters (most file systems' own limit, in bytes).
MAX_FILE_NAME = 255
# A bucket's fields, the same for every bucket: the file's name as uploaded, its size in bytes, the SHA-256 of its
# bytes (hex), its media type as uploaded, when it was uploaded (seconds since the epoch, as a workspace's created),
# and when its bytes reached the bucket's remote (absent until then, and in a bucket without one; buckets/copies.py).
# Its id is the file's, made by the service. Indexed: the name and hash, to find a file, the time, to list the newest
# first, and copied, to find the files waiting to be copied.
BUCKET = TableSchema({f.name: f for f in (
    Field("name", "string", required=True, indexed=True),
    Field("size", "integer", required=True),
    Field("sha256", "string", required=True, indexed=True),
    Field("content_type", "string", required=True),
    Field("created", "number", required=True, indexed=True),
    Field("copied", "number", indexed=True),
)}, strict=True, graph=False, bucket=True)


def column_type(f: Field) -> str:
    """The DuckDB type of the field's column: its type's (``COLUMN``), a list of it (a list of JSON values is one JSON
    array), or a vector's FLOAT[dimensions]."""
    if f.type == VECTOR:
        return f"FLOAT[{int(f.dimensions)}]"
    kind = COLUMN[f.type]
    return kind if not f.list or kind == "JSON" else f"{kind}[]"


def check_field_spec(f: Field) -> Field:
    if f.name == "id":
        raise TableError("id is built in: a string, always indexed and unique")
    if not DECLARED_FIELD.match(f.name):
        raise TableError(f"Bad field name {f.name!r}: letters, digits and underscores, starting with a letter")
    if f.type not in TYPES:
        raise TableError(f"{f.name}: type is one of {', '.join(TYPES)}")
    if f.type != REF and (f.to is not None or f.on_delete is not None):
        raise TableError(f"{f.name}: to and on_delete are a ref field's")
    if f.type == VECTOR:
        return _check_vector_spec(f)
    if any(getattr(f, option) is not None for option in VECTOR_OPTIONS):
        raise TableError(f"{f.name}: dimensions, similarity, quantization, model, from and the prompts are a vector "
                         "field's")
    if f.type == REF:
        f = _check_ref_spec(f)
    if f.unique:
        f = replace(f, indexed=True)
    if f.indexed and f.type not in INDEXABLE:
        raise TableError(f"{f.name}: only {', '.join(INDEXABLE)} fields can be indexed")
    if f.list and f.unique:
        raise TableError(f"{f.name}: a list field cannot be unique")
    if f.fulltext and (f.type != "string" or f.list):
        raise TableError(f"{f.name}: only a string field (not a list) can be full-text")
    if f.fulltext and f.indexed:
        raise TableError(f"{f.name}: a field is indexed or full-text, not both")
    return f


def _check_ref_spec(f: Field) -> Field:
    """A ref field's spec, with its on_delete filled in (restrict), indexed unless it is a list (DuckDB indexes no
    list). Whether ``to`` is a table it may name is the table store's to say (refs.py)."""
    if not isinstance(f.to, str) or len(f.to) > MAX_NAME or not NAME.match(f.to):
        raise TableError(f"{f.name}: to names the table of the workspace whose row ids the field holds")
    f = replace(f, on_delete=f.on_delete or "restrict", indexed=not f.list)
    if f.on_delete not in ON_DELETE:
        raise TableError(f"{f.name}: on_delete is one of {', '.join(ON_DELETE)}")
    if f.list and f.on_delete == "cascade":
        raise TableError(f"{f.name}: a list of refs takes a deleted row's id out of the list (unset) or refuses the "
                         "delete (restrict); cascade would delete the row for one id of its list")
    if f.required and not f.list and f.on_delete == "unset":
        raise TableError(f"{f.name}: a required ref cannot be unset when its row is deleted: restrict or cascade")
    return f


def _check_vector_spec(f: Field) -> Field:
    """A vector field's spec, with its similarity and quantization filled in (COSINE, NONE). Its dimensions may be
    left out when it has a model, until the table store finds them by embedding a probe text."""
    if f.list or f.indexed or f.unique or f.fulltext:
        raise TableError(f"{f.name}: a vector field is never a list, indexed, unique or full-text: its own index "
                         "finds the nearest rows")
    if f.dimensions is not None and (isinstance(f.dimensions, bool) or not isinstance(f.dimensions, int)
                                     or not 1 <= f.dimensions <= MAX_DIMENSIONS):
        raise TableError(f"{f.name}: dimensions is 1 to {MAX_DIMENSIONS}")
    if f.dimensions is None and f.model is None:
        raise TableError(f"{f.name}: a vector field needs its dimensions, or a model that says them")
    f = replace(f, similarity=f.similarity or "COSINE", quantization=f.quantization or "NONE")
    if f.similarity not in SIMILARITIES:
        raise TableError(f"{f.name}: similarity is one of {', '.join(SIMILARITIES)}")
    if f.quantization not in QUANTIZATIONS:
        raise TableError(f"{f.name}: quantization is one of {', '.join(QUANTIZATIONS)}")
    if (f.model is None) != (f.from_ is None):
        raise TableError(f"{f.name}: model and from go together: the model embeds the text of the string field from")
    if f.model is not None and (not isinstance(f.model, str) or not f.model.strip() or len(f.model) > 256):
        raise TableError(f"{f.name}: model is an embedding model's ID")
    if f.model is not None and f.required:
        raise TableError(f"{f.name}: a vector embedded from {f.from_} is null in a row without {f.from_}, so it "
                         f"cannot be required; make {f.from_} required instead")
    for prompt in (f.query_prompt, f.document_prompt):
        if prompt is not None and f.model is None:
            raise TableError(f"{f.name}: the prompts go with a model: they are put before the texts it embeds")
        if prompt is not None and (not isinstance(prompt, str) or len(prompt) > MAX_PROMPT):
            raise TableError(f"{f.name}: a prompt is a text of at most {MAX_PROMPT} characters")
    return f


def make_schema(fields: Sequence[Field], strict: bool = False, graph: bool = False, bucket: bool = False) -> TableSchema:
    """A table's schema from its fields; with ``bucket``, a bucket's (``BUCKET``), whose fields are fixed, so none
    are given, and which is never a graph table."""
    if bucket:
        if fields:
            raise TableError(f"A bucket's fields are fixed ({', '.join(BUCKET.fields)}): give none")
        if graph:
            raise TableError("A bucket is a plain table: its rows are files, which relationships do not join")
        return BUCKET
    checked = [check_field_spec(f) for f in fields]
    if len({f.name for f in checked}) != len(checked):
        raise TableError("Each field is declared once")
    # A field is a column, and the database compares column names ignoring case.
    if len({f.name.lower() for f in checked}) != len(checked):
        raise TableError("Field names differ by more than case: the database reads Name and name as one")
    if len(checked) > MAX_FIELDS:
        raise TableError(f"At most {MAX_FIELDS} fields")
    by_name = {f.name: f for f in checked}
    vectors = [f for f in checked if f.type == VECTOR]
    if len(vectors) > 1:
        raise TableError(f"A table has one vector field at most, not {', '.join(f.name for f in vectors)}")
    for f in vectors:
        source = by_name.get(f.from_) if f.from_ is not None else None
        if f.from_ is not None and (source is None or source.type != "string" or source.list):
            raise TableError(f"{f.name}: from names a string field of the table (not a list) to embed: "
                             f"{f.from_!r} is not one")
    return TableSchema(by_name, strict, graph)


def schema_of(*specs: str) -> TableSchema:
    """A schema from "name:type" specs, for the service's own tables: "+" after the type indexes the field,
    "!" makes it unique, "~" full-text, "[]" makes it a list of the type ("tags:string[]", "run_id:string+",
    "seq:integer!", "title:string~")."""
    fields = []
    for spec in specs:
        name, kind = spec.split(":")
        fields.append(Field(name, kind.rstrip("+!~[]"), list="[]" in kind, indexed="+" in kind or "!" in kind,
                            unique="!" in kind, fulltext="~" in kind))
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


def words_of(text: str) -> List[str]:
    """The words the full-text index finds in ``text``: lowercased, accents removed, split on what it leaves out
    (IGNORE), as DuckDB's fts splits a row's text."""
    plain = "".join(c for c in unicodedata.normalize("NFKD", text.lower()) if not unicodedata.combining(c))
    return [w for w in re.sub(IGNORE, " ", plain).split(" ") if w]


@dataclass(frozen=True)
class Search:
    """A search box's text read (``search_query``): the words a row must hold (``words``, as the index splits them),
    the words starting so it must hold one of each (``prefixes``), those it must not hold (``excluded``, and
    ``excluded_prefixes``), and the phrases of several words it must hold (``phrases``) or must not (``unwanted``) as
    written, lowercased: the index finds a phrase's words but not their order, so the rows are checked for those. A
    text that only ranks (any_words) has ``rank`` words and nothing a row must hold."""
    words: Tuple[str, ...] = ()
    prefixes: Tuple[str, ...] = ()
    excluded: Tuple[str, ...] = ()
    excluded_prefixes: Tuple[str, ...] = ()
    phrases: Tuple[str, ...] = ()
    unwanted: Tuple[str, ...] = ()
    # Words that rank the rows holding any of them, those holding more and rarer ones higher (BM25).
    rank: Tuple[str, ...] = ()
    # What it looks for, lowercased (words, phrases, the starts of word*), to show where a row holds it.
    terms: Tuple[str, ...] = field(default=(), compare=False)


def search_query(text: str) -> Search:
    """A search box's text as a search of the full-text fields: every word must be there (in any of them), a
    "quoted phrase" as written, a -word or -"phrase" must not, and word* finds words starting so. Words are as
    the index splits them: on spaces and most punctuation, but a domain, version or address (site.example.com,
    v1.2) is one word. Case and accents do not matter. TableError for text with nothing to find."""
    if len(text) > MAX_SEARCH:
        raise TableError(f"_search is at most {MAX_SEARCH} characters")
    words, prefixes, excluded, excluded_prefixes, phrases, unwanted, terms = [], [], [], [], [], [], []
    for sign, phrase, word in SEARCH_TERM.findall(text):
        stem = word.rstrip("*")
        if word and word != stem:
            split = words_of(stem)
            if len(split) == 1:
                (excluded_prefixes if sign else prefixes).append(split[0])
                if not sign:
                    terms.append(split[0])
                continue
            word = stem  # a * after more than one word goes: they are searched as words
        found = words_of(word or phrase)
        if not found:
            continue  # punctuation alone: the index keeps no word of it
        if not word and len(found) > 1:
            written = " ".join(phrase.lower().replace("\\", " ").split())
            (unwanted if sign else phrases).append(written)
            if not sign:
                terms.append(written)
                words.extend(found)
            continue  # an unwanted phrase's words apart are no reason to leave a row out
        (excluded if sign else words).extend(found)
        if not sign:
            terms.append(" ".join(found))
    if not words and not prefixes and not phrases:
        raise TableError("_search needs a word to find; -excluded words alone find nothing")
    return Search(tuple(dict.fromkeys(words)), tuple(dict.fromkeys(prefixes)), tuple(dict.fromkeys(excluded)),
                  tuple(dict.fromkeys(excluded_prefixes)), tuple(phrases), tuple(unwanted), terms=tuple(terms))


def any_words(text: str) -> Optional[Search]:
    """A text's words as a search that only ranks: the rows holding any of them, those holding more of them and rarer
    ones scoring higher (BM25); its first MAX_ANY_WORDS words. None for a text with no word."""
    words = tuple(dict.fromkeys(words_of(text)))[:MAX_ANY_WORDS]
    return Search(rank=words) if words else None


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


def encode_cursor(position: int, key: str = "o") -> str:
    """A page's next: the offset of the next page's first row, or under another ``key`` another place in the order
    (the seq after which evidence search's newest-first pages go on: "s")."""
    return base64.urlsafe_b64encode(json.dumps({key: position}).encode()).decode().rstrip("=")


def decode_cursor(cursor: str, key: str = "o") -> int:
    """The place a page's ``next`` names (``encode_cursor``); TableError (400) for anything else, a number the
    database's integers cannot hold among it."""
    try:
        position = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))[key]
    except (binascii.Error, ValueError, KeyError, TypeError):
        raise TableError("Bad cursor: pass the 'next' value of the previous page") from None
    if not isinstance(position, int) or not 0 <= position < 2 ** 63:
        raise TableError("Bad cursor: pass the 'next' value of the previous page")
    return position


def sort_keys(spec: Optional[str]) -> List[Tuple[str, bool]]:
    """A list's ``_sort`` (fields, comma-separated, ``-field`` descending) as (field, descending) pairs."""
    if not spec:
        return []
    return [(f.lstrip("-"), f.startswith("-")) for f in (s.strip() for s in spec.split(",")) if f.lstrip("-")]


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


def row_type_name(workspace: str, table: str) -> str:
    """The table as the catalog (``TABLES``) and messages name it: ``workspace.table``."""
    return f"{workspace}.{table}"


def row_type(workspace: str, table: str) -> str:
    """The table in SQL: its workspace's schema, then itself."""
    return f"{quote(workspace)}.{quote(table)}"


# A table in SQL, for stores that keep their data in /db tables (evals/, evidence/, jobs/, enrichers/).
table_type = row_type


def edges_of(workspace: str) -> str:
    """The table of the workspace's edges in SQL."""
    return f"{quote(workspace)}.{EDGES}"


def fts_schema(workspace: str, table: str) -> str:
    """The schema DuckDB's fts keeps a table's full-text index in (fts_<schema>_<table>), in SQL."""
    return quote(f"fts_{workspace}_{table}")


def check_workspace_name(name: str) -> str:
    """A workspace's name: a name (``check_name``) that DuckDB does not hold already or keep (``RESERVED``)."""
    check_name("workspace", name)
    if name.lower() in RESERVED or name.lower().startswith(RESERVED_PREFIX):
        raise TableError(f"{name!r} is a name the database keeps for itself ({', '.join(RESERVED)} and names starting "
                         f"with {RESERVED_PREFIX}): pick another")
    return name


def ordered(row: Dict[str, Any]) -> Dict[str, Any]:
    """The row with ``id`` first, as clients expect to see it."""
    return {"id": row.get("id"), **{k: v for k, v in row.items() if k != "id"}}

