import type { Schemas } from "../shared/api";
import { parseHash } from "../shared/browser";
import { parseJsonObject } from "../shared/json";

// What the Data section (DataSection.tsx, Data*.tsx) needs that is not drawing: where
// the hash points, the columns of a page of rows, the /db list query, and
// reading a row typed as JSON.

/** A row of a /db table: a JSON object with a string id. */
export type Row = Schemas["Row"];

export const FIELD_TYPES = ["string", "integer", "number", "boolean", "object", "any"] as const;
export type FieldType = Schemas["FieldInfo"]["type"];
/** The types an index can cover. */
export const INDEXABLE: FieldType[] = ["string", "integer", "number", "boolean"];

/** A declared field: rows hold ``type`` there (a list of it when ``list``), or leave it out unless ``required``. */
export type FieldInfo = Schemas["FieldInfo"];

export type TableInfo = Schemas["TableInfo"];

/** An edge type of a namespace: edges of it join rows of the namespace's graph tables. */
export type EdgeTypeInfo = Schemas["EdgeTypeInfo"];

export type ObservedField = Schemas["ObservedField"];

/** GET /db/{ns}/{table}/_schema */
export type SchemaInfo = Schemas["SchemaInfo"];

/** "string", "list of integer, indexed", "number, required, unique" */
export function describeField(f: FieldInfo): string {
  const flags = [f.required && "required", f.unique ? "unique" : f.indexed && "indexed"].filter(Boolean);
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
  return { name: o.name, type: OBSERVED_TYPES[only] ?? "any", list, required: false, indexed: false, unique: false };
}

/** GET /db/{ns}; GET /db lists these with ``created``. */
export type Namespace = Schemas["Namespace"];

/** What a table's page shows: its rows (a graph table's rows) or its schema. */
export type TableView = "rows" | "schema";
const TABLE_VIEWS: TableView[] = ["schema"];

export interface DataLocation {
  namespace?: string;
  table?: string;
  /** A table's view other than its rows ("#/data/app/people/schema"); none for its rows */
  view?: TableView;
  /** An edge type's page, its edges ("#/data/app/_edges/knows"; no table is called _edges) */
  edge?: string;
  /** The namespace's SQL query ("#/data/app/_query"; no table is called _query) */
  query?: boolean;
  /** The table the SQL query starts with a query of, run at once (``?t=``) */
  queryTable?: string;
  /** The namespace's Graph query, Cypher drawn ("#/data/app/_graph"; no table is called _graph) */
  graph?: boolean;
  /** The graph table the Graph query starts with a query of, run at once (``?t=``) */
  graphTable?: string;
  /** The edge type the Graph query starts with a query of, run at once (``?e=``) */
  graphEdge?: string;
  /** The row the Graph query's sample starts at (``?n=table/id``), loaded with its neighbors if need be, and selected */
  graphNode?: string;
}

/** The namespace and table "#/data/app/todo" points at; "#/data/app/todo/schema" for its schema, "#/data/app/_edges/knows" for an edge type. */
export function locationFromHash(hash: string): DataLocation {
  const { parts, params } = parseHash(hash);
  const [section, namespace, table, view] = parts;
  if (section !== "data") return {};
  const loc: DataLocation = { namespace, table: undefined };
  if (namespace && table === "_edges") {
    if (view) loc.edge = view;
  } else if (namespace && table === "_query") {
    loc.query = true;
    const t = params.get("t");
    if (t) loc.queryTable = t;
  } else if (namespace && table === "_graph") {
    loc.graph = true;
    const t = params.get("t");
    const e = params.get("e");
    const n = params.get("n");
    if (t) loc.graphTable = t;
    if (e) loc.graphEdge = e;
    if (n) loc.graphNode = n;
  } else if (namespace) {
    loc.table = table;
  }
  if (loc.table && TABLE_VIEWS.includes(view as TableView)) loc.view = view as TableView;
  return loc;
}

