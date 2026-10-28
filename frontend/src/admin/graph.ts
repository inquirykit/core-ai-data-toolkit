// The graph view of a namespace (GraphView.tsx) apart from the drawing:
// what GET /db/{ns}/_graph returns, the filters (tables, edge types, rules
// on fields, a search, the degree) and what each node and edge is shown as
// under them, the labels and sizes of nodes, and the colors of tables.
//
// The filters follow what graph tools do: Neo4j Browser and Bloom show a
// legend of labels and relationship types to switch off, Bloom filters on a
// property and grays the rest out until they are dismissed, Gephi filters
// on the degree. Here the tables and edge types are hard filters (off is
// hidden), the rules on fields dim or hide (``mode``), and the search only
// highlights (the rest is dimmed).

import type { Schemas } from "../shared/api";
import { cellKind, type CellKind } from "./data";

export type GraphNode = Schemas["GraphNode"];

/** An edge: its type, both ends as ``table/id``, and its properties. */
export type GraphEdge = Schemas["GraphEdge"];

/** GET /db/{ns}/_graph */
export type GraphResponse = Schemas["GraphView"];

/** One of GET /db/{ns}/{table}/{id}/_neighbors; ``row`` is null when the row is gone. */
export type Neighbor = Schemas["Neighbor"];

export const NODE_LIMITS = ["250", "500", "1000", "2500"] as const;
export type NodeLimit = (typeof NODE_LIMITS)[number];
/** Neighbors loaded when a node is expanded. */
export const EXPAND_LIMIT = 500;

/** The query of GET /db/{ns}/_graph for the whole graph (every graph table and edge type): the filters are applied in the browser. */
export function graphQuery(limit: number) {
  return { _limit: limit, _edge_limit: Math.min(20000, limit * 8) };
}

/** A node's key in the drawn graph: ``table/id``, as edges name their ends. */
export const nodeKey = (table: string, id: string): string => `${table}/${id}`;
export const edgeKey = (e: GraphEdge): string => `${e.type} ${e.from} ${e.to}`;

/** The table and id a key names (an id may itself hold slashes). */
export function splitKey(key: string): [table: string, id: string] {
  const at = key.indexOf("/");
  return at < 0 ? [key, ""] : [key.slice(0, at), key.slice(at + 1)];
}

/**
 * The rows and edges anywhere in a graph query's result (in a column, a list,
 * a path), to draw: a row is ``{table, row}``, the table one of the
 * namespace's ``graphTables`` and the row with an ``id``, an edge an object
 * with ``type``, ``from`` and ``to``. An end of an edge that the result leaves out is drawn
 * by its id alone. Nothing else in the result is drawn.
 */
export function graphFromRows(rows: Record<string, unknown>[], graphTables: string[]): GraphResponse {
  const known = new Set(graphTables);
  const nodes = new Map<string, GraphNode>();
  const edges = new Map<string, GraphEdge>();
  const visit = (value: unknown): void => {
    if (Array.isArray(value)) return value.forEach(visit);
    if (value === null || typeof value !== "object") return;
    const v = value as Record<string, unknown>;
    const row = v.row as Record<string, unknown> | null | undefined;
    if (typeof v.table === "string" && known.has(v.table) && typeof row?.id === "string") {
      nodes.set(nodeKey(v.table, row.id), { table: v.table, row: row as GraphNode["row"] });
    } else if (typeof v.type === "string" && typeof v.from === "string" && typeof v.to === "string") {
      edges.set(edgeKey(v as GraphEdge), v as GraphEdge);
    } else {
      Object.values(v).forEach(visit);
    }
  };
  rows.forEach(visit);
  for (const e of edges.values()) {
    for (const end of [e.from, e.to]) {
      const [table, id] = splitKey(end);
      if (!nodes.has(end)) nodes.set(end, { table, row: { id } });
    }
  }
  const all = [...nodes.values()];
  return { nodes: all, edges: [...edges.values()], tables: [...new Set(all.map((n) => n.table))], outside: 0, more: { nodes: false, edges: false } };
}

