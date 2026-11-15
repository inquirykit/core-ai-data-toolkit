import type { Schemas } from "../../shared/api";
import { parseHash } from "../../shared/browser";
import { parseJsonObject } from "../../shared/json";

// What the Data section's pages share that is not drawing: where the hash points,
// the groups the sidebar lists a workspace's contents in, the columns of a page of
// rows, the /db list query, reading a row typed as JSON, and quoting SQL. Each
// area's own is in its folder: tables/vectors.ts, graph/edges.ts (and the graph
// view's graph.ts), views/views.ts, query/queries.ts, buckets/buckets.ts.

/** A row of a /db table: a JSON object with a string id. */
export type Row = Schemas["Row"];

export type FieldType = Schemas["FieldInfo"]["type"];
/** Every field type, in the order the schema editor offers them: keyed by the API's, so tsc fails on one missing. */
const FIELD_TYPE_ORDER: Record<FieldType, null> = { string: null, integer: null, number: null, boolean: null, object: null, any: null, ref: null, vector: null };
export const FIELD_TYPES = Object.keys(FIELD_TYPE_ORDER) as FieldType[];
/** What the pages call a type, when not its name in the API: a ref field is a "table ref". */
const FIELD_TYPE_LABEL: Partial<Record<FieldType, string>> = { ref: "table ref" };
export const fieldTypeLabel = (t: FieldType): string => FIELD_TYPE_LABEL[t] ?? t;
/** The types an index can cover (db/schema.py's INDEXABLE; tests/test_vocabularies.py ties the two). */
export const INDEXABLE: FieldType[] = ["string", "integer", "number", "boolean", "ref"];

type OnDelete = NonNullable<FieldInfo["on_delete"]>;
/**
 * What deleting a row a table ref names does to the rows of ``table`` naming it by ``field``, said as the choice in
 * the schema editor: keyed by the API's, so tsc fails on one missing.
 */
const ON_DELETE_TEXT: Record<OnDelete, (table: string, field: string, list: boolean) => string> = {
  restrict: (table) => `Refuse it while rows of ${table} name it`,
  unset: (table, field, list) => (list ? `Keep the rows of ${table}, it taken out of their ${field}` : `Keep the rows of ${table}, their ${field} emptied`),
  cascade: (table) => `Delete the rows of ${table} naming it too`,
};
export const ON_DELETE = Object.keys(ON_DELETE_TEXT) as OnDelete[];
export const onDeleteText = (v: OnDelete, table: string, field: string, list: boolean): string => ON_DELETE_TEXT[v](table, field, list);

/**
 * A declared field: rows hold ``type`` there (a list of it when ``list``), or leave it out unless ``required``. A
 * vector field has its options too: ``dimensions``, ``similarity``, ``quantization``, and ``model`` with ``from`` and
 * the prompts put before the texts it embeds (``query_prompt``, ``document_prompt``).
 */
export type FieldInfo = Schemas["FieldInfo"];

/** How a search compares a vector field's vectors: keyed by the API's, so tsc fails on one missing. */
const SIMILARITY_ORDER: Record<NonNullable<FieldInfo["similarity"]>, null> = { COSINE: null, DOT_PRODUCT: null, EUCLIDEAN: null };
export const SIMILARITIES = Object.keys(SIMILARITY_ORDER) as NonNullable<FieldInfo["similarity"]>[];
/** The options only a vector field has. */
export const VECTOR_OPTIONS = ["dimensions", "similarity", "quantization", "model", "from", "query_prompt", "document_prompt"] as const;
/** The vector options whose "" is a value (no prompt) once the field is saved. */
export const PROMPTS = ["query_prompt", "document_prompt"] as const;

/**
 * A field as PUT _schema takes it: a vector field's options only when set (no dimensions with a model: the server
 * takes the model's; no model, no from and no prompts). A prompt left empty on a vector field not ``saved`` yet is
 * left out, so the server takes the model's own; on a saved one, "" is its value: no prompt.
 */
export function fieldAsSent(f: FieldInfo, saved: boolean): FieldInfo {
  const out: FieldInfo = { ...f };
  const value = (option: (typeof VECTOR_OPTIONS)[number]) => saved && (PROMPTS as readonly string[]).includes(option) && out[option] === "";
  for (const option of VECTOR_OPTIONS) if ((out[option] ?? "") === "" && !value(option)) delete out[option];
  if (!out.model) for (const option of ["from", ...PROMPTS] as const) delete out[option];
  return out;
}
/** The most numbers a vector holds (db/schema.py's MAX_DIMENSIONS; tests/test_vocabularies.py ties the two). */
export const MAX_DIMENSIONS = 4096;