export function dataHash(loc: DataLocation): string {
  const parts = ["#/data"];
  if (loc.namespace) {
    parts.push(encodeURIComponent(loc.namespace));
    if (loc.edge) return [...parts, "_edges", encodeURIComponent(loc.edge)].join("/");
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
      if (loc.view && loc.view !== "rows") parts.push(loc.view);
    }
  }
  return parts.join("/");
}

// ------------------------------------------------------------------ sidebar
/** A namespace with the tables and edge types the sidebar lists under it. */
export interface TreeEntry {
  namespace: Namespace;
  tables: TableInfo[];
  edges: EdgeTypeInfo[];
}

/**
 * What the sidebar shows for a search, ignoring case: a namespace whose name matches with every table and
 * edge type, otherwise only its tables and edge types whose names match, and none of it when nothing does.
 * "app/peo" (or "app.peo") matches the namespace and the table apart. An empty search shows everything.
 */
export function searchTree(namespaces: Namespace[], search: string): TreeEntry[] {
  const q = search.trim().toLowerCase();
  const has = (name: string, part: string) => name.toLowerCase().includes(part);
  const cut = q.search(/[/.]/);
  const matching = (namespace: Namespace, part: string): TreeEntry => ({
    namespace,
    tables: namespace.tables.filter((t) => has(t.name, part)),
    edges: namespace.edges.filter((e) => has(e.name, part)),
  });
  return namespaces.flatMap((namespace) => {
    if (cut >= 0) {
      const part = q.slice(cut + 1);
      const entry = matching(namespace, part);
      return has(namespace.name, q.slice(0, cut)) && (entry.tables.length || entry.edges.length || !part) ? [entry] : [];
    }
    if (has(namespace.name, q)) return [{ namespace, tables: namespace.tables, edges: namespace.edges }];
    const entry = matching(namespace, q);
    return entry.tables.length || entry.edges.length ? [entry] : [];
  });
}

/** How a pinned table is kept: "app/people". */
export const pinKey = (namespace: string, table: string): string => `${namespace}/${table}`;

/** The pinned tables in ``tree`` (searchTree's), in the order they were pinned; pins of tables gone or not shown are skipped. */
export function pinnedTables(tree: TreeEntry[], pins: string[]): { namespace: Namespace; table: TableInfo }[] {
  const shown = new Map(tree.flatMap(({ namespace, tables }) => tables.map((table) => [pinKey(namespace.name, table.name), { namespace, table }] as const)));
  return pins.flatMap((pin) => shown.get(pin) ?? []);
}

/**
 * Every field of the rows: id first, then plain values, then lists and
 * objects (wide, so last), each A to Z. ArcadeDB keeps no field order.
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
  sort: Sort | null;
  limit: number;
  cursor?: string;
}

/** The query of GET /db/{ns}/{table}/_count: each filter as ``field=value``, a field filtered twice under its key twice. */
export function filterQuery(filters: ListQuery["filters"]): Record<string, string[]> {
  const query = new Map<string, string[]>();
  for (const [field, value] of filters) query.set(field, [...(query.get(field) ?? []), value]);
  return Object.fromEntries(query);
}

/** The API's own parameters of the list, and a filter under any other key (the schema cannot name those). */
type RowsQuery = { _sort?: string; _limit?: number; _cursor?: string; [field: string]: string[] | string | number | undefined };

