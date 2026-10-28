import { describe, expect, it } from "vitest";
import {
  computeView,
  defaultLabelField,
  describeRule,
  edgeKey,
  exportView,
  fieldStats,
  graphFromRows,
  graphQuery,
  labelFields,
  labelOf,
  matchesRule,
  matchesSearch,
  nodeKey,
  nodeSize,
  parseValue,
  splitKey,
  tableColor,
  validRule,
  type GraphEdge,
  type GraphNode,
  type Rule,
  type ViewFilters,
} from "./graph";

const ann: GraphNode = { table: "people", row: { id: "ann", name: "Ann", age: 41, tags: ["lead", "eng"], address: { city: "Oslo" } } };
const bob: GraphNode = { table: "people", row: { id: "bob", name: "Bob", age: 29, active: true } };
const cy: GraphNode = { table: "people", row: { id: "cy", name: null, age: "n/a" } };
const acme: GraphNode = { table: "companies", row: { id: "acme", name: "Acme" } };
const nodes = new Map([ann, bob, cy, acme].map((n) => [nodeKey(n.table, n.row.id), n]));
const edgeList: GraphEdge[] = [
  { type: "knows", from: "people/ann", to: "people/bob", since: 2020 },
  { type: "knows", from: "people/bob", to: "people/cy" },
  { type: "works_at", from: "people/ann", to: "companies/acme" },
  { type: "works_at", from: "people/cy", to: "companies/acme" },
];
const edges = new Map(edgeList.map((e) => [edgeKey(e), e]));
const all: ViewFilters = {
  tables: new Set(["people", "companies"]),
  types: new Set(["knows", "works_at"]),
  rules: [],
  search: "",
  mode: "dim",
  hideIsolated: false,
};
const rule = (field: string, op: Rule["op"], value = "", table: string | null = null): Rule => ({ table, field, op, value });

describe("keys and queries", () => {
  it("names nodes as edges name their ends", () => {
    expect(nodeKey("people", "ann")).toBe("people/ann");
    expect(splitKey("people/a/b")).toEqual(["people", "a/b"]);
    expect(splitKey("people")).toEqual(["people", ""]);
    expect(edgeKey(edgeList[0])).toBe("knows people/ann people/bob");
  });

  it("asks for the whole graph, eight edges a node up to 20000", () => {
    expect(graphQuery(500)).toEqual({ _limit: 500, _edge_limit: 4000 });
    expect(graphQuery(2500)).toEqual({ _limit: 2500, _edge_limit: 20000 });
  });

  it("draws the rows and edges a graph query returns, wherever they are", () => {
    const n = { table: "people", row: ann.row };
    const e = { type: "works_at", from: "people/ann", to: "companies/acme", role: "CTO" };
    const path = [{ table: "people", row: bob.row }, edgeList[1], { table: "people", row: cy.row }];
    const drawn = graphFromRows([{ n, e, m: null, count: 3 }, { n, path, other: { nested: [n] } }, { name: "Ann" }], ["people", "companies"]);
    expect(drawn.nodes).toEqual([ann, bob, cy, { table: "companies", row: { id: "acme" } }]);
    expect(drawn.edges).toEqual([e, edgeList[1]]);
    expect(drawn.tables).toEqual(["people", "companies"]);
    // A map that happens to hold "table" and "row" is a row only for a graph table of the namespace.
    expect(graphFromRows([{ x: { table: "notes", row: { id: "n1" } } }], ["people"]).nodes).toEqual([]);
    // A row's own field called "table" is the row's, not its table.
    expect(graphFromRows([{ p: { table: "people", row: { id: "b", table: "tags" } } }], ["people", "tags"]).nodes).toEqual([
      { table: "people", row: { id: "b", table: "tags" } },
    ]);
    expect(graphFromRows([{ name: "Ann", age: 30 }], ["people"])).toEqual({ nodes: [], edges: [], tables: [], outside: 0, more: { nodes: false, edges: false } });
  });
});

