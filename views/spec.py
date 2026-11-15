"""
What a view and a dashboard are, as the admin writes them: JSON specs, as
Pydantic models whose JSON Schema the API publishes, so the Data section's
pages are typed from it and its forms can use it. What a spec can be checked
for alone is checked here (a bad one is refused as a bad body, 422); what
needs the database (the SQL, the workspace, its views) is checked by the
store (store.py).

A view is a saved read over a workspace's data: its ``source``, one of the
reads /db already makes (``sql``: a read-only statement as ``_query`` takes
it, named as the API names tables; ``search``: a table's nearest rows, the
body ``_search`` takes; ``rows``: a table's rows, as its list gives them),
its ``params``, and a ``display`` saying how its rows are drawn.

``params`` is a JSON Schema of an object: the parameters a run may give,
with their types, defaults, enums and bounds. SQL takes them as DuckDB's
named parameters (``$name``), bound, never written into the text; ``search``
and ``rows`` take them where a value is ``{"$param": "<name>"}``, replaced
in the JSON as it is, never by formatting text (``substitute``). Every
``$param`` names a declared parameter. A run's values are checked against
the schema, with its defaults (``values``): a parameter it does not declare
is refused, whatever it says of additionalProperties.

A dashboard is a page of a workspace's views, in order, each as wide as its
``width`` of 12 columns, sharing one ``params`` schema (each view takes the
values of its own parameters' names from it), refreshed every ``refresh_s``
seconds (0: never; else at least MIN_REFRESH_S).
"""

import copy
import json
import re
from typing import Annotated, Any, Dict, List, Literal, Optional, Union

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from core.errors import Refused
from db.tables import FIELD, MAX_K, MAX_NAME, MAX_SEARCH, NAME

# A parameter's name: also a DuckDB named parameter ($name) and a key of the page's hash, which keeps the names
# starting with _ for itself.
PARAM = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
MAX_PARAMS = 32
# The longest params schema, as JSON.
MAX_SCHEMA_BYTES = 32 * 1024
MAX_SQL = 20000
# How long a view's answer is kept for runs with the same parameters, unless it says otherwise; and the least.
REFRESH_S = 30
MIN_REFRESH_S = 5
MAX_REFRESH_S = 86400
# A dashboard's views at most, and the columns of its grid.
MAX_ITEMS = 48
COLUMNS = 12
# The series a line or bar chart names at most, as y fields.
MAX_Y = 8
# A table's rows as a view's rows source asks for them at most (the list's own page).
MAX_ROWS_LIMIT = 1000
# What a run of a view with no params of its own takes.
EMPTY_PARAMS: Dict[str, Any] = {"type": "object", "properties": {}}

Name = Annotated[str, Field(min_length=1, max_length=MAX_NAME, pattern=NAME.pattern)]
ColumnName = Annotated[str, Field(min_length=1, max_length=200)]
Text = Annotated[str, Field(min_length=1, max_length=200)]
Description = Annotated[str, Field(min_length=1, max_length=2000)]
FilterValue = Union[str, int, float, bool, None]
# A search source's or a rows source's values written in the spec, bounded as a run takes them (SearchQuery,
# RowsQuery), so one no run could take is refused when saved; a $param's value is checked when it runs.
Nearest = Annotated[int, Field(ge=1, le=MAX_K)]
RowsLimit = Annotated[int, Field(ge=1, le=MAX_ROWS_LIMIT)]
Words = Annotated[str, Field(min_length=1, max_length=MAX_SEARCH)]
Sort = Annotated[str, Field(max_length=1000)]


class ParamRef(BaseModel):
    """A value given by a parameter of the view when it runs: ``{"$param": "region"}``."""
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    param: str = Field(..., alias="$param", pattern=PARAM.pattern, description="The name of one of the view's params")


# --------------------------------------------------------------- sources
class SqlSource(BaseModel):
    """A read-only SQL statement over the workspace, as POST /db/{ws}/_query takes it: tables named as the API
    names them, the view's params as $name. Held to a time limit and a number of rows."""
    model_config = ConfigDict(extra="forbid")

    kind: Literal["sql"]
    query: str = Field(..., min_length=1, max_length=MAX_SQL,
                       description="One SELECT in DuckDB's SQL; the params are $name, bound, never written in")
    time_limit_ms: Optional[int] = Field(None, ge=1, description="Stopped past this many milliseconds: at most "
                                                                  "VIEW_TIME_LIMIT_MS, which it is when left out")


