// What the Workflows section (WorkflowsSection.tsx, Workflow*.tsx) needs that
// is not drawing: where the hash points, a spec as React Flow's nodes and
// edges and back, each node's outputs, the checks a new edge must pass, where
// a node with no place of its own goes, the next free node id, a trigger as
// its form, and the counts of a dry run or a run as words on the canvas.

import type { Edge, Node, XYPosition } from "@xyflow/react";
import { parseHash } from "../../shared/browser";
import { fmtInt, plural } from "../../shared/format";
import type {
  WorkflowEdge,
  WorkflowEdgeCount,
  WorkflowNode,
  WorkflowNodeCount,
  WorkflowNodeType,
  WorkflowRun,
  WorkflowSpec,
  WorkflowTrigger,
} from "../../shared/types";
import { isActive, POLL_ACTIVE_MS } from "../../shared/tasks";

// ------------------------------------------------------------------- hash
export type WorkflowLocation = { view: "list" } | { view: "new" } | { view: "edit"; id: string };

/** What "#/workflows", "#/workflows/new" and "#/workflows/<id>" point at. */
export function locationFromHash(hash: string): WorkflowLocation {
  const [section, id] = parseHash(hash).parts;
  if (section !== "workflows" || !id) return { view: "list" };
  return id === "new" ? { view: "new" } : { view: "edit", id };
}

export function workflowHash(loc: WorkflowLocation): string {
  switch (loc.view) {
    case "list":
      return "#/workflows";
    case "new":
      return "#/workflows/new";
    case "edit":
      return `#/workflows/${encodeURIComponent(loc.id)}`;
  }
}

// ------------------------------------------------------------------ graph
export type NodeKind = WorkflowNode["type"];
export type Settings = Record<string, unknown>;

/** A node on the canvas: its type and settings; its id and place are React Flow's own. */
export interface StepData extends Record<string, unknown> {
  type: NodeKind;
  settings: Settings;
}
export type StepNode = Node<StepData, "step">;
/** An edge on the canvas: the output of the source node it leaves from is its source handle. */
export type StepEdge = Edge;

/** The outputs of a node of ``type``: the type's own, or a Switch's (null), a number per case and then default. */
export function outputsOf(type: Pick<WorkflowNodeType, "outputs"> | undefined, settings: Settings): string[] {
  if (!type) return [];
  if (type.outputs) return type.outputs;
  const cases = Array.isArray(settings.cases) ? settings.cases.length : 0;
  return [...Array.from({ length: cases }, (_, i) => String(i)), "default"];
}

const edgeId = (source: string, output: string, target: string) => `${source}:${output}>${target}`;

export const flowEdge = (source: string, output: string, target: string): StepEdge => ({
  id: edgeId(source, output, target),
  source,
  sourceHandle: output,
  target,
});

/** A spec's nodes and edges as the canvas draws them; a node without a place in ``ui`` gets one (placeNodes). */
export function toFlow(spec: Pick<WorkflowSpec, "nodes" | "edges">): { nodes: StepNode[]; edges: StepEdge[] } {
  const places = placeNodes(spec);
  return {
    nodes: spec.nodes.map((n) => ({ id: n.id, type: "step", position: places[n.id], data: { type: n.type, settings: n.settings ?? {} } })),
    edges: (spec.edges ?? []).map((e) => flowEdge(e.source, e.output ?? "out", e.target)),
  };
}

/** The canvas back as a spec's nodes and edges, each node's place (in whole pixels) in its ``ui``. */
export function fromFlow(nodes: StepNode[], edges: StepEdge[]): { nodes: WorkflowNode[]; edges: WorkflowEdge[] } {
  return {
    nodes: nodes.map((n) => ({
      id: n.id,
      type: n.data.type,
      settings: n.data.settings,
      ui: { x: Math.round(n.position.x), y: Math.round(n.position.y) },
    })),
    edges: edges.map((e) => ({ source: e.source, output: e.sourceHandle ?? "out", target: e.target })),
  };
}

