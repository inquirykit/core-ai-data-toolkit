import { describe, expect, it } from "vitest";
import type { WorkflowNodeType, WorkflowSpec } from "../../shared/types";
import {
  COLUMN,
  edgeLabel,
  fieldsOf,
  flowEdge,
  freePlace,
  fromFlow,
  linkProblem,
  locationFromHash,
  nextNodeId,
  nodeBadge,
  type NodeKind,
  nodeSummary,
  outputsOf,
  nodesBefore,
  placeNodes,
  readable,
  readsOnlyNew,
  rewireCases,
  ROW,
  runsPoll,
  specOf,
  taskInputs,
  toFlow,
  triggerForm,
  triggerOf,
  workflowHash,
  type StepEdge,
} from "./workflows";

const type = (t: string, outputs: string[] | null, source = false, result = false): WorkflowNodeType => ({
  type: t,
  label: t,
  description: "",
  source,
  result,
  outputs,
  settings: {},
});
const TYPES: Record<string, WorkflowNodeType> = {
  "table.read": type("table.read", ["out"], true),
  task: type("task", ["out", "error"]),
  switch: type("switch", null),
  "table.write": type("table.write", []),
};

// The guide's example, less one write.
const SPEC: WorkflowSpec = {
  name: "Tag tickets",
  nodes: [
    { id: "A", type: "table.read", settings: { workspace: "support", table: "tickets" } },
    { id: "B", type: "task", settings: { task: "text-classification", model: "m", inputs: { text: "body" } } },
    { id: "S", type: "switch", settings: { cases: ["B.label == 'SPAM'"] } },
    { id: "T", type: "table.write", settings: { workspace: "support", table: "trash" } },
    { id: "F", type: "table.write", settings: { workspace: "support", table: "failed" }, ui: { x: 900, y: 40 } },
  ],
  edges: [
    { source: "A", target: "B" },
    { source: "B", output: "out", target: "S" },
    { source: "B", output: "error", target: "F" },
    { source: "S", output: "0", target: "T" },
  ],
};

describe("the hash", () => {
  it("names the list, a new workflow or one by id, and back", () => {
    expect(locationFromHash("#/workflows")).toEqual({ view: "list" });
    expect(locationFromHash("#/workflows/new")).toEqual({ view: "new" });
    expect(locationFromHash("#/workflows/ab12cd34")).toEqual({ view: "edit", id: "ab12cd34" });
    expect(locationFromHash("#/jobs")).toEqual({ view: "list" });
    for (const loc of [{ view: "list" }, { view: "new" }, { view: "edit", id: "ab12cd34" }] as const) {
      expect(locationFromHash(workflowHash(loc))).toEqual(loc);
    }
  });
});

describe("outputsOf", () => {
  it("is the type's outputs, or a Switch's cases by number and then default", () => {
    expect(outputsOf(TYPES.task, {})).toEqual(["out", "error"]);
    expect(outputsOf(TYPES["table.write"], {})).toEqual([]);
    expect(outputsOf(TYPES.switch, { cases: ["a", "b"] })).toEqual(["0", "1", "default"]);
    expect(outputsOf(TYPES.switch, {})).toEqual(["default"]);
    expect(outputsOf(undefined, {})).toEqual([]);
  });
});

describe("toFlow and fromFlow", () => {
  it("give back the spec's nodes and edges, each node with its place", () => {
    const { nodes, edges } = toFlow(SPEC);
    expect(nodes.map((n) => [n.id, n.type, n.data.type])).toEqual([
      ["A", "step", "table.read"],
      ["B", "step", "task"],
      ["S", "step", "switch"],
      ["T", "step", "table.write"],
      ["F", "step", "table.write"],
    ]);
    expect(edges[0]).toEqual({ id: "A:out>B", source: "A", sourceHandle: "out", target: "B" });
    const back = fromFlow(nodes, edges);
    expect(back.edges).toEqual(SPEC.edges!.map((e) => ({ output: "out", ...e })));
    expect(back.nodes.map(({ ui: _ui, ...n }) => n)).toEqual(SPEC.nodes.map(({ ui: _ui, ...n }) => n));
    expect(back.nodes.find((n) => n.id === "F")?.ui).toEqual({ x: 900, y: 40 });
  });

  it("keeps places in whole pixels", () => {
    const { nodes } = toFlow({ nodes: [{ id: "A", type: "table.read", settings: {}, ui: { x: 10.4, y: 20.6 } }] });
    expect(fromFlow(nodes, []).nodes[0].ui).toEqual({ x: 10, y: 21 });
  });
});

