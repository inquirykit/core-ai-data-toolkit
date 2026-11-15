// Table refs (ref fields, as the API calls them) as the Data section shows them
// (DataRefs.tsx): the text a row is shown by, and the SQL, run through POST
// /db/{ws}/_query, of the names of the rows a page's table refs name, of the
// rows a picker offers, and of how many rows of other tables name each row of
// a page; and the row typed in the editor with a ref set.
import { parseJsonObject } from "../../shared/json";
import { sqlName, sqlText, type FieldInfo, type Row, type TableInfo } from "./data";

/** SQL and its $name values, as POST /db/{ws}/_query takes them. */
export interface SqlCall {
  query: string;
  params: Record<string, unknown>;
}

/** The fields a row is shown by, before any other text field. */
const NAMING = ["name", "title", "label"];

/**
 * The SQL of the text a row of a table with these declared fields is shown by: a field called name, title or label
 * (a declared text field, or one the row holds undeclared), else its first declared text field; NULL when it has
 * none, and the row is shown by its id.
 */
export function nameSql(fields: FieldInfo[]): string {
  const text = fields.filter((f) => f.type === "string" && !f.list).map((f) => f.name);
  const named = NAMING.find((n) => text.includes(n));
  if (named) return sqlName(named);
  const undeclared = NAMING.filter((n) => !fields.some((f) => f.name === n)).map((n) => `_rest->>${sqlText(n)}`);
  const any = [...undeclared, ...text.slice(0, 1).map(sqlName)];
  return any.length ? `coalesce(${any.join(", ")})` : "NULL";
}

/** The table refs among a table's declared fields. */
export const tableRefs = (fields: FieldInfo[]): FieldInfo[] => fields.filter((f) => f.type === "ref" && f.to);

/** What a table ref holds in a row: the ids it names (none for a value that is no id). */
export function idsOf(value: unknown): string[] {
  return (Array.isArray(value) ? value : [value]).filter((v): v is string => typeof v === "string");
}

/** The ids the rows name by the table ref ``field``, each once. */
export const namedIds = (rows: Row[], field: string): string[] => [...new Set(rows.flatMap((r) => idsOf(r[field])))];

/** The SQL of the names of rows of ``to`` (with ``fields`` declared) by id: a row (id, name) each. */
export const namesSql = (to: string, fields: FieldInfo[], ids: string[]): SqlCall => ({
  query: `SELECT id, CAST(${nameSql(fields)} AS VARCHAR) AS name FROM ${sqlName(to)} WHERE id = ANY($ids)`,
  params: { ids },
});

/** The SQL of the rows of ``to`` a picker offers for what was typed: those whose id or name holds it, by name. */
export function pickSql(to: string, fields: FieldInfo[], typed: string): SqlCall {
  const name = `CAST(${nameSql(fields)} AS VARCHAR)`;
  const like = `%${typed.trim().replace(/[\\%_]/g, (c) => `\\${c}`)}%`;
  return {
    query: `SELECT id, ${name} AS name FROM ${sqlName(to)} WHERE concat_ws(' ', id, ${name}) ILIKE $like ESCAPE '\\' ORDER BY name NULLS LAST, id`,
    params: { like },
  };
}

/** A table ref of another table naming a table's rows. */
export interface Referrer {
  table: string;
  field: FieldInfo;
}

/** The table refs of the workspace's tables naming ``table``'s rows. */
export const referrers = (tables: TableInfo[], table: string): Referrer[] =>
  tables.flatMap((t) => t.fields.filter((f) => f.type === "ref" && f.to === table).map((field) => ({ table: t.name, field })));

/** The column of a table's rows a referrer is shown in: "orders:customer", as _expand names the step back (no field has a ":"). */
export const backColumn = (r: Referrer): string => `${r.table}:${r.field.name}`;

/** What a referrer's column is headed by: its table, and its field when another ref of that table names the rows too. */
export const backLabel = (r: Referrer, all: Referrer[]): string =>
  all.some((o) => o.table === r.table && o.field.name !== r.field.name) ? `${r.table} · ${r.field.name}` : r.table;

/** The SQL of how many rows of the referrer's table name each of ``ids``: a row (id, n) for each one named. */
export function countsSql(r: Referrer, ids: string[]): SqlCall {
  const field = sqlName(r.field.name);
  const named = r.field.list
    ? `SELECT unnest(list_distinct(${field})) AS named FROM ${sqlName(r.table)} WHERE list_has_any(${field}, $ids)`
    : `SELECT ${field} AS named FROM ${sqlName(r.table)} WHERE ${field} = ANY($ids)`;
  return { query: `SELECT named AS id, count(*) AS n FROM (${named}) WHERE named = ANY($ids) GROUP BY named`, params: { ids } };
}

/**
 * The row typed in the editor with its table ref ``field`` naming ``ids`` (a list for a list, else the first, or
 * null when none), the other fields as they were; or what is wrong with the text.
 */
export function withRef(text: string, field: FieldInfo, ids: string[]): { text: string } | { error: string } {
  const parsed = parseJsonObject(text, { what: "The row" });
  if (!parsed.ok) return { error: `${parsed.error}: fix it to pick a row` };
  const row = { ...parsed.value, [field.name]: field.list ? ids : (ids[0] ?? null) };
  return { text: JSON.stringify(row, null, 2) };
}