/** The table's vector field, if it has one (one at most). */
export const vectorField = (fields: FieldInfo[]): FieldInfo | undefined => fields.find((f) => f.type === "vector");

/** A table as GET /db lists it: its name, its kind, what keeps it, a bucket's remote. */
export type TableEntry = Schemas["TableEntry"];
/** A table as GET /db/{workspace} describes it: its entry with its row count, size and schema. */
export type TableInfo = Schemas["TableInfo"];

/** An edge type of a workspace: edges of it join rows of the workspace's graph tables. */
export type EdgeTypeEntry = Schemas["EdgeTypeEntry"];
export type EdgeTypeInfo = Schemas["EdgeTypeInfo"];

export type ObservedField = Schemas["ObservedField"];

/** GET /db/{ws}/{table}/_schema */
export type SchemaInfo = Schemas["SchemaInfo"];

/** "string", "list of integer, indexed", "number, required, unique", "string, full-text", "vector of 384, COSINE, embedded from text by m" */
export function describeField(f: FieldInfo): string {
  if (f.type === "vector") {
    const options = [`vector of ${f.dimensions ?? "?"}`, f.similarity ?? "COSINE", f.required && "required"];
    return [...options, f.model && `embedded from ${f.from} by ${f.model}`].filter(Boolean).join(", ");
  }
  if (f.type === "ref") {
    const kind = f.list ? `table ref to several rows of ${f.to}` : `table ref to ${f.to}`;
    return [kind, f.required && "required", f.unique && "unique", f.on_delete && f.on_delete !== "restrict" && `on delete ${f.on_delete}`]
      .filter(Boolean)
      .join(", ");
  }
  const flags = [f.required && "required", f.unique ? "unique" : f.indexed && "indexed", f.fulltext && "full-text"].filter(Boolean);
  return [f.list ? `list of ${f.type}` : f.type, ...flags].join(", ");
}

const OBSERVED_TYPES: Record<string, FieldType> = {
  string: "string", integer: "integer", number: "number", boolean: "boolean", object: "object",
};

/** A declaration fitting what was observed: the one type the rows hold (integers alongside numbers make number). */
export function inferField(o: ObservedField): FieldInfo {
  const kinds = Object.keys(o.types).filter((k) => k !== "null");
  const list = kinds.length > 0 && kinds.every((k) => k.startsWith("list"));
  // An empty list ("list") says nothing about its items.
  const items = new Set(list ? kinds.filter((k) => k !== "list").map((k) => k.slice(5, -1)) : kinds);
  if (items.has("integer") && items.has("number")) items.delete("integer");
  const only = items.size === 1 ? [...items][0] : "any";
  return { name: o.name, type: OBSERVED_TYPES[only] ?? "any", list, required: false, indexed: false, unique: false, fulltext: false };
}

/** A workspace as GET /db lists it (with ``?all=true`` the service's own too, ``managed`` set): its tables and edge types by name. */
export type WorkspaceEntry = Schemas["WorkspaceEntry"];
/** GET /db/{workspace}: a workspace with its tables' and edge types' counts, sizes and schemas. */
export type Workspace = Schemas["Workspace"];

/** What a table's page shows: its rows (a graph table's rows) or its schema. */
export type TableView = "rows" | "schema";
const TABLE_VIEWS: TableView[] = ["schema"];

export interface DataLocation {
  workspace?: string;
  table?: string;
  /** A table's view other than its rows ("#/data/app/people/schema"); none for its rows */
  view?: TableView;
  /** On a table's rows, the row opened in its editor (``?_row=c1``): where a link to a row points */
  row?: string;
  /**
   * On a table's rows, the field and the value its rows are filtered by (``?customer=c1``, as GET /db/{ws}/{table}
   * takes it; no field starts with "_"): where a link to the rows naming a row points
   */
  filter?: [string, string];
  /** An edge type's page, its edges ("#/data/app/_edges/knows"; no table is called _edges) */
  edge?: string;
  /** The workspace's SQL query ("#/data/app/_query"; no table is called _query) */
  query?: boolean;
  /** The table the SQL query starts with a query of, run at once (``?t=``) */
  queryTable?: string;
  /** The workspace's Graph query, SQL whose edge rows are drawn ("#/data/app/_graph"; no table is called _graph) */
  graph?: boolean;
  /** The workspace's diagram: its tables, their table refs and its relationships ("#/data/app/_diagram") */
  diagram?: boolean;
  /** The graph table the Graph query starts with a query of, run at once (``?t=``) */
  graphTable?: string;
  /** The edge type the Graph query starts with a query of, run at once (``?e=``) */
  graphEdge?: string;
  /** The row the Graph query's sample starts at (``?n=table/id``), loaded with its neighbors if need be, and selected */
  graphNode?: string;
  /** A view's page ("#/data/app/_views/sales"; NEW_ENTRY for a new one's editor): its result, and its editor */
  viewName?: string;
  /** A dashboard's page ("#/data/app/_dashboards/main"; NEW_ENTRY for a new one's editor) */
  dashboard?: string;
  /**
   * The params' values on a view's or dashboard's page, as text, each its own key ("?year=2025&region=north"), so a
   * link opens it with the same values; views/views.ts reads them by the params' types.
   */
  values?: Record<string, string>;
}