// ---------------------------------------------------------------- colors
/**
 * One color per table, in a fixed order (the first table keeps its color when
 * others are switched off): a categorical palette whose neighbors stay apart
 * under color blindness, stepped for each theme. A ninth table is gray.
 */
const TABLE_COLORS = {
  light: ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"],
  dark: ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"],
};
const OTHER_COLOR = { light: "#8e8e99", dark: "#73737d" };

export function tableColor(index: number, dark: boolean): string {
  const colors = dark ? TABLE_COLORS.dark : TABLE_COLORS.light;
  return index >= 0 && index < colors.length ? colors[index] : dark ? OTHER_COLOR.dark : OTHER_COLOR.light;
}

/** What the canvas draws in the theme's colors (WebGL takes hex, not the theme's CSS variables). */
export const CANVAS = {
  light: { label: "#18181b", edgeLabel: "#52525b", edge: "#c4c4cd", dimNode: "#e4e4ea", dimEdge: "#eeeef2" },
  dark: { label: "#fafafa", edgeLabel: "#a1a1aa", edge: "#4b4b55", dimNode: "#2e2e36", dimEdge: "#27272d" },
};

// ----------------------------------------------------------------- rules
export const OPS = ["=", "!=", "contains", ">", ">=", "<", "<=", "exists", "missing"] as const;
export type Op = (typeof OPS)[number];
/** The operators that take no value. */
export const UNARY: readonly Op[] = ["exists", "missing"];

/** A condition on a node: on every table, or on one (nodes of other tables pass it). */
export interface Rule {
  table: string | null;
  field: string;
  op: Op;
  value: string;
}

/** Fields every node has besides its row's: the table it is in, and how many shown edges touch it. */
export const PSEUDO_FIELDS = ["id", "table", "degree"] as const;

/** A typed value, as the table filters read one: a number, true, false or null as such; quoted text as text. */
export function parseValue(raw: string): unknown {
  const text = raw.trim();
  if (text === "") return "";
  if (text === "true") return true;
  if (text === "false") return false;
  if (text === "null") return null;
  if (/^-?\d+(\.\d+)?([eE][+-]?\d+)?$/.test(text)) return Number(text);
  if (text.length >= 2 && text[0] === '"' && text[text.length - 1] === '"') {
    try {
      return JSON.parse(text);
    } catch {
      return text.slice(1, -1);
    }
  }
  return text;
}

function fieldValue(node: GraphNode, degree: number, field: string): unknown {
  if (field === "table") return node.table;
  if (field === "degree") return degree;
  return node.row[field];
}

const same = (a: unknown, b: unknown): boolean => {
  if (a === b) return true;
  if (a === null || b === null || typeof a === "object" || typeof b === "object") return JSON.stringify(a) === JSON.stringify(b);
  return String(a) === String(b);
};

/** Numbers against numbers; otherwise text against text, A to Z. Undefined when the two do not compare. */
function compare(a: unknown, b: unknown): number | undefined {
  if (typeof a === "number" && typeof b === "number") return a - b;
  if (typeof a === "number" && typeof b === "string" && b.trim() !== "" && !Number.isNaN(Number(b))) return a - Number(b);
  if (typeof a === "string" && typeof b === "string") return a.localeCompare(b);
  if (typeof a === "boolean" && typeof b === "boolean") return Number(a) - Number(b);
  return undefined;
}

export function matchesRule(node: GraphNode, degree: number, rule: Rule): boolean {
  if (rule.table && node.table !== rule.table) return true;
  const value = fieldValue(node, degree, rule.field);
  const present = value !== undefined && value !== null;
  if (rule.op === "exists") return present;
  if (rule.op === "missing") return !present;
  const wanted = parseValue(rule.value);
  if (wanted === null) return rule.op === "=" ? value === null : rule.op === "!=" ? value !== null : false;
  if (!present) return rule.op === "!=";
  switch (rule.op) {
    case "=":
      return Array.isArray(value) ? value.some((v) => same(v, wanted)) : same(value, wanted);
    case "!=":
      return Array.isArray(value) ? !value.some((v) => same(v, wanted)) : !same(value, wanted);
    case "contains": {
      if (Array.isArray(value)) return value.some((v) => same(v, wanted) || (typeof v === "string" && typeof wanted === "string" && v.toLowerCase().includes(wanted.toLowerCase())));
      const text = typeof value === "object" ? JSON.stringify(value) : String(value);
      return text.toLowerCase().includes(String(wanted).toLowerCase());
    }
    default: {
      const c = compare(value, wanted);
      if (c === undefined) return false;
      return rule.op === ">" ? c > 0 : rule.op === ">=" ? c >= 0 : rule.op === "<" ? c < 0 : c <= 0;
    }
  }
}

