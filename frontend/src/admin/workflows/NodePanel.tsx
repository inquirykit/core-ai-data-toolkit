// The chosen node's side panel: its id and type, its settings as a form made
// from its type's settings schema, what it can read of the results before it
// (and a Task node's inputs, the ones its task needs marked), and after a dry
// run its counts, first error and a few rows as they left it.

import { useMemo, type Dispatch, type ReactNode, type SetStateAction } from "react";
import { Trash2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Notice, Pane } from "../../shared/components";
import { formSchema } from "../../shared/form-schema";
import { SchemaForm, type FormSchema } from "../../shared/schema-form";
import type { TaskInfo } from "../../shared/task-io/schema";
import type { WorkflowNodeCount, WorkflowNodeType } from "../../shared/types";
import { casesOf, nodeBadge, readable, rewireCases, taskInputs, type Settings, type StepEdge, type StepNode } from "./workflows";

/** The fields a node's result has, when they are known before it runs: a Task's, its task's output. */
export function givesOf(tasks: Record<string, TaskInfo> | undefined) {
  return (node: Pick<StepNode, "data">): string[] | null => {
    if (node.data.type !== "task") return null;
    const output = tasks?.[String(node.data.settings.task ?? "")]?.output;
    return output?.properties ? Object.keys(output.properties) : null;
  };
}

export function NodePanel(props: {
  node: StepNode;
  type: WorkflowNodeType | undefined;
  count: WorkflowNodeCount | null;
  nodes: StepNode[];
  edges: StepEdge[];
  types: Record<string, WorkflowNodeType>;
  // GET /tasks' contracts, once it answered.
  tasks: Record<string, TaskInfo> | undefined;
  setNodes: Dispatch<SetStateAction<StepNode[]>>;
  setEdges: Dispatch<SetStateAction<StepEdge[]>>;
}) {
  const { node, type, count, nodes, edges, types, tasks, setNodes, setEdges } = props;
  const schema = useMemo(() => (type ? formSchema(type.settings as FormSchema) : null), [type]);
  const fields = type?.source ? [] : readable(node.id, nodes, edges, types, givesOf(tasks));
  const inputs = node.data.type === "task" ? taskInputs(tasks?.[String(node.data.settings.task ?? "")]?.input, node.data.settings) : [];

  const change = (settings: Settings) => {
    setNodes((ns) => ns.map((n) => (n.id === node.id ? { ...n, data: { ...n.data, settings } } : n)));
    // A Switch's outputs are its cases' numbers: each case's edges follow it, and a case removed takes its own.
    if (node.data.type === "switch") setEdges((es) => rewireCases(es, node.id, casesOf(node.data.settings), casesOf(settings)));
  };
  const remove = () => {
    setNodes((ns) => ns.filter((n) => n.id !== node.id));
    setEdges((es) => es.filter((e) => e.source !== node.id && e.target !== node.id));
  };

  return (
    <div className="grid content-start gap-4 p-4">
      <div className="grid gap-1">
        <div className="flex items-center gap-2">
          <h3 className="text-sm font-semibold">{type?.label ?? node.data.type}</h3>
          <code className="text-xs text-muted-foreground">
            {node.id} · {node.data.type}
          </code>
          <Button type="button" variant="ghost" size="icon-sm" className="ml-auto text-destructive hover:text-destructive" aria-label="Delete node" title="Delete node" onClick={remove}>
            <Trash2 aria-hidden="true" />
          </Button>
        </div>
        {type && <p className="text-xs text-muted-foreground">{type.description}</p>}
      </div>
      {schema ? (
        <SchemaForm<Settings> schema={schema} value={node.data.settings} onChange={change} />
      ) : (
        <Notice notice={{ text: `This build does not know the node type ${node.data.type}`, kind: "error" }} />
      )}
      {inputs.length > 0 && (
        <Names title="The task's inputs" hint="Each in inputs (a JMESPath on the row) or params; the ones in red it needs and has not got.">
          {inputs.map((i) => (
            <code key={i.name} className={i.required && !i.given ? "text-critical-text" : i.given ? "" : "text-muted-foreground"}>
              {i.name}
              {i.required ? "*" : ""}
            </code>
          ))}
        </Names>
      )}
      {fields.length > 0 && (
        <Names title="Results it can read" hint="Besides the table's own fields.">
          {fields.map((f) => (
            <code key={f}>{f}</code>
          ))}
        </Names>
      )}
      {count && (
        <div className="grid gap-2 border-t pt-3">
          <p className="text-xs text-muted-foreground tabular-nums">{nodeBadge(count)}</p>
          {count.error && <Notice notice={{ text: count.error, kind: "error" }} />}
          {count.samples && <Pane title="Rows as they left it" value={count.samples.length ? count.samples : null} empty="No rows left it" />}
        </div>
      )}
    </div>
  );
}

function Names({ title, hint, children }: { title: string; hint: string; children: ReactNode }) {
  return (
    <div className="grid gap-1">
      <p className="text-xs font-medium">{title}</p>
      <div className="flex flex-wrap gap-x-3 gap-y-1 text-xs">{children}</div>
      <p className="text-xs text-muted-foreground">{hint}</p>
    </div>
  );
}
