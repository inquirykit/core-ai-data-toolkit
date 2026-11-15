// The SQL a graph table's rows and an edge type's edges are paged with (DataGraphRows.tsx),
// how a row and an edge read back, and the name of a pair of tables an edge type joins.

import { sqlName, sqlText, type FieldInfo, type QueryRow, type Sort } from "../data";

// A graph table's rows, and an edge type's edges, filter with an SQL
// condition, run through POST /db/{ws}/_query: on the table's columns (its
// declared fields; the others are _rest->>'field'), or on the edge's
// (from_table, from_id, to_table, to_id; its properties are props->>'name').

/** A page of a table's rows or an edge type's edges: the condition after WHERE ("" for none), the sort, the page. */
export interface SqlPage {
  where: string;
  sort: Sort | null;
  limit: number;
  skip: number;
}

const condition = (cond: string) => (cond.trim() ? `(${cond.trim()})` : "true");
/** What a JSON value sorts by, as the API's lists sort an undeclared field: its numbers by their value (9 before 10),
 * then its other values by their text. */
const jsonSortKeys = (value: string): string[] => [
  `(CASE WHEN json_type(${value}) IN ('UBIGINT', 'BIGINT', 'DOUBLE') THEN (${value}->>'$')::DOUBLE END)`,
  `(${value}->>'$')`,
];

/** What a field sorts by: its column, or its value among the undeclared ones. */
const sortKeys = (field: string, declared: string[]): string[] =>
  field === "id" || declared.includes(field) ? [sqlName(field)] : jsonSortKeys(`_rest->${sqlText(field)}`);

/** "SELECT * FROM "people" WHERE": what a table's condition is written after. */
export const rowsFrom = (table: string): string => `SELECT * FROM ${sqlName(table)} WHERE`;

/**
 * The condition of the rows whose ``field`` holds ``value``, as a link filters a table's rows by
 * ("#/data/app/orders?customer=c1"): a declared list field holding it, a declared field equal to it, or an
 * undeclared one in _rest.
 */
export function holdingSql(fields: FieldInfo[], field: string, value: string): string {
  const f = fields.find((x) => x.name === field);
  if (!f) return `_rest->>${sqlText(field)} = ${sqlText(value)}`;
  return f.list ? `list_contains(${sqlName(field)}, ${sqlText(value)})` : `${sqlName(field)} = ${sqlText(value)}`;
}

/** "SELECT * FROM _edges WHERE type = 'knows' AND": what an edge type's condition is written after. */
export const edgesFrom = (type: string): string => `SELECT * FROM _edges WHERE type = ${sqlText(type)} AND`;

/**
 * The SQL of a page of the table's rows (the whole row but its ``vector`` field, which lists leave out as hundreds
 * of numbers a row), and of their count; ``declared`` are its declared fields, the columns a sort reads.
 */
export function rowsQuery(table: string, page: SqlPage, declared: string[], vector?: string): { rows: string; count: string } {
  const from = `FROM ${sqlName(table)}\nWHERE ${condition(page.where)}`;
  const desc = page.sort?.desc ? " DESC" : "";
  const order = page.sort && page.sort.field !== "id"
    ? `${sortKeys(page.sort.field, declared).map((k) => k + desc).join(", ")}, id`
    : `id${desc}`;
  const columns = vector ? `* EXCLUDE (${sqlName(vector)}, _seq)` : "* EXCLUDE (_seq)";
  return {
    rows: `SELECT ${columns} ${from}\nORDER BY ${order}\nLIMIT ${page.limit} OFFSET ${page.skip}`,
    count: `SELECT count(*) AS count ${from}`,
  };
}

/** A row as the query gives it (its columns, and the undeclared fields in _rest), as the API shows a row. */
export function rowOf(raw: QueryRow): QueryRow {
  const { _rest, _seq: _drop, ...columns } = raw;
  void _drop;
  const row: QueryRow = Object.fromEntries(Object.entries(columns).filter(([, v]) => v !== null));
  if (_rest && typeof _rest === "object") for (const [k, v] of Object.entries(_rest as QueryRow)) if (!(k in row)) row[k] = v;
  return row;
}

/** What an edge's column sorts by: an end by its table and id, any other column by its property. */
function edgeSortColumns(column: string): string[] {
  if (column === "from") return ["from_table", "from_id"];
  if (column === "to") return ["to_table", "to_id"];
  return jsonSortKeys(`props->${sqlText(column)}`);
}

/** The SQL of a page of the edge type's edges, and of their count. */
export function edgesQuery(type: string, page: SqlPage): { rows: string; count: string } {
  const from = `FROM _edges\nWHERE type = ${sqlText(type)} AND ${condition(page.where)}`;
  const ends = "from_table, from_id, to_table, to_id";
  const way = page.sort?.desc ? " DESC" : "";
  const order = page.sort ? `${edgeSortColumns(page.sort.field).map((c) => c + way).join(", ")}, ${ends}` : ends;
  return {
    rows: `SELECT * ${from}\nORDER BY ${order}\nLIMIT ${page.limit} OFFSET ${page.skip}`,
    count: `SELECT count(*) AS count ${from}`,
  };
}

/** An edge row of _edges as the API shows an edge: {type, from: "table/id", to, ...its properties}. */
export function edgeOf(raw: QueryRow): QueryRow {
  const props = raw.props && typeof raw.props === "object" ? (raw.props as QueryRow) : {};
  return { type: raw.type, from: `${raw.from_table}/${raw.from_id}`, to: `${raw.to_table}/${raw.to_id}`, ...props };
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
