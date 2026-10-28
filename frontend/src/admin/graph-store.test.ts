import { describe, expect, it } from "vitest";
import { GraphStore, withAll } from "./graph-store";
import type { GraphEdge, GraphNode, GraphResponse, Neighbor } from "./graph";

const ann: GraphNode = { table: "people", row: { id: "ann", name: "Ann" } };
const bob: GraphNode = { table: "people", row: { id: "bob", name: "Bob" } };
const acme: GraphNode = { table: "companies", row: { id: "acme", name: "Acme" } };
const knows: GraphEdge = { type: "knows", from: "people/ann", to: "people/bob" };
const works: GraphEdge = { type: "works_at", from: "people/ann", to: "companies/acme" };
const loaded: GraphResponse = { tables: ["people"], nodes: [ann, bob, ann], edges: [knows, works, knows], outside: 0, more: { nodes: false, edges: false } };

/** New neighbors land to the right of the node they expand. */
const store = () => new GraphStore(() => 0);

describe("GraphStore.rebuild", () => {
  it("keeps each node once, places them on a circle, and only the edges among them", () => {
    const s = store();
    s.rebuild(loaded);
    expect([...s.nodes.keys()]).toEqual(["people/ann", "people/bob"]);
    expect([...s.edges.keys()]).toEqual(["knows people/ann people/bob"]);
    expect(s.graph.order).toBe(2);
    expect(s.graph.size).toBe(1);
    const { x, y } = s.graph.getNodeAttributes("people/ann");
    expect(Math.hypot(x, y)).toBeCloseTo(100);
  });

  it("drops what was there before", () => {
    const s = store();
    s.rebuild(loaded);
    s.rebuild({ ...loaded, nodes: [acme], edges: [] });
    expect([...s.nodes.keys()]).toEqual(["companies/acme"]);
    expect(s.edges.size).toBe(0);
    expect(s.graph.order).toBe(1);
  });
});

describe("GraphStore.addNode / addEdge", () => {
  it("puts a new node near the one given, or at the origin when that one is not loaded", () => {
    const s = store();
    s.rebuild(loaded);
    const at = s.graph.getNodeAttributes("people/ann");
    expect(s.addNode(acme, "people/ann")).toBe(true);
    expect(s.graph.getNodeAttributes("companies/acme")).toEqual({ x: at.x + 12, y: at.y, table: "companies" });
    expect(s.addNode({ table: "t", row: { id: "x" } }, "nowhere/0")).toBe(true);
    expect(s.graph.getNodeAttributes("t/x")).toEqual({ x: 12, y: 0, table: "t" });
  });

  it("refuses a node already loaded, an edge already loaded, and an edge to a row not loaded", () => {
    const s = store();
    s.rebuild(loaded);
    expect(s.addNode(ann)).toBe(false);
    expect(s.addEdge(knows)).toBe(false);
    expect(s.addEdge(works)).toBe(false);
    expect(s.addNode(acme)).toBe(true);
    expect(s.addEdge(works)).toBe(true);
    expect(s.graph.getEdgeAttributes("works_at people/ann companies/acme")).toEqual({ kind: "works_at" });
  });
});

describe("GraphStore.addNeighbors", () => {
  it("counts what was new, the tables of the nodes added and every edge type seen", () => {
    const s = store();
    s.rebuild(loaded);
    const neighbors: Neighbor[] = [
      { table: "people", row: bob.row, edge: knows },
      { table: "companies", row: acme.row, edge: works },
      { table: "people", row: null, edge: { type: "manages", from: "people/gone", to: "people/ann" } },
    ];
    const added = s.addNeighbors("people/ann", neighbors);
    expect(added).toEqual({ nodes: 1, edges: 1, tables: new Set(["companies"]), types: new Set(["knows", "works_at", "manages"]) });
    expect(s.nodes.size).toBe(3);
    expect(s.edges.size).toBe(2);
  });
});

describe("GraphStore.dismiss", () => {
  it("takes the node and its edges out; a node not loaded is nothing", () => {
    const s = store();
    s.rebuild(loaded);
    expect(s.dismiss("people/ann")).toBe(true);
    expect(s.nodes.has("people/ann")).toBe(false);
    expect(s.edges.size).toBe(0);
    expect(s.graph.order).toBe(1);
    expect(s.dismiss("people/ann")).toBe(false);
  });
});

describe("GraphStore counts", () => {
  it("lists the edges touching a node, and how many nodes and edges each table and type has", () => {
    const s = store();
    s.rebuild({ ...loaded, nodes: [ann, bob, acme], edges: [knows, works] });
    expect(s.edgesOf("people/ann")).toEqual([knows, works]);
    expect(s.edgesOf("people/bob")).toEqual([knows]);
    expect([...s.perTable()]).toEqual([
      ["people", 2],
      ["companies", 1],
    ]);
    expect([...s.perType()]).toEqual([
      ["knows", 1],
      ["works_at", 1],
    ]);
  });
});

describe("withAll", () => {
  it("adds what is missing, and returns the same set when nothing is", () => {
    const set = new Set(["a"]);
    expect(withAll(set, ["a"])).toBe(set);
    expect(withAll(set, [])).toBe(set);
    const next = withAll(set, ["a", "b"]);
    expect(next).not.toBe(set);
    expect([...next]).toEqual(["a", "b"]);
    expect([...set]).toEqual(["a"]);
  });
});
