// The graph view's loaded rows and edges, by key, beside the graphology graph
// that sigma draws (use-sigma.ts): starting over with a loaded graph, adding
// what an expansion brought, dismissing a node. GraphStore does this apart
// from React, so it can be tested without a DOM; useGraphStore binds one to a
// component, counting its changes in ``version`` so that what derives from
// the store (the filters' view, the legend's counts) is recomputed.
import { useMemo, useRef, useState } from "react";
import Graph from "graphology";
import { circular } from "graphology-layout";
import { edgeKey, nodeKey, type GraphEdge, type GraphNode, type GraphResponse, type Neighbor } from "./graph";

/** The attributes graphology holds per node; sigma reads x, y and the rest comes from the reducer. */
export interface NodeAttrs {
  x: number;
  y: number;
  table: string;
}
/** ``kind`` rather than ``type``: sigma reads ``type`` as the program drawing the edge. */
export interface EdgeAttrs {
  kind: string;
}
export type DrawnGraph = Graph<NodeAttrs, EdgeAttrs>;

/** What an expansion brought: the counts for its toast, and the tables and edge types it touched, to switch on. */
export interface Added {
  nodes: number;
  edges: number;
  /** The tables of the nodes added */
  tables: Set<string>;
  /** The types of every edge it came with, added or not */
  types: Set<string>;
}

/** How far from the node it expands a new neighbor lands, in graph units. */
const NEAR = 12;

export class GraphStore {
  nodes = new Map<string, GraphNode>();
  edges = new Map<string, GraphEdge>();
  readonly graph: DrawnGraph = new Graph({ multi: true, type: "directed" });

  /** ``random`` picks the direction a new neighbor lands in; a test fixes it. */
  constructor(private readonly random: () => number = Math.random) {}

  /** Starts over with a loaded graph: its nodes on a circle (the layout takes it from there), then the edges among them. */
  rebuild(data: GraphResponse): void {
    this.graph.clear();
    this.nodes = new Map();
    this.edges = new Map();
    for (const n of data.nodes) {
      const key = nodeKey(n.table, n.row.id);
      if (this.nodes.has(key)) continue;
      this.nodes.set(key, n);
      this.graph.addNode(key, { x: 0, y: 0, table: n.table });
    }
    circular.assign(this.graph, { scale: 100 });
    for (const e of data.edges) this.addEdge(e);
  }

  /** Adds a node, near ``around`` when that is loaded; false when the node is there already. */
  addNode(n: GraphNode, around?: string): boolean {
    const key = nodeKey(n.table, n.row.id);
    if (this.nodes.has(key)) return false;
    this.nodes.set(key, n);
    const at = around && this.graph.hasNode(around) ? this.graph.getNodeAttributes(around) : { x: 0, y: 0 };
    const angle = this.random() * 2 * Math.PI;
    this.graph.addNode(key, { x: at.x + NEAR * Math.cos(angle), y: at.y + NEAR * Math.sin(angle), table: n.table });
    return true;
  }

  /** Adds an edge; false when it is there already or either end is not loaded. */
  addEdge(e: GraphEdge): boolean {
    const key = edgeKey(e);
    if (this.edges.has(key) || !this.graph.hasNode(e.from) || !this.graph.hasNode(e.to)) return false;
    this.edges.set(key, e);
    this.graph.addEdgeWithKey(key, e.from, e.to, { kind: e.type });
    return true;
  }

  /** Adds a node's neighbors (the rows that still exist) near it, and the edges to them. */
  addNeighbors(around: string, neighbors: Neighbor[]): Added {
    const added: Added = { nodes: 0, edges: 0, tables: new Set(), types: new Set() };
    for (const n of neighbors) {
      if (n.row && this.addNode({ table: n.table, row: n.row }, around)) {
        added.nodes += 1;
        added.tables.add(n.table);
      }
      if (this.addEdge(n.edge)) added.edges += 1;
      added.types.add(n.edge.type);
    }
    return added;
  }

  /** Takes a node and its edges out (the row stays in its table); false when it was not loaded. */
  dismiss(key: string): boolean {
    if (!this.graph.hasNode(key)) return false;
    for (const edge of this.graph.edges(key)) this.edges.delete(edge);
    this.graph.dropNode(key);
    this.nodes.delete(key);
    return true;
  }

  /** The loaded edges touching a node. */
  edgesOf(key: string): GraphEdge[] {
    return [...this.edges.values()].filter((e) => e.from === key || e.to === key);
  }

  /** How many loaded nodes each table has, in the order the tables were seen. */
  perTable(): Map<string, number> {
    return count([...this.nodes.values()].map((n) => n.table));
  }

  /** How many loaded edges each type has, in the order the types were seen. */
  perType(): Map<string, number> {
    return count([...this.edges.values()].map((e) => e.type));
  }
}

function count(names: string[]): Map<string, number> {
  const counts = new Map<string, number>();
  for (const name of names) counts.set(name, (counts.get(name) ?? 0) + 1);
  return counts;
}

/** ``set`` with ``items`` added, or ``set`` itself when they were all in it: state that did not change is not set again. */
export function withAll(set: Set<string>, items: Iterable<string>): Set<string> {
  let next: Set<string> | null = null;
  for (const item of items) {
    if (set.has(item)) continue;
    next ??= new Set(set);
    next.add(item);
  }
  return next ?? set;
}

export interface GraphStoreHook {
  store: GraphStore;
  /** Bumped by each change made through the methods below. */
  version: number;
  rebuild(data: GraphResponse): void;
  addNode(n: GraphNode, around?: string): boolean;
  addNeighbors(around: string, neighbors: Neighbor[]): Added;
  dismiss(key: string): boolean;
}

/** One store for the component's life, and its changes counted. The methods are the same functions on every render. */
export function useGraphStore(): GraphStoreHook {
  const ref = useRef<GraphStore | null>(null);
  ref.current ??= new GraphStore();
  const store = ref.current;
  const [version, setVersion] = useState(0);
  const methods = useMemo(() => {
    const changed = () => setVersion((v) => v + 1);
    return {
      rebuild(data: GraphResponse) {
        store.rebuild(data);
        changed();
      },
      addNode(n: GraphNode, around?: string) {
        const added = store.addNode(n, around);
        if (added) changed();
        return added;
      },
      addNeighbors(around: string, neighbors: Neighbor[]) {
        const added = store.addNeighbors(around, neighbors);
        changed();
        return added;
      },
      dismiss(key: string) {
        const gone = store.dismiss(key);
        if (gone) changed();
        return gone;
      },
    };
  }, [store]);
  return { store, version, ...methods };
}
