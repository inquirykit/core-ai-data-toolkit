import type { Schemas } from "../../../shared/api";
import { formSchema } from "../../../shared/form-schema";
import type { FormSchema } from "../../../shared/schema-form";
import { parseJsonObject } from "../../../shared/json";
import { sqlName, type Row } from "../data";

// What the Data section's views and dashboards (DataViews.tsx, DataDashboard.tsx,
// ViewDisplay.tsx) need that is not drawing: the params' values as the hash keeps
// them and as a run sends them, the series and slices a chart draws from a
// result, what a display names that a result lacks, the editor's spec from its
// fields, and a dashboard's items moved about.

/** A view as PUT takes it (views/spec.py's ViewSpec), and as GET answers it, with when it was made and changed. */
export type ViewSpec = Schemas["ViewSpec"];
export type ViewInfo = Schemas["ViewInfo"];
/** What a run of a view answers. */
export type ViewResult = Schemas["ViewResult"];
export type DashboardSpec = Schemas["DashboardSpec"];
export type DashboardInfo = Schemas["DashboardInfo"];
export type DashboardItem = Schemas["DashboardItem"];
export type ViewDisplay = NonNullable<ViewSpec["display"]>;
export type DisplayType = ViewDisplay["type"];
export type ViewSource = ViewSpec["source"];
export type SourceKind = ViewSource["kind"];
export type ChartDisplay = Schemas["LineDisplay"] | Schemas["BarDisplay"];
/** A display that needs only the rows: not a graph's or files', which need the workspace. */
export type RowsDisplay = Exclude<ViewDisplay, { type: "graph" | "files" }>;
/** A params schema: the JSON Schema of an object, each property a parameter. */
export type ParamsSchema = Record<string, unknown>;
export type ParamValues = Record<string, unknown>;

/** A dashboard refreshes no more often than this, whatever its spec says (the server refuses less, but for 0). */
export const MIN_REFRESH_S = 5;
/** A dashboard's grid. */
export const GRID_COLUMNS = 12;
/** The series and slices a chart colors apart (the palette's slots); past them, the rest are one, "Other". */
export const MAX_SERIES = 8;
export const OTHER = "Other";

export const DISPLAYS: [DisplayType, string][] = [
  ["table", "Table"],
  ["number", "Number"],
  ["line", "Line"],
  ["bar", "Bar"],
  ["pie", "Pie"],
  ["graph", "Graph"],
  ["files", "Files"],
];
export const SOURCES: [SourceKind, string][] = [
  ["sql", "SQL"],
  ["rows", "Rows"],
  ["search", "Search"],
];

// ----------------------------------------------------------------- params
type Property = { type?: unknown; default?: unknown; enum?: unknown; const?: unknown; anyOf?: unknown; oneOf?: unknown; allOf?: unknown };
const propertiesOf = (params: ParamsSchema | undefined): Record<string, Property> =>
  ((params?.properties ?? {}) as Record<string, Property>) ?? {};

/** The parameters a params schema declares, in order. */
export const paramNames = (params: ParamsSchema | undefined): string[] => Object.keys(propertiesOf(params));

/** A params schema as the form (SchemaForm) takes it: its own $defs inlined. */
export const paramsForm = (params: ParamsSchema | undefined): FormSchema => formSchema((params ?? { type: "object", properties: {} }) as FormSchema);

/** Each parameter's schema as the form reads it: its $ref followed, an optional one's null left out. */
const formProperties = (params: ParamsSchema | undefined): Record<string, Property> => propertiesOf(paramsForm(params) as ParamsSchema);

/** A value as the hash writes it: text as it is, the rest as JSON. */
const hashText = (value: unknown): string => (typeof value === "string" ? value : JSON.stringify(value));

const jsonType = (value: unknown): string => (value === null ? "null" : Array.isArray(value) ? "array" : typeof value);

/** The choices a parameter is held to: its enum, or its const. */
const choicesOf = (p: Property): unknown[] => (Array.isArray(p.enum) ? p.enum : p.const !== undefined ? [p.const] : []);