class SearchSource(BaseModel):
    """A table's nearest rows to a vector, or a text its vector field's model embeds, as POST
    /db/{ws}/{table}/_search takes them; each row gets its distance as ``_distance``, and with ``hybrid`` or a
    ``rerank`` model its score as ``_score``."""
    model_config = ConfigDict(extra="forbid")

    kind: Literal["search"]
    table: Name
    vector: Optional[Union[Annotated[List[float], Field(min_length=1)], ParamRef]] = Field(
        None, description="The vector to find the rows nearest to")
    text: Optional[Union[Annotated[str, Field(min_length=1)], ParamRef]] = Field(
        None, description="A text the field's model embeds, in place of a vector")
    k: Union[Nearest, ParamRef] = Field(10, description=f"How many rows, nearest first: 1 to {MAX_K}")
    filters: Dict[str, Union[FilterValue, ParamRef]] = Field(
        default_factory=dict, max_length=64,
        description="field: value, all of which hold; each value matched as the JSON it is (the text \"null\" is "
                    "that text, null a field with no value); one whose $param has no value is left out")
    q: Optional[Union[Words, ParamRef]] = Field(None, description="Words the full-text fields hold, as the list's "
                                                                  "_search")
    hybrid: bool = Field(False, description="Rank by the text's words too, as POST .../_search does; each row gets "
                                            "its fused score as _score")
    rerank: Optional[str] = Field(None, min_length=1, max_length=256,
                                  description="A rerank model that reads the rows found, as POST .../_search does; "
                                              "each row gets its score as _score")


class RowsSource(BaseModel):
    """A table's rows, as GET /db/{ws}/{table} gives them: filtered, sorted, searched, the first ``limit``."""
    model_config = ConfigDict(extra="forbid")

    kind: Literal["rows"]
    table: Name
    filters: Dict[str, Union[FilterValue, ParamRef]] = Field(
        default_factory=dict, max_length=64,
        description="field: value, all of which hold; each value matched as the JSON it is (the text \"null\" is "
                    "that text, null a field with no value); one whose $param has no value is left out")
    sort: Optional[Union[Sort, ParamRef]] = Field(None, description="As the list's _sort: fields, comma-separated, "
                                                                    "-field descending")
    search: Optional[Union[Words, ParamRef]] = Field(None, description="Words the full-text fields hold, as _search")
    limit: Optional[Union[RowsLimit, ParamRef]] = Field(None, description=f"Rows at most, 1 to {MAX_ROWS_LIMIT}; "
                                                                          "the view's row cap when left out")


Source = Annotated[Union[SqlSource, SearchSource, RowsSource], Field(discriminator="kind")]


# -------------------------------------------------------------- displays
class TableDisplay(BaseModel):
    """The rows as a table."""
    model_config = ConfigDict(extra="forbid")

    type: Literal["table"]
    columns: Optional[List[ColumnName]] = Field(None, max_length=64, description="The fields shown, in this order; "
                                                                                 "all, as first seen, when left out")


class NumberDisplay(BaseModel):
    """One figure: a field of the first row, large, with what it is."""
    model_config = ConfigDict(extra="forbid")

    type: Literal["number"]
    field: ColumnName
    label: Optional[Text] = None
    unit: Optional[Annotated[str, Field(min_length=1, max_length=32)]] = None


class LineDisplay(BaseModel):
    """A line per y field over x, or with ``series``, a line per value of that field (one y field then)."""
    model_config = ConfigDict(extra="forbid")

    type: Literal["line"]
    x: ColumnName
    y: List[ColumnName] = Field(..., min_length=1, max_length=MAX_Y)
    series: Optional[ColumnName] = None

    @model_validator(mode="after")
    def _one_y_with_series(self):
        if self.series is not None and len(self.y) != 1:
            raise ValueError("with a series field, give one y field: each value of the series field is a line of it")
        return self