/** How far apart placeNodes puts the columns and the rows. */
export const COLUMN = 280;
export const ROW = 150;

function placeOf(ui: WorkflowNode["ui"]): XYPosition | null {
  return ui && typeof ui.x === "number" && typeof ui.y === "number" ? { x: ui.x, y: ui.y } : null;
}

/**
 * Each node's place: the one in its ``ui``, else a column by its depth (the longest way to it from a node nothing
 * leads to, so the source is the first column) and the next free row of that column.
 */
export function placeNodes(spec: Pick<WorkflowSpec, "nodes" | "edges">): Record<string, XYPosition> {
  const depth: Record<string, number> = Object.fromEntries(spec.nodes.map((n) => [n.id, 0]));
  const edges = (spec.edges ?? []).filter((e) => e.source in depth && e.target in depth);
  // One pass per node at most, so a cycle (which a save refuses) cannot keep it going.
  for (let pass = 0; pass < spec.nodes.length; pass++) {
    let moved = false;
    for (const e of edges) {
      if (depth[e.target] < depth[e.source] + 1) {
        depth[e.target] = depth[e.source] + 1;
        moved = true;
      }
    }
    if (!moved) break;
  }
  const rows: Record<number, number> = {};
  return Object.fromEntries(
    spec.nodes.map((n) => {
      const kept = placeOf(n.ui);
      if (kept) return [n.id, kept];
      const d = depth[n.id];
      const row = rows[d] ?? 0;
      rows[d] = row + 1;
      return [n.id, { x: d * COLUMN, y: row * ROW }];
    }),
  );
}

/** ``at``, or the first place under it (a row at a time) where no node is: a node added there covers none. */
export function freePlace(taken: XYPosition[], at: XYPosition): XYPosition {
  const near = (p: XYPosition) => taken.some((t) => Math.abs(t.x - p.x) < COLUMN / 2 && Math.abs(t.y - p.y) < ROW / 2);
  let place = at;
  while (near(place)) place = { x: place.x, y: place.y + ROW };
  return place;
}

/** A, B, ... Z, then AA, AB...: the ``n``th node id. */
function letters(n: number): string {
  let id = "";
  for (let k = n + 1; k > 0; k = Math.floor((k - 1) / 26)) id = String.fromCharCode(65 + ((k - 1) % 26)) + id;
  return id;
}

/** The first of A, B, ... Z, AA... that no node has. */
export function nextNodeId(taken: Iterable<string>): string {
  const used = new Set(taken);
  for (let n = 0; ; n++) if (!used.has(letters(n))) return letters(n);
}

/** Whether the edges lead from ``from`` to ``to``. */
export function reaches(edges: Pick<StepEdge, "source" | "target">[], from: string, to: string): boolean {
  const seen = new Set<string>();
  const stack = [from];
  while (stack.length) {
    const id = stack.pop()!;
    if (id === to) return true;
    if (seen.has(id)) continue;
    seen.add(id);
    for (const e of edges) if (e.source === id) stack.push(e.target);
  }
  return false;
}

/** A connection being drawn: React Flow's, from an output handle to a node. */
export interface Link {
  source: string | null;
  sourceHandle?: string | null;
  target: string | null;
}

/** Why the edge ``link`` would draw may not be, or null when it may: into the source, there twice, or round in a circle. */
export function linkProblem(link: Link, nodes: StepNode[], edges: StepEdge[], types: Record<string, WorkflowNodeType>): string | null {
  const { source, target } = link;
  const output = link.sourceHandle ?? "out";
  if (!source || !target) return "An edge joins two nodes";
  if (source === target) return "A node cannot lead to itself";
  const into = nodes.find((n) => n.id === target);
  if (into && types[into.data.type]?.source) return `${target} reads the table: nothing goes into it`;
  if (edges.some((e) => e.source === source && (e.sourceHandle ?? "out") === output && e.target === target)) return "That edge is there already";
  if (reaches(edges, target, source)) return "That edge would go round in a circle";
  return null;
}

