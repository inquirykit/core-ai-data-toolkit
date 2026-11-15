// The query page's (DataQuery.tsx) logic: the examples it offers, the queries the
// other pages open it with, its parameters, its result's columns, and CSV.

import { parseJsonObject } from "../../../shared/json";
import { sqlName, sqlText, type EdgeTypeEntry, type QueryRow, type TableEntry } from "../data";

// Both query pages send one DuckDB SELECT to POST /db/{ws}/_query: the SQL
// query shows its rows, the Graph query draws them too, when they are edges
// as the workspace's _edges table holds them (from_table, from_id, to_table,
// to_id, maybe type and props). A table's rows hold its declared fields as
// columns, the others in _rest (JSON); _seq is the write that last changed one.

/** Which of the two pages: the SQL query, or the Graph query, which draws the edges its rows are. */
export type QueryPage = "sql" | "graph";
export const QUERY_LIMITS = ["100", "500", "1000"] as const;


export interface QuerySnippet {
  title: string;
  text: string;
  params?: Record<string, unknown>;
}


/**
 * Example queries for the workspace, written with its own table and edge
 * type names: the first graph table (or the first table) and the first edge
 * type stand in, placeholders when it has none. The Graph query's draw the
 * edges they return.
 */
export function querySnippets(page: QueryPage, tables: Pick<TableEntry, "name" | "graph">[], edges: EdgeTypeEntry[]): QuerySnippet[] {
  const table = sqlName(tables[0]?.name ?? "table");
  const graph = sqlName(tables.find((t) => t.graph)?.name ?? tables[0]?.name ?? "table");
  const edge = sqlText(edges[0]?.name ?? "edge_type");
  if (page === "graph") {
    return [
      { title: "Relationships of a type", text: `SELECT * FROM _edges\nWHERE type = ${edge} LIMIT 100` },
      {
        title: "A row's relationships, both ways",
        text: `SELECT * FROM _edges\nWHERE (from_table = ${sqlText(unquoted(graph))} AND from_id = $id)\n   OR (to_table = ${sqlText(unquoted(graph))} AND to_id = $id)`,
        params: { id: "" },
      },
      {
        title: "Relationships within two steps",
        text: `SELECT b.* FROM _edges a\nJOIN _edges b ON b.from_table = a.to_table AND b.from_id = a.to_id\nWHERE a.from_id = $id\nUNION ALL SELECT * FROM _edges WHERE from_id = $id`,
        params: { id: "" },
      },
      { title: "Relationships with a property", text: `SELECT * FROM _edges\nWHERE type = ${edge} AND props->>'since' >= '2020'` },
    ];
  }
  return [
    { title: "Rows of a table", text: `SELECT * FROM ${table} LIMIT 100` },
    { title: "Filter and sort", text: `SELECT * FROM ${table}\nWHERE id >= $after\nORDER BY id LIMIT 100`, params: { after: "" } },
    { title: "A field the table does not declare", text: `SELECT id, _rest->>'kind' AS kind FROM ${table}\nWHERE _rest->>'kind' IS NOT NULL` },
    { title: "Rows counted per value", text: `SELECT _rest->>'kind' AS kind, count(*) AS n FROM ${table}\nGROUP BY kind ORDER BY n DESC` },
    {
      title: "A row's neighbors",
      text: `SELECT n.* FROM _edges e\nJOIN ${graph} n ON n.id = e.to_id AND e.to_table = ${sqlText(unquoted(graph))}\nWHERE e.type = ${edge} AND e.from_id = $id`,
      params: { id: "" },
    },
    { title: "Relationships counted by type", text: "SELECT type, from_table, to_table, count(*) AS n FROM _edges\nGROUP BY ALL ORDER BY n DESC" },
    {
      title: "Rows within two steps",
      text: `WITH RECURSIVE walk(t, i, depth) AS (\n  SELECT ${sqlText(unquoted(graph))}, $id, 0\n  UNION SELECT e.to_table, e.to_id, depth + 1 FROM walk\n  JOIN _edges e ON e.from_table = walk.t AND e.from_id = walk.i WHERE depth < 2\n)\nSELECT t AS "table", i AS id, min(depth) AS depth FROM walk WHERE depth > 0 GROUP BY ALL ORDER BY depth`,
      params: { id: "" },
    },
  ];
}

const unquoted = (quoted: string): string => quoted.slice(1, -1).replace(/""/g, '"');

/** The query a table's page opens the SQL query with: its rows. */
export const tableQuery = (table: string): string => `SELECT * FROM ${sqlName(table)} LIMIT 100`;

/** The query a graph table's link opens the Graph query with: the relationships at its rows, which the page draws. */
export const tableEdgesQuery = (table: string): string =>
  `SELECT * FROM _edges\nWHERE from_table = ${sqlText(table)} OR to_table = ${sqlText(table)} LIMIT 100`;

/** The query an edge type's page opens the Graph query with: its edges, which the page draws. */
export const edgeTypeQuery = (type: string): string => `SELECT * FROM _edges\nWHERE type = ${sqlText(type)} LIMIT 100`;

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