/**
 * The JSON types a parameter's value may have: its type's, its choices' (an enum or a const with no type), and those
 * of the schemas it is any, one or all of; none when it does not say.
 */
function typesOf(p: Property): Set<string> {
  const types = new Set<string>(Array.isArray(p.type) ? p.type.map(String) : typeof p.type === "string" ? [p.type] : []);
  for (const choice of choicesOf(p)) types.add(jsonType(choice));
  for (const list of [p.anyOf, p.oneOf, p.allOf]) {
    if (Array.isArray(list)) for (const part of list) if (part && typeof part === "object") for (const t of typesOf(part as Property)) types.add(t);
  }
  return types;
}

/**
 * One value as the hash gives it, read back as valuesToHash wrote it: the choice (enum or const) whose text it is;
 * else the text, when the parameter may be text (the hash writes text as it is, and never a null); else JSON (a
 * number, a boolean, a list, an object), or the text when it is no JSON, for the server to say what is wrong.
 */
function readValue(p: Property, raw: string): unknown {
  const chosen = choicesOf(p).find((choice) => hashText(choice) === raw);
  if (chosen !== undefined) return chosen;
  if (typesOf(p).has("string")) return raw;
  try {
    return JSON.parse(raw);
  } catch {
    return raw;
  }
}

/**
 * The values of a view's or dashboard's params that the hash gives, each read by its parameter's schema as the form
 * reads it ($refs followed); names the schema does not declare are left out.
 */
export function valuesFromHash(params: ParamsSchema | undefined, raw: Record<string, string> = {}): ParamValues {
  const props = formProperties(params);
  return Object.fromEntries(Object.entries(raw).flatMap(([name, text]) => (name in props ? [[name, readValue(props[name], text)]] : [])));
}

/**
 * ``values`` with the defaults of the parameters they do not give, as the params form shows them ($refs followed; a
 * default of null is no value): what a dashboard's views run with, so they run with what its form shows rather than
 * their own defaults.
 */
export function withDefaults(params: ParamsSchema | undefined, values: ParamValues): ParamValues {
  const out = { ...values };
  for (const [name, p] of Object.entries(formProperties(params))) {
    if (out[name] === undefined && p.default !== undefined && p.default !== null) out[name] = p.default;
  }
  return out;
}

/** Values as the hash keeps them: text as it is, the rest as JSON; unset and empty ones left out. */
export function valuesToHash(values: ParamValues): Record<string, string> {
  return Object.fromEntries(
    Object.entries(values).flatMap(([name, v]) => (v === undefined || v === null || v === "" ? [] : [[name, typeof v === "string" ? v : JSON.stringify(v)]])),
  );
}

/** The values a dashboard's view runs with: those of its own params' names. */
export function valuesFor(params: ParamsSchema | undefined, values: ParamValues): ParamValues {
  const names = new Set(paramNames(params));
  return Object.fromEntries(Object.entries(values).filter(([name]) => names.has(name)));
}

/** Milliseconds between a dashboard's refreshes: 0 for none, never under MIN_REFRESH_S. */
export const refreshMs = (refreshS: number | null | undefined): number => (refreshS ? Math.max(MIN_REFRESH_S, refreshS) * 1000 : 0);

// ---------------------------------------------------------------- results
/** The fields the display draws from (none for a table of every column, a graph, or files). */
export function displayFields(display: ViewDisplay): string[] {
  switch (display.type) {
    case "table":
      return display.columns ?? [];
    case "number":
      return [display.field];
    case "line":
    case "bar":
      return [display.x, ...display.y, ...(display.series ? [display.series] : [])];
    case "pie":
      return [display.label, display.value];
    default:
      return [];
  }
}

/** The fields the display names that the result has no column of (none when it has no rows to tell by). */
export function missingFields(display: ViewDisplay, result: Pick<ViewResult, "columns" | "rows">): string[] {
  if (!result.rows.length) return [];
  const have = new Set(result.columns);
  return [...new Set(displayFields(display))].filter((f) => !have.has(f));
}

