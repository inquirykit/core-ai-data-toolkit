// The graph view's drawing (GraphView.tsx): the sigma.js instance over the
// store's graphology graph, its reducers (what each node and edge is shown
// as, from the latest ``Drawing``) and its events, and the ForceAtlas2 layout
// that places the nodes. Sigma is pinned (package.json): v4 changed the API.
import { useCallback, useEffect, useRef, useState, type RefObject } from "react";
import forceAtlas2 from "graphology-layout-forceatlas2";
import FA2Layout from "graphology-layout-forceatlas2/worker";
import Sigma from "sigma";
import { EdgeArrowProgram, EdgeLineProgram, type EdgeProgramType } from "sigma/rendering";
import type { EdgeDisplayData, NodeDisplayData } from "sigma/types";
import { CANVAS, labelOf, nodeSize, type GraphNode, type SizeBy, type View } from "./graph";
import type { DrawnGraph, EdgeAttrs, NodeAttrs } from "./graph-store";

/** What the reducers read on each refresh. */
export interface Drawing {
  nodes: Map<string, GraphNode>;
  view: View;
  colorOf(table: string): string;
  labelField: string;
  sizeBy: SizeBy;
  selected: string | null;
  edgeLabels: boolean;
  dark: boolean;
}

/** What the pointer does on the canvas. */
export interface SigmaActions {
  select(key: string | null): void;
  expand(key: string): void;
}

export type GraphSigma = Sigma<NodeAttrs, EdgeAttrs>;

/**
 * Draws ``graph`` in ``container`` for the component's life, as ``drawing`` says: the reducers read the latest
 * one, and the canvas is refreshed when it changes. Clicks select, double-clicks expand, dragging a node moves it.
 */