/** What a view's or dashboard's location names for a new one: no name starts with "_". */
export const NEW_ENTRY = "_new";

/**
 * The workspace and table "#/data/app/todo" points at; "#/data/app/todo/schema" for its schema, "#/data/app/_edges/knows"
 * for an edge type, "#/data/app/_views/sales?year=2025" for a view with its params' values (_dashboards for a dashboard).
 */
export function locationFromHash(hash: string): DataLocation {
  const { parts, params } = parseHash(hash);
  const [section, workspace, table, view] = parts;
  if (section !== "data") return {};
  const loc: DataLocation = { workspace, table: undefined };
  if (workspace && table === "_edges") {
    if (view) loc.edge = view;
  } else if (workspace && table === "_query") {
    loc.query = true;
    const t = params.get("t");
    if (t) loc.queryTable = t;
  } else if (workspace && (table === "_views" || table === "_dashboards") && view) {
    if (table === "_views") loc.viewName = view;
    else loc.dashboard = view;
    loc.values = Object.fromEntries(params.entries());
  } else if (workspace && (table === "_views" || table === "_dashboards")) {
    // The list of them is the workspace's page.
  } else if (workspace && table === "_diagram") {
    loc.diagram = true;
  } else if (workspace && table === "_graph") {
    loc.graph = true;
    const t = params.get("t");
    const e = params.get("e");
    const n = params.get("n");
    if (t) loc.graphTable = t;
    if (e) loc.graphEdge = e;
    if (n) loc.graphNode = n;
  } else if (workspace) {
    loc.table = table;
    // Only a table's rows take these: "#/data/app/people/graph?n=..." is an old view's, fallen back to its rows.
    const row = params.get("_row");
    const filter = [...params.entries()].find(([k]) => !k.startsWith("_"));
    if (table && view === undefined && row) loc.row = row;
    if (table && view === undefined && filter) loc.filter = filter;
  }
  if (loc.table && TABLE_VIEWS.includes(view as TableView)) loc.view = view as TableView;
  return loc;
}

export function dataHash(loc: DataLocation): string {
  const parts = ["#/data"];
  if (loc.workspace) {
    parts.push(encodeURIComponent(loc.workspace));
    if (loc.edge) return [...parts, "_edges", encodeURIComponent(loc.edge)].join("/");
    if (loc.viewName || loc.dashboard) {
      const name = loc.viewName ?? loc.dashboard!;
      const query = new URLSearchParams(loc.values ?? {}).toString();
      return [...parts, loc.viewName ? "_views" : "_dashboards", encodeURIComponent(name)].join("/") + (query ? `?${query}` : "");
    }
    if (loc.diagram) return [...parts, "_diagram"].join("/");
    if (loc.query) {
      parts.push("_query");
      return parts.join("/") + (loc.queryTable ? `?t=${encodeURIComponent(loc.queryTable)}` : "");
    }
    if (loc.graph) {
      parts.push("_graph");
      const params = new URLSearchParams();
      if (loc.graphTable) params.set("t", loc.graphTable);
      if (loc.graphEdge) params.set("e", loc.graphEdge);
      if (loc.graphNode) params.set("n", loc.graphNode);
      const query = params.toString();
      return parts.join("/") + (query ? `?${query}` : "");
    }
    if (loc.table) {
      parts.push(encodeURIComponent(loc.table));
      if (loc.view && loc.view !== "rows") return [...parts, loc.view].join("/");
      const params = new URLSearchParams();
      if (loc.filter) params.set(...loc.filter);
      if (loc.row) params.set("_row", loc.row);
      const query = params.toString();
      return parts.join("/") + (query ? `?${query}` : "");
    }
  }
  return parts.join("/");
}

// ------------------------------------------------------------------ sidebar
/** A workspace with the tables and edge types the sidebar lists under it. */
export interface TreeEntry {
  workspace: WorkspaceEntry;
  tables: TableEntry[];
  edges: EdgeTypeEntry[];
}

