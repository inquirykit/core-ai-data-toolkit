// The canvas of a workflow, on React Flow: the palette of node types on the
// left (click one, or drag it onto the canvas), the nodes and edges in the
// middle, placed freely, and the chosen node's settings on the right
// (NodePanel.tsx). Each node has an input handle (but the source) and a
// labelled handle per output; an edge that would go into the source, be there
// twice or go round in a circle cannot be drawn. A dry run's or a run's counts
// label the edges and badge the nodes.

import { createContext, useContext, useEffect, useMemo, useRef, type Dispatch, type DragEvent, type SetStateAction } from "react";
import {
  Background,
  BackgroundVariant,
  Controls,
  Handle,
  Position,
  ReactFlow,
  ReactFlowProvider,
  applyEdgeChanges,
  applyNodeChanges,
  useReactFlow,
  useUpdateNodeInternals,
  type NodeProps,
  type XYPosition,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import "../../shared/flow.css";
import { Cpu, Database, Merge, PenLine, Route, ScanSearch, Sheet, Split, TableProperties, Workflow, type LucideIcon } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Card } from "@/components/ui/card";
import { cn } from "@/lib/utils";
import type { TaskInfo } from "../../shared/task-io/schema";
import type { WorkflowNodeType } from "../../shared/types";
import { NodePanel } from "./NodePanel";
import { COLUMN, edgeLabel, flowEdge, freePlace, linkProblem, nextNodeId, nodeBadge, nodeSummary, outputsOf, type Counts, type StepEdge, type StepNode } from "./workflows";

const ICONS: Record<string, LucideIcon> = {
  "table.read": Sheet,
  task: Cpu,
  enrich: ScanSearch,
  if: Split,
  switch: Route,
  set: PenLine,
  merge: Merge,
  sql: Database,
  "table.write": TableProperties,
};
const iconOf = (type: string) => ICONS[type] ?? Workflow;

/** What every node on the canvas reads: the node types by name, and the counts shown. */
const CanvasContext = createContext<{ types: Record<string, WorkflowNodeType>; counts: Counts | null }>({ types: {}, counts: null });

/** What a palette item carries when it is dragged onto the canvas. */
const DRAG_TYPE = "application/x-workflow-node";

export function WorkflowCanvas(props: CanvasProps) {
  return (
    <ReactFlowProvider>
      <Canvas {...props} />
    </ReactFlowProvider>
  );
}

interface CanvasProps {
  types: WorkflowNodeType[];
  nodes: StepNode[];
  edges: StepEdge[];
  setNodes: Dispatch<SetStateAction<StepNode[]>>;
  setEdges: Dispatch<SetStateAction<StepEdge[]>>;
  counts: Counts | null;
  /** What the counts are of, said above the canvas */
  countsOf: string | null;
  /** GET /tasks' contracts, for a node's inputs and what it can read */
  tasks?: Record<string, TaskInfo>;
}