describe("placeNodes", () => {
  it("puts a node without a place in the column of its depth, the next free row", () => {
    const places = placeNodes(SPEC);
    expect(places.A).toEqual({ x: 0, y: 0 });
    expect(places.B).toEqual({ x: COLUMN, y: 0 });
    expect(places.S).toEqual({ x: 2 * COLUMN, y: 0 });
    expect(places.T).toEqual({ x: 3 * COLUMN, y: 0 });
    expect(places.F).toEqual({ x: 900, y: 40 });
  });

  it("goes by the longest way in, and stacks a column's nodes", () => {
    const places = placeNodes({
      nodes: ["A", "B", "C", "D"].map((id) => ({ id, type: "task" as const })),
      edges: [
        { source: "A", target: "B" },
        { source: "A", target: "C" },
        { source: "B", target: "D" },
        { source: "A", target: "D" },
      ],
    });
    expect(places).toEqual({ A: { x: 0, y: 0 }, B: { x: COLUMN, y: 0 }, C: { x: COLUMN, y: ROW }, D: { x: 2 * COLUMN, y: 0 } });
  });

  it("ends on a cycle", () => {
    const places = placeNodes({
      nodes: [{ id: "A", type: "task" }, { id: "B", type: "task" }],
      edges: [
        { source: "A", target: "B" },
        { source: "B", target: "A" },
      ],
    });
    expect(Object.keys(places)).toEqual(["A", "B"]);
  });
});

describe("freePlace", () => {
  it("is the place asked for when it is free, else the first free row under it", () => {
    expect(freePlace([{ x: 0, y: 0 }], { x: COLUMN, y: 0 })).toEqual({ x: COLUMN, y: 0 });
    expect(freePlace([{ x: COLUMN, y: 0 }, { x: COLUMN + 10, y: ROW }], { x: COLUMN, y: 0 })).toEqual({ x: COLUMN, y: 2 * ROW });
  });
});

describe("nextNodeId", () => {
  it("is the first free letter, then two", () => {
    expect(nextNodeId([])).toBe("A");
    expect(nextNodeId(["A", "B", "D"])).toBe("C");
    const alphabet = Array.from({ length: 26 }, (_, i) => String.fromCharCode(65 + i));
    expect(nextNodeId(alphabet)).toBe("AA");
    expect(nextNodeId([...alphabet, "AA"])).toBe("AB");
  });
});

describe("linkProblem", () => {
  const { nodes, edges } = toFlow(SPEC);
  const check = (source: string, sourceHandle: string, target: string) => linkProblem({ source, sourceHandle, target }, nodes, edges, TYPES);

  it("lets a new edge forward through", () => {
    expect(check("S", "default", "T")).toBeNull();
    expect(check("A", "out", "S")).toBeNull();
  });

  it("refuses an edge into the source, onto itself, twice or round in a circle", () => {
    expect(check("B", "out", "A")).toMatch(/reads the table/);
    expect(check("B", "out", "B")).toMatch(/itself/);
    expect(check("A", "out", "B")).toMatch(/already/);
    expect(check("S", "default", "B")).toMatch(/circle/);
  });
});

describe("rewireCases", () => {
  const edges = [flowEdge("S", "0", "Trash"), flowEdge("S", "1", "Billing"), flowEdge("S", "2", "Bugs"), flowEdge("S", "default", "Rest"), flowEdge("B", "1", "T")];
  const ids = (es: StepEdge[]) => es.map((e) => e.id);

  it("takes a removed case's edges with it, and the others follow their case", () => {
    const rewired = rewireCases(edges, "S", ["spam", "billing", "bug"], ["billing", "bug"]);
    expect(ids(rewired)).toEqual(["S:0>Billing", "S:1>Bugs", "S:default>Rest", "B:1>T"]);
    expect(rewired[0].sourceHandle).toBe("0");
  });

  it("keeps a case being edited where it is, and follows cases moved", () => {
    expect(ids(rewireCases(edges, "S", ["spam", "billing", "bug"], ["spam", "billin", "bug"]))).toEqual(ids(edges));
    expect(ids(rewireCases(edges, "S", ["spam", "billing", "bug"], ["bug", "spam", "billing"]))).toEqual(["S:1>Trash", "S:2>Billing", "S:0>Bugs", "S:default>Rest", "B:1>T"]);
    expect(ids(rewireCases(edges, "S", ["spam", "billing", "bug"], ["spam", "billing", "bug", ""]))).toEqual(ids(edges));
  });
});

describe("nodeSummary", () => {
  it("says what a node works on", () => {
    expect(nodeSummary("table.read", { workspace: "support", table: "tickets" })).toBe("support / tickets");
    expect(nodeSummary("task", { task: "t", model: "m" })).toBe("m");
    expect(nodeSummary("switch", { cases: ["a", "b"] })).toBe("2 cases");
    expect(nodeSummary("set", { fields: { a: "x", b: "y" } })).toBe("a, b");
    expect(nodeSummary("merge", {})).toBe("");
    expect(nodeSummary("sql", { query: "SELECT * FROM rows" })).toBe("SELECT * FROM rows");
  });
});