/** A value as a chart's number: a number, or text that reads as one; null otherwise. */
export function toNumber(value: unknown): number | null {
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  if (typeof value === "string" && value.trim() !== "" && Number.isFinite(Number(value))) return Number(value);
  return null;
}

/** An x value or a series' name as a label. */
export const labelOf = (value: unknown): string => (value === null || value === undefined ? "(none)" : typeof value === "object" ? JSON.stringify(value) : String(value));

export interface ChartSeries {
  name: string;
  /** One per category; null where the rows have none */
  values: (number | null)[];
  /** The palette's slot, in the order first seen; -1 for "Other" */
  slot: number;
}

/**
 * What a line or bar chart draws from the rows: the x values in the order first seen, and a series per y field, or
 * with ``series`` one per value of that field (of its one y), the values of rows sharing an x summed. Past
 * MAX_SERIES, the series after the first MAX_SERIES - 1 (in the order first seen) are summed as "Other", so a series
 * keeps its color whatever comes after it.
 */
export function chartData(rows: Row[] | Record<string, unknown>[], display: Pick<ChartDisplay, "x" | "y" | "series">): { categories: string[]; series: ChartSeries[]; folded: number } {
  const categories: string[] = [];
  const at = new Map<string, number>();
  const sums = new Map<string, Map<number, number>>();
  const names: string[] = [];
  const add = (name: string, i: number, value: number | null) => {
    if (!sums.has(name)) {
      sums.set(name, new Map());
      names.push(name);
    }
    if (value === null) return;
    const series = sums.get(name)!;
    series.set(i, (series.get(i) ?? 0) + value);
  };
  for (const row of rows as Record<string, unknown>[]) {
    const x = labelOf(row[display.x]);
    if (!at.has(x)) {
      at.set(x, categories.length);
      categories.push(x);
    }
    const i = at.get(x)!;
    if (display.series) add(labelOf(row[display.series]), i, toNumber(row[display.y[0]]));
    else for (const y of display.y) add(y, i, toNumber(row[y]));
  }
  const kept = names.length > MAX_SERIES ? names.slice(0, MAX_SERIES - 1) : names;
  const valuesOf = (of: Map<number, number>[]) => categories.map((_, i) => (of.some((m) => m.has(i)) ? of.reduce((sum, m) => sum + (m.get(i) ?? 0), 0) : null));
  const series = kept.map((name, slot) => ({ name, values: valuesOf([sums.get(name)!]), slot }));
  const rest = names.slice(kept.length);
  if (rest.length) series.push({ name: OTHER, values: valuesOf(rest.map((n) => sums.get(n)!)), slot: -1 });
  return { categories, series, folded: rest.length };
}

export interface Slice {
  name: string;
  value: number;
  slot: number;
}

/**
 * A pie's slices: ``value`` summed per ``label``, largest first, those that are no positive number left out. Past
 * MAX_SERIES, the slices after the first MAX_SERIES - 1 are one, "Other".
 */
export function pieSlices(rows: Record<string, unknown>[], label: string, value: string): Slice[] {
  const sums = new Map<string, number>();
  for (const row of rows) {
    const v = toNumber(row[value]);
    if (v === null || v <= 0) continue;
    const name = labelOf(row[label]);
    sums.set(name, (sums.get(name) ?? 0) + v);
  }
  const all = [...sums.entries()].sort((a, b) => b[1] - a[1]).map(([name, v], slot) => ({ name, value: v, slot }));
  if (all.length <= MAX_SERIES) return all;
  const rest = all.slice(MAX_SERIES - 1).reduce((sum, s) => sum + s.value, 0);
  return [...all.slice(0, MAX_SERIES - 1), { name: OTHER, value: rest, slot: -1 }];
}

/** A number display's figure: its field of the first row. */
export const figureOf = (rows: Record<string, unknown>[], field: string): unknown => rows[0]?.[field];

/** A figure as the number display writes it: whole numbers grouped, others to two decimals, the rest as text. */
export function fmtFigure(value: unknown): string {
  const n = toNumber(value);
  if (n === null) return value === undefined ? "–" : labelOf(value);
  return n.toLocaleString(undefined, { maximumFractionDigits: Number.isInteger(n) ? 0 : 2 });
}