describe("values and rules", () => {
  it("reads values as the table filters do", () => {
    expect(parseValue("30")).toBe(30);
    expect(parseValue("-2.5")).toBe(-2.5);
    expect(parseValue("true")).toBe(true);
    expect(parseValue("null")).toBe(null);
    expect(parseValue('"30"')).toBe("30");
    expect(parseValue("Ann")).toBe("Ann");
    expect(parseValue("  ")).toBe("");
  });

  it("compares numbers as numbers and text as text", () => {
    expect(matchesRule(ann, 0, rule("age", ">=", "41"))).toBe(true);
    expect(matchesRule(ann, 0, rule("age", ">", "41"))).toBe(false);
    expect(matchesRule(bob, 0, rule("age", "<", "30"))).toBe(true);
    // "n/a" is not a number: it never compares.
    expect(matchesRule(cy, 0, rule("age", "<", "30"))).toBe(false);
    expect(matchesRule(ann, 0, rule("name", ">=", "B"))).toBe(false);
    expect(matchesRule(bob, 0, rule("name", ">=", "B"))).toBe(true);
    expect(matchesRule(ann, 0, rule("age", "=", "41"))).toBe(true);
    expect(matchesRule(ann, 0, rule("age", "=", '"41"'))).toBe(true);
    expect(matchesRule(bob, 0, rule("active", "=", "true"))).toBe(true);
    expect(matchesRule(ann, 0, rule("name", "!=", "Bob"))).toBe(true);
  });

  it("looks inside lists, objects and text for contains", () => {
    expect(matchesRule(ann, 0, rule("tags", "contains", "lead"))).toBe(true);
    expect(matchesRule(ann, 0, rule("tags", "=", "eng"))).toBe(true);
    expect(matchesRule(ann, 0, rule("tags", "contains", "ops"))).toBe(false);
    expect(matchesRule(ann, 0, rule("name", "contains", "an"))).toBe(true);
    expect(matchesRule(ann, 0, rule("address", "contains", "oslo"))).toBe(true);
  });

  it("knows which rows have a field, and treats null as missing", () => {
    expect(matchesRule(ann, 0, rule("tags", "exists"))).toBe(true);
    expect(matchesRule(bob, 0, rule("tags", "exists"))).toBe(false);
    expect(matchesRule(bob, 0, rule("tags", "missing"))).toBe(true);
    expect(matchesRule(cy, 0, rule("name", "missing"))).toBe(true);
    expect(matchesRule(cy, 0, rule("name", "=", "null"))).toBe(true);
    expect(matchesRule(ann, 0, rule("name", "=", "null"))).toBe(false);
    // A missing value fails every comparison but "not equal".
    expect(matchesRule(bob, 0, rule("tags", ">", "1"))).toBe(false);
    expect(matchesRule(bob, 0, rule("tags", "!=", "x"))).toBe(true);
  });

  it("has the table and the degree as fields, and scopes a rule to a table", () => {
    expect(matchesRule(ann, 3, rule("degree", ">=", "2"))).toBe(true);
    expect(matchesRule(ann, 1, rule("degree", ">=", "2"))).toBe(false);
    expect(matchesRule(acme, 0, rule("table", "=", "companies"))).toBe(true);
    expect(matchesRule(ann, 0, rule("id", "contains", "an"))).toBe(true);
    // Scoped to companies, the rule says nothing about a person.
    expect(matchesRule(ann, 0, rule("age", ">", "100", "companies"))).toBe(true);
    expect(matchesRule(acme, 0, rule("age", ">", "100", "companies"))).toBe(false);
  });

  it("describes and validates a rule", () => {
    expect(describeRule(rule("age", ">=", "30"))).toBe("age ≥ 30");
    expect(describeRule(rule("name", "contains", "ann", "people"))).toBe("people · name contains ann");
    expect(describeRule(rule("email", "missing"))).toBe("email missing");
    expect(validRule(rule("age", ">=", ""))).toBe(false);
    expect(validRule(rule("age", "exists"))).toBe(true);
    expect(validRule(rule("", "exists"))).toBe(false);
  });

  it("searches ids, tables and plain values, not case-sensitive", () => {
    expect(matchesSearch(ann, "ANN")).toBe(true);
    expect(matchesSearch(ann, "41")).toBe(true);
    expect(matchesSearch(ann, "lead")).toBe(true);
    expect(matchesSearch(ann, "oslo")).toBe(false);
    expect(matchesSearch(acme, "compan")).toBe(true);
    expect(matchesSearch(bob, "  ")).toBe(true);
  });
});