class BarDisplay(LineDisplay):
    """Bars per x: one per y field, or with ``series`` one per value of that field; side by side, or stacked."""
    type: Literal["bar"]  # type: ignore[assignment]
    stacked: bool = False


class PieDisplay(BaseModel):
    """A slice per value of ``label``, as large as the sum of ``value``; past seven, the rest are one slice."""
    model_config = ConfigDict(extra="forbid")

    type: Literal["pie"]
    label: ColumnName
    value: ColumnName


class GraphDisplay(BaseModel):
    """The edges the SQL returns (rows with from_table, from_id, to_table and to_id, maybe type), drawn with the rows
    they join as the Data section's graph."""
    model_config = ConfigDict(extra="forbid")

    type: Literal["graph"]


class FilesDisplay(BaseModel):
    """A bucket's files (rows with id, name, size, content_type), each with its download; images as thumbnails."""
    model_config = ConfigDict(extra="forbid")

    type: Literal["files"]
    bucket: Optional[Name] = Field(None, description="The bucket the rows are files of: a rows or search source's "
                                                     "table when left out")


Display = Annotated[Union[TableDisplay, NumberDisplay, LineDisplay, BarDisplay, PieDisplay, GraphDisplay, FilesDisplay],
                    Field(discriminator="type")]


# ---------------------------------------------------------------- params
def check_params_schema(schema: Dict[str, Any]) -> Dict[str, Any]:
    """``schema`` if it is a JSON Schema of an object whose properties are named as parameters are, no larger than
    MAX_SCHEMA_BYTES, referring to nothing outside itself; else a ValueError saying what is wrong."""
    try:
        size = len(json.dumps(schema))
    except (TypeError, ValueError):
        raise ValueError("params is a JSON Schema: JSON") from None
    if size > MAX_SCHEMA_BYTES:
        raise ValueError(f"params is at most {MAX_SCHEMA_BYTES} bytes of JSON Schema")
    if schema.get("type") != "object":
        raise ValueError('params is the JSON Schema of an object: {"type": "object", "properties": {...}}')
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        raise ValueError("params.properties is an object: each parameter's name to its schema")
    if len(properties) > MAX_PARAMS:
        raise ValueError(f"At most {MAX_PARAMS} params")
    bad = [name for name in properties if not PARAM.fullmatch(name)]
    if bad:
        raise ValueError(f"Bad parameter name {bad[0]!r}: letters, digits and _, starting with a letter, up to 64")
    refs = _refs(schema)
    if any(not ref.startswith("#") for ref in refs):
        raise ValueError("params refers only to itself ($ref starting with #), never to another document")
    missing = [ref for ref in refs if not _resolves(schema, ref)]
    if missing:
        raise ValueError(f"params's $ref {missing[0]!r} points at nothing in it")
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as e:
        raise ValueError(f"params is not a valid JSON Schema: {e.message}") from None
    except RecursionError:
        raise ValueError("params nests too deep") from None
    return schema


def _refs(value: Any) -> List[str]:
    """Every $ref in a schema."""
    if isinstance(value, dict):
        found = [value["$ref"]] if isinstance(value.get("$ref"), str) else []
        return found + [r for v in value.values() for r in _refs(v)]
    if isinstance(value, list):
        return [r for v in value for r in _refs(v)]
    return []


# What _pointed gives for a $ref that points at nothing.
_NOWHERE = object()


def _pointed(schema: Dict[str, Any], ref: str) -> Any:
    """The part of the schema a $ref within it (#, #/$defs/x, a JSON pointer) points at; _NOWHERE for none."""
    node: Any = schema
    for part in [p for p in ref[1:].split("/") if p]:
        part = part.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            return _NOWHERE
    return node


def _resolves(schema: Dict[str, Any], ref: str) -> bool:
    """Whether a $ref within the schema (#, #/$defs/x, a JSON pointer) points at a part of it."""
    return isinstance(_pointed(schema, ref), (dict, bool))