/** "A bar chart of SQL", "A table of the rows of orders": what a view shows, in words. */
export function describeView(spec: Pick<ViewSpec, "source" | "display">): string {
  const type = spec.display?.type ?? "table";
  const shown = { table: "table", number: "figure", line: "line chart", bar: "bar chart", pie: "pie chart", graph: "graph", files: "gallery of files" }[type];
  const source = spec.source;
  const from = source.kind === "sql" ? "SQL" : source.kind === "rows" ? `the rows of ${source.table}` : `the nearest rows of ${source.table}`;
  return `A ${shown} of ${from}`;
}

/** The bucket a files display's rows are files of: its own, or the rows' or search's table. */
export function bucketOf(spec: Pick<ViewSpec, "source" | "display">): string | null {
  if (spec.display?.type === "files" && spec.display.bucket) return spec.display.bucket;
  return spec.source.kind === "sql" ? null : spec.source.table;
}

// ----------------------------------------------------------------- editor
/** What the view editor holds: the spec's fields as they are typed. */
export interface ViewDraft {
  name: string;
  label: string;
  description: string;
  kind: SourceKind;
  sql: string;
  /** A rows or search source's options as JSON: all but its kind */
  sourceJson: string;
  timeLimitMs: string;
  paramsJson: string;
  display: DisplayDraft;
  maxRows: string;
  refreshS: string;
}

/** A display's fields as typed: lists comma-separated. */
export interface DisplayDraft {
  type: DisplayType;
  columns: string;
  field: string;
  label: string;
  unit: string;
  x: string;
  y: string;
  series: string;
  stacked: boolean;
  value: string;
  bucket: string;
}

const EMPTY_DISPLAY: DisplayDraft = { type: "table", columns: "", field: "", label: "", unit: "", x: "", y: "", series: "", stacked: false, value: "", bucket: "" };
const NO_PARAMS = { type: "object", properties: {} };
const pretty = (value: unknown) => JSON.stringify(value, null, 2);

/** What a new view starts as: SQL over the workspace's first table, its rows as a table. */
export function newDraft(tables: string[]): ViewDraft {
  const table = tables[0] ?? "table";
  return {
    name: "",
    label: "",
    description: "",
    kind: "sql",
    sql: `SELECT * FROM ${sqlName(table)} LIMIT 100`,
    sourceJson: pretty(sourceExample("rows", table)),
    timeLimitMs: "",
    paramsJson: pretty(NO_PARAMS),
    display: EMPTY_DISPLAY,
    maxRows: "",
    refreshS: "",
  };
}

/** A rows or search source's options to start from, over ``table``. */
export function sourceExample(kind: Exclude<SourceKind, "sql">, table: string): Record<string, unknown> {
  return kind === "rows" ? { table, filters: {}, sort: "-id", limit: 100 } : { table, text: "a text its model embeds", k: 10 };
}

const list = (text: string) =>
  text
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
const optional = (text: string) => text.trim() || undefined;

/** A saved view as the editor holds it. */
export function draftOf(spec: ViewSpec): ViewDraft {
  const source = spec.source;
  const { kind, ...options } = source as ViewSource & Record<string, unknown>;
  const d = spec.display ?? { type: "table" };
  const display: DisplayDraft = { ...EMPTY_DISPLAY, type: d.type };
  if (d.type === "table") display.columns = (d.columns ?? []).join(", ");
  if (d.type === "number") Object.assign(display, { field: d.field, label: d.label ?? "", unit: d.unit ?? "" });
  if (d.type === "line" || d.type === "bar") Object.assign(display, { x: d.x, y: d.y.join(", "), series: d.series ?? "", stacked: d.type === "bar" && Boolean(d.stacked) });
  if (d.type === "pie") Object.assign(display, { label: d.label, value: d.value });
  if (d.type === "files") display.bucket = d.bucket ?? "";
  const rest = Object.fromEntries(Object.entries(options).filter(([k, v]) => k !== "query" && k !== "time_limit_ms" && v !== null));
  return {
    name: spec.name,
    label: spec.label ?? "",
    description: spec.description ?? "",
    kind,
    sql: source.kind === "sql" ? source.query : "",
    sourceJson: source.kind === "sql" ? pretty(sourceExample("rows", "table")) : pretty(rest),
    timeLimitMs: source.kind === "sql" && source.time_limit_ms ? String(source.time_limit_ms) : "",
    paramsJson: pretty(spec.params ?? NO_PARAMS),
    display,
    maxRows: spec.max_rows ? String(spec.max_rows) : "",
    refreshS: spec.refresh_s ? String(spec.refresh_s) : "",
  };
}