function Canvas({ types, nodes, edges, setNodes, setEdges, counts, countsOf, tasks }: CanvasProps) {
  const flow = useReactFlow<StepNode, StepEdge>();
  const box = useRef<HTMLDivElement>(null);
  const byType = useMemo(() => Object.fromEntries(types.map((t) => [t.type, t])), [types]);
  const context = useMemo(() => ({ types: byType, counts }), [byType, counts]);
  const picked = nodes.filter((n) => n.selected);
  const chosen = picked.length === 1 ? picked[0] : null;
  const hasSource = nodes.some((n) => byType[n.data.type]?.source);
  // The edges as drawn: labelled with the rows along each, the error outputs' in the error color.
  const shownEdges = useMemo(
    () => edges.map((e) => ({ ...e, label: edgeLabel(counts, e), style: e.sourceHandle === "error" ? { stroke: "var(--critical)" } : undefined })),
    [edges, counts],
  );

  /** A node of ``type`` at ``at``, else beside the chosen node, else amid the canvas, on no other; it becomes the chosen one. */
  const add = (type: string, at?: XYPosition) => {
    let position = at;
    if (!position) {
      let near = chosen && { x: chosen.position.x + COLUMN, y: chosen.position.y };
      if (!near) {
        const r = box.current!.getBoundingClientRect();
        const middle = flow.screenToFlowPosition({ x: r.left + r.width / 2, y: r.top + r.height / 2 });
        near = { x: middle.x - 100, y: middle.y - 40 };
      }
      position = freePlace(nodes.map((n) => n.position), near);
    }
    const node: StepNode = { id: nextNodeId(nodes.map((n) => n.id)), type: "step", position, data: { type: type as StepNode["data"]["type"], settings: {} }, selected: true };
    setNodes((ns) => [...ns.map((n) => (n.selected ? { ...n, selected: false } : n)), node]);
  };
  const drop = (e: DragEvent) => {
    const type = e.dataTransfer.getData(DRAG_TYPE);
    if (!type) return;
    e.preventDefault();
    const at = flow.screenToFlowPosition({ x: e.clientX, y: e.clientY });
    add(type, { x: at.x - 100, y: at.y - 20 });
  };

  return (
    <Card className="gap-0 overflow-hidden py-0">
      <div className="grid h-[640px] grid-cols-[10.5rem_minmax(0,1fr)] lg:grid-cols-[10.5rem_minmax(0,1fr)_22rem]">
        <nav aria-label="Node types" className="grid content-start gap-0.5 overflow-y-auto border-r p-2">
          <p className="px-2 pt-1 pb-2 text-xs font-medium text-muted-foreground">Add a node</p>
          {types.map((t) => {
            const Icon = iconOf(t.type);
            const off = t.source && hasSource;
            return (
              <button
                key={t.type}
                type="button"
                draggable={!off}
                disabled={off}
                title={off ? "A workflow reads one table" : t.description}
                onClick={() => add(t.type)}
                onDragStart={(e) => {
                  e.dataTransfer.setData(DRAG_TYPE, t.type);
                  e.dataTransfer.effectAllowed = "move";
                }}
                className="flex h-8 items-center gap-2 rounded-md px-2 text-left text-sm outline-none hover:bg-muted focus-visible:ring-[3px] focus-visible:ring-ring/50 disabled:pointer-events-none disabled:opacity-50"
              >
                <Icon className="size-3.5 shrink-0 text-muted-foreground" aria-hidden="true" />
                {t.label}
              </button>
            );
          })}
        </nav>
        <div ref={box} className="flow-canvas relative min-w-0" onDragOver={(e) => e.preventDefault()} onDrop={drop}>
          {countsOf && <div className="absolute top-2 left-1/2 z-10 -translate-x-1/2 rounded-md border bg-card px-2 py-1 text-xs text-muted-foreground shadow-sm">{countsOf}</div>}
          <CanvasContext.Provider value={context}>
            <ReactFlow<StepNode, StepEdge>
              nodes={nodes}
              edges={shownEdges}
              nodeTypes={NODE_TYPES}
              onNodesChange={(changes) => setNodes((ns) => applyNodeChanges(changes, ns))}
              onEdgesChange={(changes) => setEdges((es) => applyEdgeChanges(changes, es))}
              onConnect={(c) => setEdges((es) => [...es, flowEdge(c.source, c.sourceHandle ?? "out", c.target)])}
              isValidConnection={(c) => linkProblem(c, nodes, edges, byType) === null}
              deleteKeyCode={["Backspace", "Delete"]}
              fitView
              fitViewOptions={{ maxZoom: 1 }}
            >
              <Background variant={BackgroundVariant.Dots} gap={16} />
              <Controls showInteractive={false} />
            </ReactFlow>
          </CanvasContext.Provider>
        </div>
        <div className="col-span-2 min-h-0 overflow-y-auto border-t lg:col-span-1 lg:border-t-0 lg:border-l">
          {chosen ? (
            <NodePanel
              key={chosen.id}
              node={chosen}
              type={byType[chosen.data.type]}
              count={counts?.nodes[chosen.id] ?? null}
              nodes={nodes}
              edges={edges}
              types={byType}
              tasks={tasks}
              setNodes={setNodes}
              setEdges={setEdges}
            />
          ) : (
            <p className="p-4 text-sm text-muted-foreground">Click a node to see its settings{counts ? " and the rows that left it" : ""}.</p>
          )}
        </div>
      </div>
    </Card>
  );
}

/** A node as the canvas draws it: its type and id, what it works on, an input, a handle per output, and its counts. */
function StepCard({ id, data, selected }: NodeProps<StepNode>) {
  const { types, counts } = useContext(CanvasContext);
  const type = types[data.type];
  const outputs = outputsOf(type, data.settings);
  // React Flow measures a node's handles when it first draws it: a Switch's cases changing adds or takes some away.
  const updateInternals = useUpdateNodeInternals();
  const handles = outputs.join("|");
  const measured = useRef(handles);
  useEffect(() => {
    if (measured.current === handles) return;
    measured.current = handles;
    updateInternals(id);
  }, [id, handles, updateInternals]);
  const Icon = iconOf(data.type);
  const summary = nodeSummary(data.type, data.settings);
  const count = counts?.nodes[id];
  return (
    <div className={cn("relative w-52 rounded-lg border bg-card text-card-foreground shadow-sm", selected && "ring-2 ring-ring")}>
      {!type?.source && <Handle type="target" position={Position.Left} />}
      <div className="flex items-center gap-2 px-3 pt-2">
        <Icon className="size-3.5 shrink-0 text-muted-foreground" aria-hidden="true" />
        <span className="truncate text-sm font-medium">{type?.label ?? data.type}</span>
        <Badge variant="secondary" className="ml-auto h-4 px-1 font-mono text-[10px]">
          {id}
        </Badge>
      </div>
      <div className="truncate px-3 pt-0.5 font-mono text-[11px] text-muted-foreground" title={summary}>
        {summary || " "}
      </div>
      <div className="grid pt-1 pb-1.5">
        {outputs.map((o) => (
          <div key={o} className={cn("relative h-5 pr-3 text-right text-[11px] leading-5", o === "error" ? "text-critical-text" : "text-muted-foreground")}>
            {o}
            <Handle type="source" position={Position.Right} id={o} />
          </div>
        ))}
      </div>
      {count && (
        <div className={cn("border-t px-3 py-1 text-[11px] tabular-nums", count.failed ? "text-critical-text" : "text-muted-foreground")} title={count.error ?? undefined}>
          {nodeBadge(count)}
          {count.error && <div className="truncate">{count.error}</div>}
        </div>
      )}
    </div>
  );
}

const NODE_TYPES = { step: StepCard };