/** Whether the search text is in the node's id, table, or any of its plain values (not case-sensitive). */
export function matchesSearch(node: GraphNode, text: string): boolean {
  const needle = text.trim().toLowerCase();
  if (!needle) return true;
  if (node.table.toLowerCase().includes(needle)) return true;
  for (const value of Object.values(node.row)) {
    const kind = cellKind(value);
    if (kind === "string" || kind === "number" || kind === "boolean") {
      if (String(value).toLowerCase().includes(needle)) return true;
    } else if (kind === "list") {
      if ((value as unknown[]).some((v) => typeof v !== "object" && String(v).toLowerCase().includes(needle))) return true;
    }
  }
  return false;
}

const OP_TEXT: Record<Op, string> = { "=": "=", "!=": "≠", contains: "contains", ">": ">", ">=": "≥", "<": "<", "<=": "≤", exists: "exists", missing: "missing" };

/** "age ≥ 30", "people · name contains ann", "email missing" */
export function describeRule(rule: Rule): string {
  const scope = rule.table ? `${rule.table} · ` : "";
  const value = UNARY.includes(rule.op) ? "" : ` ${rule.value}`;
  return `${scope}${rule.field} ${OP_TEXT[rule.op]}${value}`;
}

/** The rule is complete: a field, and a value unless the operator takes none. */
export function validRule(rule: Rule): boolean {
  return rule.field.trim() !== "" && (UNARY.includes(rule.op) || rule.value.trim() !== "");
}

// ------------------------------------------------------------ visibility
export type Shown = "shown" | "dimmed" | "hidden";
export type FilterMode = "dim" | "hide";

export interface ViewFilters {
  /** The tables switched on */
  tables: Set<string>;
  /** The edge types switched on */
  types: Set<string>;
  rules: Rule[];
  search: string;
  /** What a node failing a rule becomes */
  mode: FilterMode;
  /** Drop nodes no shown edge touches */
  hideIsolated: boolean;
}

export interface View {
  nodes: Map<string, Shown>;
  edges: Map<string, Shown>;
  /** Edges touching each node after the tables and edge types are switched off, before the rules */
  degree: Map<string, number>;
  shownNodes: number;
  shownEdges: number;
  /** Nodes not hidden, per table */
  perTable: Map<string, number>;
  /** Edges not hidden, per type */
  perType: Map<string, number>;
}

/**
 * What each node and edge is shown as. Tables and edge types switched off
 * hide; the degree counts the edges left; then isolated nodes are hidden if
 * asked, nodes failing a rule are dimmed or hidden by the mode, and nodes
 * the search does not match are dimmed. An edge is hidden with either end,
 * and dimmed with either end.
 */