/** The display a draft says, its empty fields left out. */
export function displayOf(d: DisplayDraft): ViewDisplay {
  switch (d.type) {
    case "table":
      return list(d.columns).length ? { type: "table", columns: list(d.columns) } : { type: "table" };
    case "number":
      return { type: "number", field: d.field.trim(), label: optional(d.label), unit: optional(d.unit) };
    case "line":
      return { type: "line", x: d.x.trim(), y: list(d.y), series: optional(d.series) };
    case "bar":
      return { type: "bar", x: d.x.trim(), y: list(d.y), series: optional(d.series), stacked: d.stacked };
    case "pie":
      return { type: "pie", label: d.label.trim(), value: d.value.trim() };
    case "graph":
      return { type: "graph" };
    case "files":
      return { type: "files", bucket: optional(d.bucket) };
  }
}

/** A whole number typed in a field, or what is wrong with it; empty is unset. */
function wholeNumber(text: string, what: string): { value?: number } | { error: string } {
  if (!text.trim()) return {};
  const n = Number(text);
  return Number.isInteger(n) && n > 0 ? { value: n } : { error: `${what} is a whole number above 0` };
}

/** The spec the editor's draft says, or the first thing wrong with it (the server checks the rest). */
export function specOf(draft: ViewDraft): { spec: ViewSpec } | { error: string } {
  if (!draft.name.trim()) return { error: "Give the view a name" };
  const params = parseJsonObject(draft.paramsJson, { what: "The params", plural: true, hint: '{"type": "object", "properties": {...}}', emptyMeans: NO_PARAMS });
  if (!params.ok) return { error: params.error };
  const maxRows = wholeNumber(draft.maxRows, "Rows at most");
  const refresh = wholeNumber(draft.refreshS, "Kept for (s)");
  const limit = wholeNumber(draft.timeLimitMs, "The time limit");
  for (const n of [maxRows, refresh, limit]) if ("error" in n) return { error: n.error };
  let source: ViewSource;
  if (draft.kind === "sql") {
    if (!draft.sql.trim()) return { error: "Write the view's SQL" };
    source = { kind: "sql", query: draft.sql, time_limit_ms: "value" in limit ? limit.value : undefined };
  } else {
    const options = parseJsonObject(draft.sourceJson, { what: "The source", hint: '{"table": "orders", ...}' });
    if (!options.ok) return { error: options.error };
    source = { ...options.value, kind: draft.kind } as ViewSource;
  }
  return {
    spec: {
      name: draft.name.trim(),
      label: optional(draft.label),
      description: optional(draft.description),
      source,
      params: params.value,
      display: displayOf(draft.display),
      max_rows: "value" in maxRows ? maxRows.value : undefined,
      refresh_s: "value" in refresh ? refresh.value : undefined,
    },
  };
}

/**
 * A display of ``type`` drawn from a result's columns, keeping what the draft says already: a chart's x the first
 * column and its y the next that holds numbers, a number the first that does, a pie's label and value likewise.
 */