def _default(schema: Dict[str, Any], prop: Any) -> Any:
    """A parameter's default: its own, or that of what its $ref points at (and so on), as the page's form reads it
    (its own beside a $ref wins); _NOWHERE for none."""
    followed = set()
    while isinstance(prop, dict):
        if "default" in prop:
            return prop["default"]
        ref = prop.get("$ref")
        if not isinstance(ref, str) or not ref.startswith("#") or ref in followed:  # a $ref back to itself ends
            return _NOWHERE
        followed.add(ref)
        prop = _pointed(schema, ref)
    return _NOWHERE


def declared(schema: Dict[str, Any]) -> List[str]:
    """The parameters a params schema declares, in order."""
    return list((schema.get("properties") or {}).keys())


def values(schema: Dict[str, Any], given: Dict[str, Any]) -> Dict[str, Any]:
    """A run's parameter values, checked against the params ``schema`` with its defaults filled in: Refused for one
    the schema does not declare (whatever it says of additionalProperties), a required one missing, or a value that
    does not fit (naming the parameter)."""
    names = declared(schema)
    unknown = [name for name in given if name not in names]
    if unknown:
        takes = ", ".join(names) or "none"
        raise Refused(f"Unknown parameter {unknown[0]!r}: this takes {takes}")
    out = dict(given)
    for name, prop in (schema.get("properties") or {}).items():
        if name not in out and (default := _default(schema, prop)) is not _NOWHERE:
            out[name] = copy.deepcopy(default)
    try:
        errors = sorted(Draft202012Validator(schema).iter_errors(out), key=lambda e: [str(p) for p in e.absolute_path])
    except RecursionError:
        raise Refused("The params schema nests too deep to check") from None
    except Exception as e:  # a $ref or a keyword the validator cannot follow (saved before it was checked for)
        raise Refused(f"The params schema cannot be checked: {e}") from None
    if errors:
        error = errors[0]
        if error.validator == "required" and not error.absolute_path:
            missing = next((n for n in error.validator_value if n not in out), "")
            raise Refused(f"Parameter {missing!r} is required")
        name = str(error.absolute_path[0]) if error.absolute_path else ""
        raise Refused(f"Parameter {name!r}: {error.message}" if name else f"The parameters: {error.message}")
    return out


# The value a $param stands for when its parameter has none (not given, no default): the key holding it is left out.
_ABSENT = object()


def substitute(value: Any, params: Dict[str, Any]) -> Any:
    """``value`` (a source's JSON) with each ``{"$param": name}`` replaced by that parameter's value, as it is: the
    JSON walked, never text formatted. One whose parameter has no value is left out of the object or list holding
    it, so a filter on an optional parameter applies only when it is given."""
    def walk(v: Any) -> Any:
        if isinstance(v, dict):
            if set(v) == {"$param"}:
                return copy.deepcopy(params[v["$param"]]) if v["$param"] in params else _ABSENT
            walked = {k: walk(x) for k, x in v.items()}
            return {k: x for k, x in walked.items() if x is not _ABSENT}
        if isinstance(v, list):
            return [x for x in (walk(i) for i in v) if x is not _ABSENT]
        return v

    out = walk(value)
    return None if out is _ABSENT else out


def param_refs(value: Any) -> List[str]:
    """The names of the ``$param``s in a source's JSON, in order, each once."""
    found: List[str] = []

    def walk(v: Any) -> None:
        if isinstance(v, dict):
            if set(v) == {"$param"} and isinstance(v["$param"], str):
                found.append(v["$param"])
                return
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)

    walk(value)
    return list(dict.fromkeys(found))