/** The workspace that always exists: it cannot be deleted. */
export const DEFAULT_WORKSPACE = "default";

/** Whether the service keeps it (evals, tasks...): then it is not a workspace, and is read-only here. */
export const isOwn = (workspace: Pick<WorkspaceEntry, "managed">): boolean => Boolean(workspace.managed);

/**
 * What keeps the table, when the service does: its whole workspace (``managed``, evals or tasks) or the table
 * alone (``kept``, a kept table of a workspace). Then the table is read-only here.
 */
export const keeperOf = (workspace: Pick<WorkspaceEntry, "managed">, table?: Pick<TableEntry, "kept">): string | null =>
  workspace.managed ?? table?.kept ?? null;

/**
 * What the sidebar shows for a search, ignoring case: a workspace whose name or label matches with every table
 * and edge type, otherwise only its tables and edge types whose names match, and none of it when nothing does.
 * "app/peo" (or "app.peo") matches the workspace and the table apart. An empty search shows everything.
 */
export function searchTree(workspaces: WorkspaceEntry[], search: string): TreeEntry[] {
  const q = search.trim().toLowerCase();
  const has = (name: string, part: string) => name.toLowerCase().includes(part);
  const named = (workspace: WorkspaceEntry, part: string) => has(workspace.name, part) || has(workspace.label ?? "", part);
  const cut = q.search(/[/.]/);
  const matching = (workspace: WorkspaceEntry, part: string): TreeEntry => ({
    workspace,
    tables: workspace.tables.filter((t) => has(t.name, part)),
    edges: workspace.edges.filter((e) => has(e.name, part)),
  });
  return workspaces.flatMap((workspace) => {
    if (cut >= 0) {
      const part = q.slice(cut + 1);
      const entry = matching(workspace, part);
      return named(workspace, q.slice(0, cut)) && (entry.tables.length || entry.edges.length || !part) ? [entry] : [];
    }
    if (named(workspace, q)) return [{ workspace, tables: workspace.tables, edges: workspace.edges }];
    const entry = matching(workspace, q);
    return entry.tables.length || entry.edges.length ? [entry] : [];
  });
}

/** The groups the sidebar lists a workspace's contents in, in this order. */
export type GroupKey = "tables" | "graph" | "vectors" | "buckets";
const GROUP_LABELS: Record<GroupKey, string> = { tables: "Tables", graph: "Graph", vectors: "Vectors", buckets: "Buckets" };

/** A group of a workspace's contents: its tables, or for Graph its relationships (edge types). */
export interface ContentGroup<T extends TableEntry = TableEntry, E extends EdgeTypeEntry = EdgeTypeEntry> {
  key: GroupKey;
  label: string;
  tables: T[];
  edges: E[];
}

/**
 * The group a table is listed in: a bucket (a table of files, ``bucket``) in Buckets, one with a ``vector``
 * field in Vectors, any other in Tables, graph tables too (Graph lists the relationships that join them).
 */
export function groupOfTable(table: Pick<TableEntry, "bucket" | "vector">): Exclude<GroupKey, "graph"> {
  if (table.bucket) return "buckets";
  if (table.vector) return "vectors";
  return "tables";
}

/**
 * A workspace's contents, or the part of them a search shows (searchTree's), in their groups: Tables, Graph
 * (the relationships), Vectors and Buckets, each only when it has entries. With ``graphQuery`` the Graph group
 * also holds the Graph query entry, so it shows without relationships.
 */
export function groupsOf<T extends TableEntry, E extends EdgeTypeEntry>(of: { tables: T[]; edges: E[] }, graphQuery = false): ContentGroup<T, E>[] {
  const tablesOf = (key: GroupKey) => of.tables.filter((t) => groupOfTable(t) === key);
  return (Object.keys(GROUP_LABELS) as GroupKey[])
    .map((key) => ({ key, label: GROUP_LABELS[key], tables: key === "graph" ? [] : tablesOf(key), edges: key === "graph" ? of.edges : [] }))
    .filter((g) => g.tables.length > 0 || g.edges.length > 0 || (g.key === "graph" && graphQuery));
}

/** How a pinned table is kept: "app/people". */
export const pinKey = (workspace: string, table: string): string => `${workspace}/${table}`;

/** The pinned tables in ``tree`` (searchTree's), in the order they were pinned; pins of tables gone or not shown are skipped. */
export function pinnedTables(tree: TreeEntry[], pins: string[]): { workspace: WorkspaceEntry; table: TableEntry }[] {
  const shown = new Map(tree.flatMap(({ workspace, tables }) => tables.map((table) => [pinKey(workspace.name, table.name), { workspace, table }] as const)));
  return pins.flatMap((pin) => shown.get(pin) ?? []);
}