describe("the trigger", () => {
  it("goes to its form and back", () => {
    expect(triggerOf(triggerForm(null))).toBeNull();
    expect(triggerOf(triggerForm({ cron: "0 3 * * *" }))).toEqual({ cron: "0 3 * * *" });
    expect(triggerOf(triggerForm({ interval_s: 3600 }))).toEqual({ interval_s: 3600 });
    expect(triggerOf({ kind: "cron", cron: " 5 * * * * ", interval: "" })).toEqual({ cron: "5 * * * *" });
  });
});

describe("specOf", () => {
  it("is the fields and the canvas as a spec", () => {
    const { nodes, edges } = toFlow(SPEC);
    const spec = specOf({ ...fieldsOf(SPEC), name: " Tag tickets ", page_size: "50" }, nodes, edges);
    expect(spec).toMatchObject({ name: "Tag tickets", description: "", page_size: 50, trigger: null, invokable: false });
    expect(specOf({ ...fieldsOf({ ...SPEC, invokable: true }) }, nodes, edges).invokable).toBe(true);
    expect(spec.nodes).toHaveLength(5);
    expect(spec.edges).toHaveLength(4);
  });

  it("knows a source that reads only new rows", () => {
    expect(readsOnlyNew(SPEC)).toBe(false);
    expect(readsOnlyNew({ nodes: [{ id: "A", type: "table.read", settings: { only_new: true } }] })).toBe(true);
  });
});

describe("counts", () => {
  const counts = {
    edges: [
      { source: "A", output: "out", target: "B", rows: 20 },
      { source: "B", output: "error", target: "F", rows: 1 },
    ],
    nodes: { B: { rows_in: 20, rows_out: 19, failed: 1, error: "boom" }, T: { rows_in: 5, rows_out: 0, failed: 0, error: null, written: 5 } },
  };

  it("label an edge by its rows, 0 for one none went along, nothing without counts", () => {
    expect(edgeLabel(counts, flowEdge("A", "out", "B"))).toBe("20 rows");
    expect(edgeLabel(counts, flowEdge("B", "error", "F"))).toBe("1 row");
    expect(edgeLabel(counts, flowEdge("B", "out", "S"))).toBe("0 rows");
    expect(edgeLabel(null, flowEdge("A", "out", "B"))).toBeUndefined();
  });

  it("badge a node with its rows in, out, failed and written", () => {
    expect(nodeBadge(counts.nodes.B)).toBe("20 in · 19 out · 1 failed");
    expect(nodeBadge(counts.nodes.T)).toBe("5 in · 0 out · 5 written");
  });

  it("poll the runs only while one is queued or running", () => {
    expect(runsPoll([{ status: "succeeded" }])).toBe(0);
    expect(runsPoll([{ status: "succeeded" }, { status: "running" }])).toBeGreaterThan(0);
    expect(runsPoll(undefined)).toBe(0);
  });
});

describe("wiring", () => {
  // A reads the table, B classifies, E enriches, I branches on B, F takes B's failed rows, W writes.
  const step = (id: string, t: NodeKind, settings = {}) => ({ id, data: { type: t, settings } });
  const nodes = [step("A", "table.read"), step("B", "task", { task: "text-classification" }), step("E", "enrich"),
    step("I", "if"), step("F", "table.write"), step("W", "table.write")];
  const edges = [flowEdge("A", "out", "B"), flowEdge("B", "out", "I"), flowEdge("B", "error", "F"), flowEdge("I", "true", "E"),
    flowEdge("E", "out", "W")];
  const types = { task: { result: true }, enrich: { result: true }, if: { result: false }, "table.read": { result: false } };
  const gives = (n: { data: { type: string } }) => (n.data.type === "task" ? ["label", "confidence"] : null);

  it("find the nodes on a way to a node", () => {
    expect([...nodesBefore("W", edges)].sort()).toEqual(["A", "B", "E", "I"]);
    expect(nodesBefore("A", edges).size).toBe(0);
  });

  it("list the results a node can read: a task's fields, an enrichment whole, the error behind an error output", () => {
    expect(readable("I", nodes, edges, types, gives)).toEqual(["B.label", "B.confidence"]);
    expect(readable("W", nodes, edges, types, gives)).toEqual(["B.label", "B.confidence", "E"]);
    expect(readable("F", nodes, edges, types, gives)).toEqual(["B.label", "B.confidence", "_error.message"]);
    expect(readable("B", nodes, edges, types, gives)).toEqual([]);
  });

  it("say which of a task's inputs it needs and which the node gives", () => {
    const input = { properties: { question: {}, context: {}, top_k: {} }, required: ["question", "context"] };
    expect(taskInputs(input, { inputs: { question: "q" }, params: { top_k: 2 } })).toEqual([
      { name: "question", required: true, given: true },
      { name: "context", required: true, given: false },
      { name: "top_k", required: false, given: true },
    ]);
    expect(taskInputs(null, {})).toEqual([]);
  });
});