/**
 * The edges after Switch ``id``'s cases went from ``before`` to ``after``. An output is a case's number, so each
 * case's edges follow it, found by its text, to its new number, and a case removed takes its edges with it. When
 * the cases left whose text is new are as many as those gone (a case being edited), they keep their places.
 * ``default`` stays as it is.
 */
export function rewireCases(edges: StepEdge[], id: string, before: string[], after: string[]): StepEdge[] {
  const moved = new Map<string, string>();
  const free = new Set(after.keys());
  const gone: number[] = [];
  before.forEach((text, i) => {
    const j = after.findIndex((t, k) => t === text && free.has(k));
    if (j < 0) gone.push(i);
    else {
      moved.set(String(i), String(j));
      free.delete(j);
    }
  });
  if (gone.length === free.size) [...free].forEach((j, n) => moved.set(String(gone[n]), String(j)));
  return edges.flatMap((e) => {
    const output = e.sourceHandle ?? "out";
    if (e.source !== id || output === "default") return [e];
    const to = moved.get(output);
    return to === undefined ? [] : [{ ...e, ...flowEdge(id, to, e.target) }];
  });
}

/** A Switch's cases, as its settings hold them. */
export const casesOf = (settings: Settings): string[] => (Array.isArray(settings.cases) ? settings.cases.map(String) : []);

/** What a node shows under its name: the table, the model, the condition... */
export function nodeSummary(type: NodeKind, settings: Settings): string {
  const text = (key: string) => (typeof settings[key] === "string" ? (settings[key] as string) : "");
  const keys = (key: string) => Object.keys((settings[key] as object | undefined) ?? {}).join(", ");
  switch (type) {
    case "table.read":
    case "table.write":
      return [text("workspace"), text("table")].filter(Boolean).join(" / ");
    case "task":
      return text("model") || text("task");
    case "enrich":
      return text("value");
    case "if":
      return text("condition");
    case "switch":
      return Array.isArray(settings.cases) ? plural(settings.cases.length, "case") : "";
    case "set":
      return keys("fields");
    case "sql":
      return text("query");
    default:
      return "";
  }
}

// --------------------------------------------------------------- the spec
export type TriggerKind = "none" | "cron" | "interval";

/** The trigger as its fields edit it: text, so each is a plain input. */
export interface TriggerForm {
  kind: TriggerKind;
  cron: string;
  /** Seconds */
  interval: string;
}

export function triggerForm(trigger: WorkflowTrigger | null | undefined): TriggerForm {
  if (trigger?.cron) return { kind: "cron", cron: trigger.cron, interval: "3600" };
  if (trigger?.interval_s) return { kind: "interval", cron: "", interval: String(trigger.interval_s) };
  return { kind: "none", cron: "", interval: "3600" };
}

/** The trigger a form says; the server says what is wrong with it (a cron that does not parse, under 60 seconds). */
export function triggerOf(form: TriggerForm): WorkflowTrigger | null {
  if (form.kind === "cron") return { cron: form.cron.trim() };
  if (form.kind === "interval") return { interval_s: Number(form.interval) };
  return null;
}

/** The fields of a workflow other than its graph, as the editor holds them. */
export interface WorkflowFields {
  name: string;
  description: string;
  page_size: string;
  trigger: TriggerForm;
  /** Whether a client key may invoke it. */
  invokable: boolean;
}

export function fieldsOf(spec: Partial<WorkflowSpec>): WorkflowFields {
  return {
    name: spec.name ?? "",
    description: spec.description ?? "",
    page_size: String(spec.page_size ?? 100),
    trigger: triggerForm(spec.trigger),
    invokable: spec.invokable ?? false,
  };
}

/** The spec the editor holds, as POST and PUT take it. */
export function specOf(fields: WorkflowFields, nodes: StepNode[], edges: StepEdge[]): WorkflowSpec {
  return {
    name: fields.name.trim(),
    description: fields.description,
    page_size: Number(fields.page_size),
    trigger: triggerOf(fields.trigger),
    invokable: fields.invokable,
    ...fromFlow(nodes, edges),
  };
}