/** The query of GET /db/{ns}/{table}: the filters, then the sort and the page. */
export function listQuery(q: ListQuery): RowsQuery {
  const query: RowsQuery = filterQuery(q.filters);
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

// ------------------------------------------------------------------ queries
export type QueryLanguage = "sql" | "cypher";
export const QUERY_LIMITS = ["100", "500", "1000"] as const;

/** A row of a query's result: any JSON object (a whole row carries its ``table``, an edge its ``type``). */
export type QueryRow = Record<string, unknown>;

export interface QuerySnippet {
  title: string;
  language: QueryLanguage;
  text: string;
  params?: Record<string, unknown>;
}

/**
 * Example queries for the namespace, written with its own table and edge
 * type names: the first graph table (or the first table) and the first edge
 * type stand in, placeholders when it has none.
 */
export function querySnippets(language: QueryLanguage, tables: TableInfo[], edges: EdgeTypeInfo[]): QuerySnippet[] {
  const table = tables[0]?.name ?? "table";
  const graph = tables.find((t) => t.graph)?.name ?? table;
  const edge = edges[0]?.name ?? "edge_type";
  if (language === "cypher") {
    return [
      { title: "A table's rows and their relationships", language, text: tableQuery(graph, true).text },
      { title: "A pattern across a relationship", language, text: `MATCH (a:${graph})-[e:${edge}]->(b)\nRETURN a, e, b LIMIT 100` },
      {
        title: "Paths one to three relationships long",
        language,
        text: `MATCH path = (a:${graph})-[:${edge}*1..3]->(b)\nWHERE a.id = $id\nRETURN path LIMIT 100`,
        params: { id: "" },
      },
      { title: "Rows counted per value", language, text: `MATCH (n:${graph})\nRETURN n.kind AS kind, count(*) AS total\nORDER BY total DESC` },
      { title: "Relationships counted by type", language, text: `MATCH (a:${graph})-[e]-(b)\nRETURN type(e) AS type, count(DISTINCT e) AS n` },
    ];
  }
  return [
    { title: "Rows of a table", language, text: `SELECT FROM ${table} LIMIT 100` },
    { title: "Filter and sort", language, text: `SELECT FROM ${table}\nWHERE id >= :after\nORDER BY id LIMIT 100`, params: { after: "" } },
    { title: "Rows counted per value", language, text: `SELECT kind, count(*) AS n FROM ${table}\nGROUP BY kind ORDER BY n DESC` },
    { title: "A row's neighbors", language, text: `SELECT expand(both('${edge}')) FROM ${graph}\nWHERE id = :id`, params: { id: "" } },
    { title: "Relationships with their ends", language, text: `SELECT *, outV().id AS from_id, inV().id AS to_id FROM ${edge} LIMIT 100` },
    {
      title: "Rows within two steps",
      language,
      text: `SELECT *, $depth AS depth FROM (\n  TRAVERSE out('${edge}') FROM (SELECT FROM ${graph} WHERE id = :id) MAXDEPTH 2\n) WHERE $depth > 0`,
      params: { id: "" },
    },
    {
      title: "Shortest path between two rows",
      language,
      text: `SELECT expand(shortestPath($a, $b))\nLET $a = (SELECT FROM ${graph} WHERE id = :a), $b = (SELECT FROM ${graph} WHERE id = :b)`,
      params: { a: "", b: "" },
    },
    {
      title: "A pattern (MATCH)",
      language,
      text: `MATCH {type: ${graph}, as: a, where: (id = :id)}.out('${edge}'){as: b}\nRETURN a.id AS a, b.id AS b`,
      params: { id: "" },
    },
  ];
}

/** The query a table's page opens its query with: its rows in SQL, a graph table's rows and their edges in Cypher. */
export function tableQuery(table: string, graph = false): { language: QueryLanguage; text: string } {
  return graph
    ? { language: "cypher", text: `MATCH (n:${table})\nOPTIONAL MATCH (n)-[e]-(m)\nRETURN n, e, m LIMIT 100` }
    : { language: "sql", text: `SELECT FROM ${table} LIMIT 100` };
}

/** The Cypher an edge type's page opens the Graph query with: its edges with the nodes at both ends. */
export const edgeTypeQuery = (type: string): string => `MATCH (a)-[e:${type}]->(b)\nRETURN a, e, b LIMIT 100`;

/** Every key of the result rows in the order first seen, ``id`` first when there is one. */
export function queryColumns(rows: QueryRow[]): string[] {
  const seen = new Set<string>();
  for (const row of rows) for (const key of Object.keys(row)) seen.add(key);
  const columns = [...seen];
  return columns.includes("id") ? ["id", ...columns.filter((c) => c !== "id")] : columns;
}

/** The parameters box as an object; an error message when it is not a JSON object. */
export function parseParams(text: string): { params: Record<string, unknown> } | { error: string } {
  const parsed = parseJsonObject(text, { what: "Parameters", plural: true, hint: 'like {"id": "ann"}', emptyMeans: {} });
  return parsed.ok ? { params: parsed.value } : { error: parsed.error };
}

/** The rows as CSV: lists and objects as JSON, null and missing values empty. */
export function toCsv(rows: QueryRow[], columns = queryColumns(rows)): string {
  const cell = (value: unknown) => {
    if (value == null) return "";
    const text = typeof value === "object" ? JSON.stringify(value) : String(value);
    return /[",\n\r]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
  };
  return [columns.map(cell), ...rows.map((row) => columns.map((c) => cell(row[c])))].map((line) => line.join(",")).join("\n");
}

// ------------------------------------------------------- nodes and edges
// A graph table's rows, and an edge type's edges, filter with a Cypher
// condition, run through POST /db/{ns}/_query: ``n`` is the table's node; ``e``
// is an edge of the type, ``a`` the node it leaves and ``b`` the one it reaches.

/** A page of a graph table's rows or an edge type's edges: the condition after WHERE ("" for none), the sort, the page. */
export interface CypherPage {
  where: string;
  sort: Sort | null;
  limit: number;
  skip: number;
}

/** "MATCH (n:people)": what a graph table's condition is written against. */
export const nodesMatch = (table: string): string => `MATCH (n:${table})`;

/** "MATCH (a)-[e:knows]->(b)": what an edge type's condition is written against. */
export const edgesMatch = (type: string): string => `MATCH (a)-[e:${type}]->(b)`;

const where = (condition: string) => (condition.trim() ? `\nWHERE ${condition.trim()}` : "");
const property = (variable: string, field: string) => `${variable}.\`${field.replace(/`/g, "``")}\``;