describe("the view", () => {
  it("shows everything when nothing filters", () => {
    const v = computeView(nodes, edges, all);
    expect([...v.nodes.values()].every((s) => s === "shown")).toBe(true);
    expect(v.shownNodes).toBe(4);
    expect(v.shownEdges).toBe(4);
    expect(v.degree.get("people/ann")).toBe(2);
    expect(v.degree.get("companies/acme")).toBe(2);
    expect(v.perTable.get("people")).toBe(3);
    expect(v.perType.get("works_at")).toBe(2);
  });

  it("hides a table switched off with the edges to it, and an edge type with its edges", () => {
    const v = computeView(nodes, edges, { ...all, tables: new Set(["people"]) });
    expect(v.nodes.get("companies/acme")).toBe("hidden");
    expect(v.edges.get("works_at people/ann companies/acme")).toBe("hidden");
    expect(v.shownEdges).toBe(2);
    expect(v.degree.get("people/ann")).toBe(1);
    const w = computeView(nodes, edges, { ...all, types: new Set(["works_at"]) });
    expect(w.edges.get("knows people/ann people/bob")).toBe("hidden");
    expect(w.degree.get("people/bob")).toBe(0);
    expect(w.perType.has("knows")).toBe(false);
  });

  it("dims or hides the nodes failing a rule, and their edges with them", () => {
    const dim = computeView(nodes, edges, { ...all, rules: [rule("age", ">=", "30")] });
    expect(dim.nodes.get("people/ann")).toBe("shown");
    expect(dim.nodes.get("people/bob")).toBe("dimmed");
    expect(dim.nodes.get("people/cy")).toBe("dimmed");
    expect(dim.edges.get("knows people/ann people/bob")).toBe("dimmed");
    // A company has no age, so an unscoped rule dims it too; scoped to people, it leaves companies alone.
    expect(dim.nodes.get("companies/acme")).toBe("dimmed");
    expect(dim.edges.get("works_at people/ann companies/acme")).toBe("dimmed");
    expect(dim.shownNodes).toBe(1);
    expect(dim.perTable.get("people")).toBe(3);
    const scoped = computeView(nodes, edges, { ...all, rules: [rule("age", ">=", "30", "people")] });
    expect(scoped.nodes.get("companies/acme")).toBe("shown");
    expect(scoped.edges.get("works_at people/ann companies/acme")).toBe("shown");
    expect(scoped.shownNodes).toBe(2);
    const hide = computeView(nodes, edges, { ...all, rules: [rule("age", ">=", "30", "people")], mode: "hide" });
    expect(hide.nodes.get("people/bob")).toBe("hidden");
    expect(hide.edges.get("knows people/ann people/bob")).toBe("hidden");
    expect(hide.perTable.get("people")).toBe(1);
    expect(hide.shownEdges).toBe(1);
  });

  it("drops isolated nodes when asked, counting the edges left", () => {
    const v = computeView(nodes, edges, { ...all, types: new Set(["works_at"]), hideIsolated: true });
    expect(v.nodes.get("people/bob")).toBe("hidden");
    expect(v.nodes.get("people/ann")).toBe("shown");
    expect(v.shownNodes).toBe(3);
  });

  it("only dims what the search misses", () => {
    const v = computeView(nodes, edges, { ...all, search: "acme", mode: "hide" });
    expect(v.nodes.get("companies/acme")).toBe("shown");
    expect(v.nodes.get("people/ann")).toBe("dimmed");
    expect(v.shownNodes).toBe(1);
  });

  it("exports what is not hidden", () => {
    const v = computeView(nodes, edges, { ...all, tables: new Set(["people"]), rules: [rule("age", ">=", "30")] });
    const out = exportView(nodes, edges, v);
    expect(out.nodes.map((n) => n.row.id)).toEqual(["ann", "bob", "cy"]);
    expect(out.edges.map((e) => e.type)).toEqual(["knows", "knows"]);
  });
});

describe("labels, sizes and colors", () => {
  it("counts the fields of the rows", () => {
    const stats = fieldStats(nodes.values());
    expect(stats[0]).toEqual({ name: "name", count: 4, kinds: { string: 3, null: 1 } });
    expect(stats.map((s) => s.name)).toEqual(["name", "age", "active", "address", "tags"]);
    expect(labelFields(stats)).toEqual(["id", "name", "age"]);
  });

  it("labels by name when most rows have one, else by id", () => {
    const stats = fieldStats(nodes.values());
    expect(defaultLabelField(stats, 4)).toBe("name");
    expect(defaultLabelField(fieldStats([cy]), 1)).toBe("id");
    expect(labelOf(ann, "name")).toBe("Ann");
    expect(labelOf(cy, "name")).toBe("cy");
    expect(labelOf(ann, "age")).toBe("41");
    expect(labelOf({ table: "t", row: { id: "x", name: "a".repeat(50) } }, "name", 10)).toBe("aaaaaaaaa…");
  });

  it("sizes by degree within bounds", () => {
    expect(nodeSize(0, "uniform")).toBe(6);
    expect(nodeSize(100, "uniform")).toBe(6);
    expect(nodeSize(0, "degree")).toBe(4);
    expect(nodeSize(4, "degree")).toBe(8);
    expect(nodeSize(1000, "degree")).toBe(20);
  });

  it("gives tables fixed colors, gray past the palette", () => {
    expect(tableColor(0, false)).toBe("#2a78d6");
    expect(tableColor(0, true)).toBe("#3987e5");
    expect(tableColor(8, false)).toBe("#8e8e99");
    expect(tableColor(-1, true)).toBe("#73737d");
  });
});