export function guessDisplay(draft: DisplayDraft, type: DisplayType, result?: Pick<ViewResult, "columns" | "rows">): DisplayDraft {
  const columns = result?.columns ?? [];
  const numeric = columns.filter((c) => result?.rows.some((r) => toNumber((r as Record<string, unknown>)[c]) !== null));
  const first = columns[0] ?? "";
  const firstNumber = numeric.find((c) => c !== first) ?? numeric[0] ?? "";
  const next = { ...draft, type };
  if ((type === "line" || type === "bar") && !next.x) Object.assign(next, { x: first, y: next.y || firstNumber });
  if (type === "pie" && !next.label) Object.assign(next, { label: first, value: next.value || firstNumber });
  if (type === "number" && !next.field) next.field = numeric[0] ?? first;
  return next;
}

// ------------------------------------------------------------- dashboards
/** The items with the one at ``index`` moved ``by`` places (up is -1), kept within the list. */
export function moveItem<T>(items: T[], index: number, by: number): T[] {
  const to = Math.min(items.length - 1, Math.max(0, index + by));
  if (to === index) return items;
  const out = [...items];
  const [item] = out.splice(index, 1);
  out.splice(to, 0, item);
  return out;
}

/** What the dashboard editor holds: the spec's fields as they are typed. */
export interface DashboardDraft {
  name: string;
  title: string;
  description: string;
  paramsJson: string;
  refreshS: string;
  items: DashboardItem[];
}

/** What a new dashboard starts as. */
export const newDashboardDraft = (): DashboardDraft => ({ name: "", title: "", description: "", paramsJson: pretty(NO_PARAMS), refreshS: "0", items: [] });

/** A saved dashboard as the editor holds it. */
export const dashboardDraftOf = (spec: DashboardSpec): DashboardDraft => ({
  name: spec.name,
  title: spec.title,
  description: spec.description ?? "",
  paramsJson: pretty(spec.params ?? NO_PARAMS),
  refreshS: String(spec.refresh_s ?? 0),
  items: (spec.items ?? []).map((i) => ({ view: i.view, width: i.width ?? 6 })),
});

/** The spec the dashboard editor's draft says, or the first thing wrong with it (the server checks the rest). */
export function dashboardSpecOf(draft: DashboardDraft): { spec: DashboardSpec } | { error: string } {
  if (!draft.name.trim()) return { error: "Give the dashboard a name" };
  if (!draft.title.trim()) return { error: "Give the dashboard a title" };
  const refresh = Number(draft.refreshS.trim() || "0");
  if (!Number.isInteger(refresh) || refresh < 0) return { error: "Refresh is a whole number of seconds: 0 for none" };
  if (refresh > 0 && refresh < MIN_REFRESH_S) return { error: `Refresh is 0 (none) or at least ${MIN_REFRESH_S} seconds` };
  const params = parseJsonObject(draft.paramsJson, { what: "The params", plural: true, hint: '{"type": "object", "properties": {...}}', emptyMeans: NO_PARAMS });
  if (!params.ok) return { error: params.error };
  return {
    spec: {
      name: draft.name.trim(),
      title: draft.title.trim(),
      description: optional(draft.description),
      params: params.value,
      refresh_s: refresh,
      items: draft.items.map((i) => ({ view: i.view, width: clampWidth(i.width ?? 6) })),
    },
  };
}

/** A width as the grid takes it: a whole number of columns, 1 to 12. */
export const clampWidth = (width: number): number => Math.min(GRID_COLUMNS, Math.max(1, Math.round(width) || 1));

/**
 * The params a dashboard's views declare between them, merged into one schema: what a new dashboard's params can
 * start from (a parameter two views declare differently takes the first one's).
 */
export function sharedParams(views: Pick<ViewSpec, "params">[]): ParamsSchema {
  const properties: Record<string, unknown> = {};
  // As the form reads them, so a $ref into a view's own $defs is not left pointing at nothing.
  for (const view of views) for (const [name, p] of Object.entries(formProperties(view.params))) if (!(name in properties)) properties[name] = p;
  return { type: "object", properties };
}

/** "Refreshes every 30 s", or that it does not. */
export const refreshLabel = (refreshS: number | null | undefined): string =>
  refreshS ? `Refreshes every ${Math.max(MIN_REFRESH_S, refreshS)} s` : "Does not refresh by itself";