/** The Cypher of a page of the graph table's rows (``row``, the whole row), and of their count. */
export function nodesCypher(table: string, page: CypherPage): { rows: string; count: string } {
  const match = nodesMatch(table) + where(page.where);
  const desc = page.sort?.desc ? " DESC" : "";
  const order = page.sort && page.sort.field !== "id" ? `${property("n", page.sort.field)}${desc}, n.id` : `n.id${desc}`;
  return {
    rows: `${match}\nRETURN properties(n) AS row\nORDER BY ${order}\nSKIP ${page.skip} LIMIT ${page.limit}`,
    count: `${match}\nRETURN count(n) AS count`,
  };
}

/** An edge's column as Cypher: its ends are functions of it, the rest its properties. */
function edgeColumn(column: string): string {
  if (column === "from") return "startNode(e).id";
  if (column === "to") return "endNode(e).id";
  return property("e", column);
}

/** The Cypher of a page of the edge type's edges (``e``, as ``{type, from, to, ...}``), and of their count. */
export function edgesCypher(type: string, page: CypherPage): { rows: string; count: string } {
  const match = edgesMatch(type) + where(page.where);
  const ends = "startNode(e).id, endNode(e).id";
  const order = page.sort ? `${edgeColumn(page.sort.field)}${page.sort.desc ? " DESC" : ""}, ${ends}` : ends;
  return {
    rows: `${match}\nRETURN e\nORDER BY ${order}\nSKIP ${page.skip} LIMIT ${page.limit}`,
    count: `${match}\nRETURN count(e) AS count`,
  };
}

/** An edge type's columns: from and to (its type is the page's), then its properties A to Z. */
export function edgeColumns(edges: QueryRow[]): string[] {
  const ends = ["from", "to"];
  const props = new Set<string>();
  for (const edge of edges) for (const key of Object.keys(edge)) if (key !== "type" && !ends.includes(key)) props.add(key);
  return [...ends, ...[...props].sort((a, b) => a.localeCompare(b))];
}

/** "people → companies": a pair of tables an edge type joins. */
export const joinLabel = (join: { from: string; to: string }): string => `${join.from} → ${join.to}`;