# ----------------------------------------------------------------- specs
class ViewSpec(BaseModel):
    """A view: a saved read over a workspace's data (``source``), its parameters (``params``, a JSON Schema) and
    how its rows are drawn (``display``). The admin writes it; a client key runs it."""
    model_config = ConfigDict(extra="forbid")

    name: Name = Field(..., description="Letters, digits and single underscores, starting with a letter")
    label: Optional[Text] = Field(None, description="What it shows, in words")
    description: Optional[Description] = None
    source: Source
    params: Dict[str, Any] = Field(default_factory=lambda: dict(EMPTY_PARAMS),
                                   description="A JSON Schema of an object: the parameters a run gives, with their "
                                               "types, defaults, enums and bounds")
    display: Display = Field(default_factory=lambda: TableDisplay(type="table"))
    max_rows: Optional[int] = Field(None, ge=1, description="Rows at most: VIEW_MAX_ROWS, or fewer")
    refresh_s: Optional[int] = Field(None, ge=MIN_REFRESH_S, le=MAX_REFRESH_S,
                                     description=f"Seconds an answer is kept for runs with the same parameters "
                                                 f"({REFRESH_S} when left out)")

    @field_validator("params")
    @classmethod
    def _params(cls, schema: Dict[str, Any]) -> Dict[str, Any]:
        return check_params_schema(schema)

    @model_validator(mode="after")
    def _references(self):
        names = declared(self.params)
        missing = [p for p in param_refs(self.source.model_dump(by_alias=True)) if p not in names]
        if missing:
            raise ValueError(f"The source's $param {missing[0]!r} is not one of the params ({', '.join(names) or 'none'})")
        if isinstance(self.source, (SearchSource, RowsSource)):
            bad = [f for f in self.source.filters if not FIELD.match(f)]
            if bad:
                raise ValueError(f"Bad field name {bad[0]!r} in filters")
        if isinstance(self.display, GraphDisplay) and not isinstance(self.source, SqlSource):
            raise ValueError("A graph is drawn from SQL that returns edges (from_table, from_id, to_table, to_id), "
                             "as a workspace's _edges table holds them")
        return self

    @property
    def refresh(self) -> int:
        """Seconds its answer is kept."""
        return self.refresh_s or REFRESH_S


class DashboardItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    view: Name = Field(..., description="A view of the workspace")
    width: int = Field(6, ge=1, le=COLUMNS, description=f"Columns of the {COLUMNS}-column grid it takes")


class DashboardSpec(BaseModel):
    """A dashboard: a page of a workspace's views in order on a 12-column grid, sharing their parameters, refreshed
    every ``refresh_s`` seconds."""
    model_config = ConfigDict(extra="forbid")

    name: Name = Field(..., description="Letters, digits and single underscores, starting with a letter")
    title: Text
    description: Optional[Description] = None
    params: Dict[str, Any] = Field(default_factory=lambda: dict(EMPTY_PARAMS),
                                   description="A JSON Schema of an object, shared: each view takes the values of "
                                               "its own params' names")
    refresh_s: int = Field(0, ge=0, le=MAX_REFRESH_S,
                           description=f"Seconds between refreshes: 0 for none, else at least {MIN_REFRESH_S}")
    items: List[DashboardItem] = Field(default_factory=list, max_length=MAX_ITEMS,
                                       description="Its views, in order")

    @field_validator("params")
    @classmethod
    def _params(cls, schema: Dict[str, Any]) -> Dict[str, Any]:
        return check_params_schema(schema)

    @field_validator("refresh_s")
    @classmethod
    def _refresh(cls, seconds: int) -> int:
        if 0 < seconds < MIN_REFRESH_S:
            raise ValueError(f"refresh_s is 0 (no refresh) or at least {MIN_REFRESH_S} seconds")
        return seconds


# ---------------------------------------------------- resolved sources
class SearchQuery(BaseModel):
    """A search source with its params in: what TableStore.search takes."""
    model_config = ConfigDict(extra="forbid")

    vector: Optional[List[float]] = Field(None, min_length=1)
    text: Optional[str] = Field(None, min_length=1)
    k: int = Field(10, ge=1, le=MAX_K)
    filters: Dict[str, FilterValue] = Field(default_factory=dict)
    q: Optional[str] = Field(None, min_length=1, max_length=MAX_SEARCH)
    hybrid: bool = False
    rerank: Optional[str] = Field(None, min_length=1, max_length=256)

    @model_validator(mode="after")
    def _one(self):
        if (self.vector is None) == (self.text is None):
            raise ValueError("give a vector or a text, one of them")
        return self


class RowsQuery(BaseModel):
    """A rows source with its params in: what TableStore.list takes."""
    model_config = ConfigDict(extra="forbid")

    filters: Dict[str, FilterValue] = Field(default_factory=dict)
    sort: Optional[str] = Field(None, max_length=1000)
    search: Optional[str] = Field(None, min_length=1, max_length=MAX_SEARCH)
    limit: Optional[int] = Field(None, ge=1, le=MAX_ROWS_LIMIT)