/**
 * Every field of the rows: id first, then plain values, then lists and
 * objects (wide, so last), each A to Z: a row's fields come in no set order.
 */
export function columnsOf(rows: Row[]): string[] {
  const nested = new Map<string, boolean>();
  for (const row of rows) {
    for (const [key, value] of Object.entries(row)) {
      if (key === "id") continue;
      const kind = cellKind(value);
      nested.set(key, (nested.get(key) ?? false) || kind === "list" || kind === "object");
    }
  }
  const fields = [...nested.keys()].sort((a, b) => Number(nested.get(a)) - Number(nested.get(b)) || a.localeCompare(b));
  return ["id", ...fields];
}

export type CellKind = "missing" | "null" | "boolean" | "number" | "string" | "list" | "object";

export function cellKind(value: unknown): CellKind {
  if (value === undefined) return "missing";
  if (value === null) return "null";
  if (Array.isArray(value)) return "list";
  const t = typeof value;
  return t === "boolean" || t === "number" || t === "string" ? t : "object";
}

/** The row without the field ``name`` (a vector field, whose hundreds of numbers no grid shows). */
export function without<T extends Record<string, unknown>>(row: T, name: string | undefined): T {
  if (name === undefined || !(name in row)) return row;
  const { [name]: _left, ...rest } = row;
  return rest as T;
}

/** A one-line preview of any JSON value, cut to ``max`` characters. */
export function preview(value: unknown, max = 60): string {
  const text = typeof value === "string" ? value : JSON.stringify(value);
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}

export interface Sort {
  field: string;
  desc: boolean;
}

/** A click on a column header: ascending, then descending, then unsorted (by id). */
export function nextSort(current: Sort | null, field: string): Sort | null {
  if (current?.field !== field) return { field, desc: false };
  return current.desc ? null : { field, desc: true };
}

export interface ListQuery {
  filters: [field: string, value: string][];
  /** Words the full-text fields hold (``_search``); empty for none */
  search?: string;
  sort: Sort | null;
  limit: number;
  cursor?: string;
}

/** The query of GET /db/{ws}/{table}/_count: each filter as ``field=value``, a field filtered twice under its key twice. */
export function filterQuery(filters: ListQuery["filters"]): Record<string, string[]> {
  const query = new Map<string, string[]>();
  for (const [field, value] of filters) query.set(field, [...(query.get(field) ?? []), value]);
  return Object.fromEntries(query);
}

/** The API's own parameters of the list, and a filter under any other key (the schema cannot name those). */
type RowsQuery = { _search?: string; _sort?: string; _limit?: number; _cursor?: string; [field: string]: string[] | string | number | undefined };

/** The query of GET /db/{ws}/{table}/_count: the filters, and the search when there is one. */
export function countQuery(q: Pick<ListQuery, "filters" | "search">): RowsQuery {
  const query: RowsQuery = filterQuery(q.filters);
  if (q.search?.trim()) query._search = q.search.trim();
  return query;
}

/** The query of GET /db/{ws}/{table}: the filters and the search, then the sort and the page. */
export function listQuery(q: ListQuery): RowsQuery {
  const query: RowsQuery = countQuery(q);
  if (q.sort) query._sort = `${q.sort.desc ? "-" : ""}${q.sort.field}`;
  query._limit = q.limit;
  if (q.cursor) query._cursor = q.cursor;
  return query;
}

/** The row typed in the editor, or what is wrong with it. An empty id is dropped, so the server makes one. */
export function parseRow(text: string): { row: Record<string, unknown> } | { error: string } {
  const parsed = parseJsonObject(text, { what: "A row" });
  if (!parsed.ok) return { error: parsed.error };
  const row = { ...parsed.value };
  if (row.id === "" || row.id === undefined) {
    delete row.id;
    return { row };
  }
  if (typeof row.id !== "string") return { error: "id is a string" };
  return { row };
}

// ---------------------------------------------------------------------- sql
// What the pages that send SQL to POST /db/{ws}/_query share: the query page
// (query/), a graph table's and an edge type's pages (graph/edges.ts) and the
// views (views/views.ts).

/** A row of a query's result: any JSON object. */
export type QueryRow = Record<string, unknown>;

/** A name as SQL quotes it: "people", "default" (a word SQL keeps stays a name). */
export const sqlName = (name: string): string => `"${name.replace(/"/g, '""')}"`;
/** A text as an SQL string: 'it''s'. */
export const sqlText = (text: string): string => `'${text.replace(/'/g, "''")}'`;