/** A new workflow: its source, waiting for a table. */
export const NEW_SPEC: WorkflowSpec = { name: "", nodes: [{ id: "A", type: "table.read", settings: {} }], edges: [] };

/** Whether a run reads only the rows new since the last: then a reset makes the next read them all. */
export const readsOnlyNew = (spec: Pick<WorkflowSpec, "nodes">): boolean =>
  spec.nodes.some((n) => n.type === "table.read" && n.settings?.only_new === true);

// ----------------------------------------------------------------- wiring
type Wire = Pick<StepEdge, "source" | "target" | "sourceHandle">;

/** The nodes on a way to ``id``. */
export function nodesBefore(id: string, edges: Wire[]): Set<string> {
  const seen = new Set<string>();
  const waiting = edges.filter((e) => e.target === id).map((e) => e.source);
  while (waiting.length) {
    const source = waiting.pop()!;
    if (seen.has(source)) continue;
    seen.add(source);
    for (const e of edges) if (e.target === source) waiting.push(e.source);
  }
  return seen;
}

/**
 * What node ``id``'s expressions can read of the results before it, as the save checks them: each field of a result
 * (``B.label``), or the result whole where its fields are not known before it runs (``gives`` null: an Enrich
 * node's), and ``_error.message`` behind an error output. The source table's own fields are not listed.
 */
export function readable(
  id: string,
  nodes: Pick<StepNode, "id" | "data">[],
  edges: Wire[],
  types: Record<string, Pick<WorkflowNodeType, "result">>,
  gives: (node: Pick<StepNode, "id" | "data">) => string[] | null,
): string[] {
  const before = nodesBefore(id, edges);
  const out: string[] = [];
  for (const n of nodes) {
    if (!before.has(n.id) || !types[n.data.type]?.result) continue;
    const fields = gives(n);
    out.push(...(fields ? fields.map((f) => `${n.id}.${f}`) : [n.id]));
  }
  const failed = edges.some((e) => e.sourceHandle === "error" && (e.target === id || before.has(e.target)));
  return failed ? [...out, "_error.message"] : out;
}

/** A task's inputs, from its input schema: each with whether the task needs it and whether the node gives it. */
export function taskInputs(
  input: { properties?: Record<string, unknown>; required?: string[] } | null | undefined,
  settings: Settings,
): { name: string; required: boolean; given: boolean }[] {
  const given = { ...(settings.inputs as Settings | undefined), ...(settings.params as Settings | undefined) };
  const required = new Set(input?.required ?? []);
  return Object.keys(input?.properties ?? {}).map((name) => ({ name, required: required.has(name), given: name in given }));
}

// ----------------------------------------------------------------- counts
/** The rows along each edge and through each node: of a dry run, or of a run. */
export interface Counts {
  edges: WorkflowEdgeCount[];
  nodes: Record<string, WorkflowNodeCount>;
}

/** An edge's label: the rows that went along it ("0 rows" for none); none without counts. */
export function edgeLabel(counts: Counts | null, edge: Pick<StepEdge, "source" | "sourceHandle" | "target">): string | undefined {
  if (!counts) return undefined;
  const output = edge.sourceHandle ?? "out";
  const hit = counts.edges.find((c) => c.source === edge.source && c.output === output && c.target === edge.target);
  return plural(hit?.rows ?? 0, "row");
}

/** A node's badge: its rows in and out, those that failed, and those a Table write wrote. */
export function nodeBadge(count: WorkflowNodeCount): string {
  const parts = [`${fmtInt(count.rows_in)} in`, `${fmtInt(count.rows_out)} out`];
  if (count.failed) parts.push(`${fmtInt(count.failed)} failed`);
  if (count.written != null) parts.push(`${fmtInt(count.written)} written`);
  return parts.join(" · ");
}

/** The runs are polled while one is queued or running, else not (a run starts from here or on its trigger). */
export const runsPoll = (runs: Pick<WorkflowRun, "status">[] | undefined): number => (runs?.some(isActive) ? POLL_ACTIVE_MS : 0);