export function computeView(nodes: Map<string, GraphNode>, edges: Map<string, GraphEdge>, f: ViewFilters): View {
  const state = new Map<string, Shown>();
  const degree = new Map<string, number>();
  for (const [key, node] of nodes) {
    state.set(key, f.tables.has(node.table) ? "shown" : "hidden");
    degree.set(key, 0);
  }

  const edgeState = new Map<string, Shown>();
  for (const [key, edge] of edges) {
    const live = f.types.has(edge.type) && state.get(edge.from) === "shown" && state.get(edge.to) === "shown";
    edgeState.set(key, live ? "shown" : "hidden");
    if (!live) continue;
    degree.set(edge.from, (degree.get(edge.from) ?? 0) + 1);
    if (edge.to !== edge.from) degree.set(edge.to, (degree.get(edge.to) ?? 0) + 1);
  }

  const perTable = new Map<string, number>();
  let shownNodes = 0;
  for (const [key, node] of nodes) {
    if (state.get(key) === "hidden") continue;
    const d = degree.get(key) ?? 0;
    let s: Shown = "shown";
    if (f.hideIsolated && d === 0) s = "hidden";
    else if (f.rules.some((r) => !matchesRule(node, d, r))) s = f.mode === "hide" ? "hidden" : "dimmed";
    else if (f.search.trim() && !matchesSearch(node, f.search)) s = "dimmed";
    state.set(key, s);
    if (s === "hidden") continue;
    perTable.set(node.table, (perTable.get(node.table) ?? 0) + 1);
    if (s === "shown") shownNodes += 1;
  }

  const perType = new Map<string, number>();
  let shownEdges = 0;
  for (const [key, edge] of edges) {
    if (edgeState.get(key) === "hidden") continue;
    const a = state.get(edge.from) ?? "hidden";
    const b = state.get(edge.to) ?? "hidden";
    const s: Shown = a === "hidden" || b === "hidden" ? "hidden" : a === "dimmed" || b === "dimmed" ? "dimmed" : "shown";
    edgeState.set(key, s);
    if (s === "hidden") continue;
    perType.set(edge.type, (perType.get(edge.type) ?? 0) + 1);
    if (s === "shown") shownEdges += 1;
  }
  return { nodes: state, edges: edgeState, degree, shownNodes, shownEdges, perTable, perType };
}

// ---------------------------------------------------------------- fields
/** A field of the loaded rows: in how many, and with which kinds of value. */
export interface FieldStat {
  name: string;
  count: number;
  kinds: Partial<Record<CellKind, number>>;
}

/** Every field of the rows but ``id``, most common first, then A to Z. */
export function fieldStats(nodes: Iterable<GraphNode>): FieldStat[] {
  const stats = new Map<string, FieldStat>();
  for (const node of nodes) {
    for (const [name, value] of Object.entries(node.row)) {
      if (name === "id") continue;
      const stat = stats.get(name) ?? { name, count: 0, kinds: {} };
      stat.count += 1;
      const kind = cellKind(value);
      stat.kinds[kind] = (stat.kinds[kind] ?? 0) + 1;
      stats.set(name, stat);
    }
  }
  return [...stats.values()].sort((a, b) => b.count - a.count || a.name.localeCompare(b.name));
}

const LABEL_CANDIDATES = ["name", "title", "label", "username", "email"];

/** The field nodes are labeled by when none is chosen: name, title or label if most rows have it as text, else id. */
export function defaultLabelField(stats: FieldStat[], total: number): string {
  for (const name of LABEL_CANDIDATES) {
    const stat = stats.find((s) => s.name === name);
    if (stat && (stat.kinds.string ?? 0) >= total / 2) return name;
  }
  return "id";
}

/** The fields that can label nodes: id, then the fields that hold text or numbers in some row. */
export function labelFields(stats: FieldStat[]): string[] {
  return ["id", ...stats.filter((s) => (s.kinds.string ?? 0) + (s.kinds.number ?? 0) > 0).map((s) => s.name)];
}

/** The node's label: the field's text or number, falling back to its id. */
export function labelOf(node: GraphNode, field: string, max = 40): string {
  const value = field === "id" ? node.row.id : node.row[field];
  const text = typeof value === "string" ? value : typeof value === "number" ? String(value) : node.row.id;
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}

export type SizeBy = "uniform" | "degree";

/** A node's radius on screen: the same for all, or growing with the square root of its degree. */
export function nodeSize(degree: number, by: SizeBy): number {
  if (by === "uniform") return 6;
  return Math.min(20, 4 + 2 * Math.sqrt(degree));
}

/** What the view shows, as JSON to download: the nodes not hidden and the edges among them. */
export function exportView(nodes: Map<string, GraphNode>, edges: Map<string, GraphEdge>, view: View): { nodes: GraphNode[]; edges: GraphEdge[] } {
  return {
    nodes: [...nodes].filter(([key]) => view.nodes.get(key) !== "hidden").map(([, node]) => node),
    edges: [...edges].filter(([key]) => view.edges.get(key) !== "hidden").map(([, edge]) => edge),
  };
}