export function useSigma(container: RefObject<HTMLDivElement | null>, graph: DrawnGraph, drawing: Drawing, actions: SigmaActions): RefObject<GraphSigma | null> {
  const sigmaRef = useRef<GraphSigma | null>(null);
  const hoverRef = useRef<string | null>(null);
  // Set during React's render, so the next refresh (sigma's own or the effect's below) sees the newest.
  const latest = useRef({ drawing, actions });
  latest.current = { drawing, actions };

  useEffect(() => {
    const el = container.current;
    if (!el) return;
    const sigma = new Sigma<NodeAttrs, EdgeAttrs>(graph, el, {
      allowInvalidContainer: true,
      defaultEdgeType: "arrow",
      // The programs are written for any attributes; they never read ours.
      edgeProgramClasses: {
        arrow: EdgeArrowProgram as unknown as EdgeProgramType<NodeAttrs, EdgeAttrs>,
        line: EdgeLineProgram as unknown as EdgeProgramType<NodeAttrs, EdgeAttrs>,
      },
      renderEdgeLabels: false,
      labelFont: "'Inter Variable', system-ui, sans-serif",
      labelSize: 12,
      labelWeight: "500",
      labelRenderedSizeThreshold: 5,
      edgeLabelFont: "'JetBrains Mono Variable', ui-monospace, monospace",
      edgeLabelSize: 10,
      zIndex: true,
      minCameraRatio: 0.02,
      maxCameraRatio: 10,
      nodeReducer: (node, data): Partial<NodeDisplayData> => {
        const d = latest.current.drawing;
        const canvas = d.dark ? CANVAS.dark : CANVAS.light;
        const state = d.view.nodes.get(node) ?? "hidden";
        if (state === "hidden") return { ...data, hidden: true };
        const row = d.nodes.get(node);
        const hovered = hoverRef.current;
        const res: Partial<NodeDisplayData> = {
          ...data,
          color: d.colorOf(data.table),
          label: row ? labelOf(row, d.labelField) : node,
          size: nodeSize(d.view.degree.get(node) ?? 0, d.sizeBy),
          zIndex: 1,
        };
        const away = hovered !== null && hovered !== node && !graph.areNeighbors(hovered, node);
        if (state === "dimmed" || away) {
          res.color = canvas.dimNode;
          res.label = "";
          res.zIndex = 0;
        }
        if (node === d.selected || node === hovered) {
          res.highlighted = true;
          res.forceLabel = true;
          res.zIndex = 2;
          res.color = d.colorOf(data.table);
          res.label = row ? labelOf(row, d.labelField) : node;
        } else if (hovered !== null && !away) {
          res.forceLabel = true;
        }
        return res;
      },
      edgeReducer: (edge, data): Partial<EdgeDisplayData> => {
        const d = latest.current.drawing;
        const canvas = d.dark ? CANVAS.dark : CANVAS.light;
        const state = d.view.edges.get(edge) ?? "hidden";
        if (state === "hidden") return { ...data, hidden: true };
        const [from, to] = graph.extremities(edge);
        const hovered = hoverRef.current;
        const focus = hovered ?? d.selected;
        const touches = focus !== null && (from === focus || to === focus);
        const res: Partial<EdgeDisplayData> = { ...data, label: data.kind, color: canvas.edge, size: 1, zIndex: 0 };
        if (state === "dimmed" || (hovered !== null && !touches)) {
          res.color = canvas.dimEdge;
          res.label = "";
        }
        if (touches) {
          res.color = d.colorOf(graph.getNodeAttribute(from, "table"));
          res.size = 2;
          res.zIndex = 1;
          res.forceLabel = d.edgeLabels;
        }
        return res;
      },
    });
    sigmaRef.current = sigma;

    sigma.on("clickNode", ({ node }) => latest.current.actions.select(node));
    sigma.on("doubleClickNode", ({ node, event }) => {
      event.preventSigmaDefault();
      latest.current.actions.expand(node);
    });
    sigma.on("clickStage", () => latest.current.actions.select(null));
    sigma.on("enterNode", ({ node }) => {
      hoverRef.current = node;
      el.style.cursor = "pointer";
      sigma.scheduleRefresh();
    });
    sigma.on("leaveNode", () => {
      hoverRef.current = null;
      el.style.cursor = "";
      sigma.scheduleRefresh();
    });
    // Dragging a node moves it; the camera stays put meanwhile.
    let dragged: string | null = null;
    sigma.on("downNode", ({ node }) => {
      dragged = node;
      if (!sigma.getCustomBBox()) sigma.setCustomBBox(sigma.getBBox());
    });
    sigma.on("moveBody", ({ event }) => {
      if (!dragged) return;
      const at = sigma.viewportToGraph(event);
      graph.setNodeAttribute(dragged, "x", at.x);
      graph.setNodeAttribute(dragged, "y", at.y);
      event.preventSigmaDefault();
      event.original.preventDefault();
      event.original.stopPropagation();
    });
    const release = () => {
      dragged = null;
    };
    sigma.on("upNode", release);
    sigma.on("upStage", release);

    const observer = new ResizeObserver(() => sigma.scheduleRefresh());
    observer.observe(el);
    return () => {
      observer.disconnect();
      sigma.kill();
      sigmaRef.current = null;
    };
  }, [container, graph]);

  // The theme's colors, and whether edge labels are drawn, are settings; everything else is in the reducers.
  const { dark, edgeLabels } = drawing;
  useEffect(() => {
    const canvas = dark ? CANVAS.dark : CANVAS.light;
    sigmaRef.current?.setSettings({
      labelColor: { color: canvas.label },
      edgeLabelColor: { color: canvas.edgeLabel },
      defaultEdgeColor: canvas.edge,
      renderEdgeLabels: edgeLabels,
    });
  }, [dark, edgeLabels]);
  // Whenever what the reducers read has changed.
  const { view, colorOf, labelField, sizeBy, selected } = drawing;
  useEffect(() => {
    sigmaRef.current?.refresh();
  }, [view, colorOf, labelField, sizeBy, selected, edgeLabels, dark]);

  return sigmaRef;
}

/** How long the layout runs on its own after a load, by the graph's size. */
export const layoutMs = (order: number) => Math.min(6000, 1200 + order * 2);

export interface ForceLayout {
  /** Whether it is running */
  on: boolean;
  /** Run it, for ``forMs`` or until stopped. */
  start(forMs?: number): void;
  stop(): void;
  /** Forget it: the supervisor is tied to one graph, so a graph rebuilt gets a new one. */
  drop(): void;
}

/** ForceAtlas2 over ``graph``, in a worker; killed when the component goes. The functions are the same on every render. */
export function useForceLayout(graph: DrawnGraph): ForceLayout {
  const fa2 = useRef<FA2Layout | null>(null);
  const timer = useRef<number | undefined>(undefined);
  const [on, setOn] = useState(false);

  const stop = useCallback(() => {
    clearTimeout(timer.current);
    fa2.current?.stop();
    setOn(false);
  }, []);
  const start = useCallback(
    (forMs?: number) => {
      if (!graph.order) return;
      clearTimeout(timer.current);
      if (!fa2.current) {
        const settings = forceAtlas2.inferSettings(graph);
        fa2.current = new FA2Layout(graph, { settings: { ...settings, gravity: 1, scalingRatio: 10 } });
      }
      fa2.current.start();
      setOn(true);
      if (forMs) timer.current = window.setTimeout(stop, forMs);
    },
    [graph, stop],
  );
  const drop = useCallback(() => {
    clearTimeout(timer.current);
    fa2.current?.kill();
    fa2.current = null;
    setOn(false);
  }, []);
  useEffect(() => drop, [drop]);

  return { on, start, stop, drop };
}
