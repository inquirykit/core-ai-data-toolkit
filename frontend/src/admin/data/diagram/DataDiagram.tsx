// A workspace's diagram ("#/data/app/_diagram", opened from its page): its
// tables as boxes of their declared fields, on React Flow. A table ref is a
// line from its field to the table it names, labelled one or several; a
// relationship (edge type) a dashed line between the tables it joins, by its
// name. The boxes are placed by diagram.ts each time it is drawn, and can be
// moved about, not kept. A box opens its table's rows, a ref's line its table's
// schema, a relationship's line its page.

import { useMemo } from "react";
import {
  Background,
  BackgroundVariant,
  Controls,
  Handle,
  MarkerType,
  Position,
  ReactFlow,
  type Edge,
  type Node,
  type NodeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import "../../../shared/flow.css";
import { Skeleton } from "@/components/ui/skeleton";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { useApi } from "../../../shared/api";
import { errorText } from "../../../shared/query";
import { Notice } from "../../../shared/components";
import { plural } from "../../../shared/format";
import { dataHash, fieldTypeLabel, type FieldInfo, type TableInfo, type Workspace } from "../data";
import { BucketBadge, KeptBadge } from "../DataWorkspace";
import { BOX, boxRows, diagramLinks, MAX_FIELDS, placeTables, type DiagramLink } from "./diagram";

type TableBox = Node<{ table: TableInfo; loops: string[] }, "table">;
type Line = Edge<{ hash: string }>;

const NODE_TYPES = { table: TableBoxView };

export function WorkspaceDiagram({ workspace }: { workspace: Workspace }) {
  const client = useApi();
  const name = workspace.name;
  // The pairs of tables each relationship joins: a read of every edge, so only when there are some.
  const edgeTypes = client.useQuery("get", "/db/{workspace}/_edges", { params: { path: { workspace: name } } }, { enabled: workspace.edges.length > 0 });
  const waiting = workspace.edges.length > 0 && !edgeTypes.data && !edgeTypes.error;

  const { nodes, edges, refs } = useMemo(() => {
    const links = diagramLinks(workspace.tables, edgeTypes.data?.edges ?? []);
    // A relationship joining a table's rows to its own is said in its box, a row each, not drawn round it.
    const loops = (t: string) => links.filter((l) => l.kind === "relationship" && l.from === t && l.to === t).map((l) => l.label);
    const place = placeTables(new Map(workspace.tables.map((t) => [t.name, boxRows(t.fields.length) + loops(t.name).length])), links);
    const nodes: TableBox[] = workspace.tables.map((t) => ({
      id: t.name,
      type: "table",
      position: place.get(t.name)!,
      data: { table: t, loops: loops(t.name) },
    }));
    const edges: Line[] = links.filter((l) => l.from !== l.to).map((l) => line(name, l));
    return { nodes, edges, refs: links.filter((l) => l.kind === "ref").length };
  }, [workspace.tables, edgeTypes.data, name]);

  return (
    <Card className="min-w-0 gap-4">
      <CardHeader>
        <CardTitle className="flex items-center gap-1.5 font-mono text-lg">
          <a className="text-muted-foreground hover:text-foreground" href={dataHash({ workspace: name })}>
            {name}
          </a>
          <span className="text-muted-foreground">/</span>
          <span className="font-sans">Diagram</span>
        </CardTitle>
        <CardDescription>
          {plural(workspace.tables.length, "table")} with their declared fields; {plural(refs, "table ref")}, each a line from its field to the table
          whose rows it names, and the relationships joining their rows, dashed. A table opens its rows, a line its schema or its relationship.
          Boxes can be moved: they are placed anew each time.
        </CardDescription>
      </CardHeader>
      <CardContent className="grid gap-3">
        {edgeTypes.error && <Notice notice={{ text: `Relationships are not drawn: ${errorText(edgeTypes.error)}`, kind: "error" }} />}
        {waiting ? (
          <Skeleton className="h-[70vh] rounded-lg" />
        ) : (
          <div className="flow-canvas h-[70vh] min-h-96 overflow-hidden rounded-lg border">
            <ReactFlow<TableBox, Line>
              key={nodes.map((n) => n.id).join("|") + edges.length}
              defaultNodes={nodes}
              defaultEdges={edges}
              nodeTypes={NODE_TYPES}
              nodesConnectable={false}
              edgesFocusable={false}
              onNodeClick={(_, node) => (location.hash = dataHash({ workspace: name, table: node.id }))}
              onEdgeClick={(_, edge) => edge.data && (location.hash = edge.data.hash)}
              fitView
              fitViewOptions={{ maxZoom: 1 }}
              minZoom={0.2}
            >
              <Background variant={BackgroundVariant.Dots} gap={16} />
              <Controls showInteractive={false} />
            </ReactFlow>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

/** A link drawn: a ref from its field's handle, solid; a relationship from its table's box, dashed. */
function line(workspace: string, l: DiagramLink): Line {
  const ref = l.kind === "ref";
  return {
    id: l.id,
    source: l.from,
    sourceHandle: ref ? l.field : "out",
    target: l.to,
    targetHandle: "in",
    label: l.label,
    markerEnd: { type: MarkerType.ArrowClosed, width: 16, height: 16, color: ref ? "var(--primary)" : "var(--muted-foreground)" },
    style: ref ? { stroke: "var(--primary)" } : { strokeDasharray: "5 4" },
    data: { hash: ref ? dataHash({ workspace, table: l.from, view: "schema" }) : dataHash({ workspace, edge: l.label }) },
  };
}

/** What a field's row says of its type: a ref the table it names, a list its items' type with []. */
function typeOf(f: FieldInfo): string {
  if (f.type === "ref") return `→ ${f.to}${f.list ? " (several)" : ""}`;
  return `${fieldTypeLabel(f.type)}${f.list ? "[]" : ""}`;
}

/** A table's box: its name and rows, its declared fields (a ref's with the handle its line leaves from). */
function TableBoxView({ data }: NodeProps<TableBox>) {
  const { table, loops } = data;
  const fields = table.fields.slice(0, MAX_FIELDS);
  const more = table.fields.length - fields.length;
  // Its handles sit on the header's middle, where relationships leave and every line arrives.
  const middle = { top: BOX.header / 2 };
  return (
    <div className="cursor-pointer overflow-hidden rounded-lg border bg-card text-card-foreground shadow-sm hover:border-ring" style={{ width: BOX.width }}>
      <Handle type="target" id="in" position={Position.Left} isConnectable={false} style={middle} />
      <Handle type="source" id="out" position={Position.Right} isConnectable={false} style={middle} />
      <div className="flex items-center gap-2 border-b bg-muted/50 px-3" style={{ height: BOX.header }}>
        <span className="min-w-0 truncate font-mono text-[13px] font-medium" title={table.name}>
          {table.name}
        </span>
        {table.bucket && <BucketBadge className="h-5 px-1.5 text-[10px]" />}
        {table.kept && <KeptBadge keeper={table.kept} className="h-5 max-w-24 px-1.5 text-[10px]" />}
        <span className="ml-auto shrink-0 text-xs text-muted-foreground tabular-nums">{plural(table.count, "row")}</span>
      </div>
      <div style={{ paddingBottom: BOX.pad }}>
        {fields.map((f) => (
          <div key={f.name} className="relative flex items-center gap-2 px-3 text-xs" style={{ height: BOX.row }}>
            <span className={f.type === "ref" ? "truncate font-mono font-medium text-primary" : "truncate font-mono"}>{f.name}</span>
            <span className="ml-auto truncate text-muted-foreground">{typeOf(f)}</span>
            {f.type === "ref" && <Handle type="source" id={f.name} position={Position.Right} isConnectable={false} />}
          </div>
        ))}
        {more > 0 && (
          <div className="flex items-center px-3 text-xs text-muted-foreground" style={{ height: BOX.row }}>
            +{more} more fields
          </div>
        )}
        {!table.fields.length && (
          <div className="flex items-center px-3 text-xs text-muted-foreground italic" style={{ height: BOX.row }}>
            no declared fields
          </div>
        )}
        {loops.map((type) => (
          <div key={type} className="flex items-center gap-1 px-3 text-xs text-muted-foreground" style={{ height: BOX.row }} title={`${type} joins rows of ${table.name} to each other`}>
            ↻ <span className="font-mono">{type}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
