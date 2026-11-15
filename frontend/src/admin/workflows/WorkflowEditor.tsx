// A workflow's editor: its name, description, page size and trigger above, the
// canvas of its nodes and edges (WorkflowCanvas.tsx), and its runs below
// (WorkflowRuns.tsx). The canvas shows the counts of the last dry run, or of
// the run picked in the list.

import { useState } from "react";
import { ChevronLeft, FlaskConical, Play, RotateCcw, Save } from "lucide-react";
import { useQueryClient } from "@tanstack/react-query";
import { Badge } from "@/components/ui/badge";
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { useAction } from "../../shared/action";
import { useApi } from "../../shared/api";
import { ConfirmButton, Field, Segmented, SubmitButton } from "../../shared/components";
import { valueKey } from "../../shared/form-schema";
import { plural } from "../../shared/format";
import { poll } from "../../shared/query";
import type { TasksResponse } from "../../shared/task-io/schema";
import type { WorkflowDryRun, WorkflowInfo, WorkflowNodeType, WorkflowRun } from "../../shared/types";
import { WorkflowCanvas } from "./WorkflowCanvas";
import { WorkflowRuns } from "./WorkflowRuns";
import {
  fieldsOf,
  NEW_SPEC,
  readsOnlyNew,
  runsPoll,
  specOf,
  toFlow,
  workflowHash,
  type Counts,
  type StepEdge,
  type StepNode,
  type TriggerKind,
  type WorkflowFields,
} from "./workflows";

/** The most rows a dry run takes through (the server's DRY_RUN_ROWS). */
const DRY_RUN_MAX = 50;

/** What the canvas counts: the last dry run's, or a run's from the list (kept up to date as it is polled). */
type Shown = { kind: "dry"; result: WorkflowDryRun } | { kind: "run"; id: string } | null;

export function WorkflowEditor({ types, saved }: { types: WorkflowNodeType[]; saved: WorkflowInfo | null }) {
  const client = useApi();
  const queries = useQueryClient();
  const id = saved?.id ?? "";
  // Drawn once from the spec; from then on the editor's own until it is saved.
  const [start] = useState(() => {
    const spec = saved ?? NEW_SPEC;
    return { fields: fieldsOf(spec), ...toFlow(spec) };
  });
  const [fields, setFields] = useState<WorkflowFields>(start.fields);
  const [nodes, setNodes] = useState<StepNode[]>(start.nodes);
  const [edges, setEdges] = useState<StepEdge[]>(start.edges);
  const spec = specOf(fields, nodes, edges);
  // The spec as it was saved (or first drawn): anything else is a change not saved yet.
  const [baseline, setBaseline] = useState(() => valueKey(specOf(start.fields, start.nodes, start.edges)));
  const dirty = valueKey(spec) !== baseline;
  const [limit, setLimit] = useState("20");
  const [shown, setShown] = useState<Shown>(null);

  // GET /tasks changes only with a restart: asked once per page (TryPanel shares the reply).
  const tasks = client.useQuery("get", "/tasks", undefined, { staleTime: Infinity, select: (d) => (d as TasksResponse).tasks });
  const runs = client.useQuery(
    "get",
    "/workflows/{id}/runs",
    { params: { path: { id }, query: { limit: 20 } } },
    { enabled: Boolean(id), select: (d) => d.runs, ...poll((d?: { runs: WorkflowRun[] }) => runsPoll(d?.runs)) },
  );
  const counts: Counts | null = shown?.kind === "dry" ? shown.result : shown?.kind === "run" ? (runs.data?.find((r) => r.id === shown.id) ?? null) : null;

  const set = (patch: Partial<WorkflowFields>) => setFields((f) => ({ ...f, ...patch }));
  const reloadList = () => queries.invalidateQueries({ queryKey: ["get", "/workflows"] });

  const save = useAction(
    async () => {
      if (id) await client.put("/workflows/{id}", { params: { path: { id } }, body: spec });
      else {
        const made = await client.post("/workflows", { body: spec });
        location.hash = workflowHash({ view: "edit", id: made.id });
      }
      setBaseline(valueKey(spec));
      void reloadList();
      void queries.invalidateQueries({ queryKey: ["get", "/workflows/{id}"] });
    },
    { success: "Saved", failed: "Not saved" },
  );
  const remove = useAction(
    async () => {
      await client.delete("/workflows/{id}", { params: { path: { id } } });
      void reloadList();
      location.hash = workflowHash({ view: "list" });
    },
    { success: `Deleted ${fields.name || "the workflow"}`, failed: "Not deleted" },
  );
  const dry = useAction(
    async () => {
      const result = await client.post("/workflows/dry-run", { body: { spec, limit: Number(limit) } });
      setShown({ kind: "dry", result });
    },
    { failed: "The dry run was refused" },
  );
  const run = useAction(
    async () => {
      const queued = await client.post("/workflows/{id}/run", { params: { path: { id } }, body: { priority: "low" } });
      await runs.refetch();
      setShown({ kind: "run", id: queued.id });
    },
    { success: "Run queued", failed: "Not started" },
  );
  const reset = useAction(
    async () => {
      await client.post("/workflows/{id}/reset", { params: { path: { id } } });
      void queries.invalidateQueries({ queryKey: ["get", "/workflows/{id}"] });
    },
    { success: "The next run reads the whole table", failed: "Not reset" },
  );

  const shownWhat =
    shown?.kind === "dry" ? `Dry run: ${plural(shown.result.rows, "row")} through, nothing written` : shown?.kind === "run" ? `Run ${shown.id}` : null;

  return (
    <section className="grid min-w-0 gap-4">
      <Card className="gap-4">
        <CardHeader>
          <a href={workflowHash({ view: "list" })} className="flex w-fit items-center gap-1 text-xs text-muted-foreground hover:text-foreground">
            <ChevronLeft className="size-3.5" aria-hidden="true" />
            Workflows
          </a>
          <CardTitle className="flex items-center gap-2">
            {saved ? saved.name : "New workflow"}
            {dirty && (
              <Badge variant="outline" className="font-normal">
                not saved
              </Badge>
            )}
          </CardTitle>
          <CardDescription>
            Add nodes from the palette, join an output to a node's input, and click a node for its settings. A dry run takes the table's first rows
            through what is drawn, saved or not, and writes nothing.
          </CardDescription>
          <CardAction className="flex flex-wrap items-center justify-end gap-2">
            <Input
              type="number"
              min={1}
              max={DRY_RUN_MAX}
              className="h-8 w-16"
              aria-label="Rows the dry run takes"
              title="Rows the dry run takes"
              value={limit}
              onChange={(e) => setLimit(e.target.value)}
            />
            <SubmitButton type="button" variant="outline" size="sm" busy={dry.busy} icon={<FlaskConical aria-hidden="true" />} onClick={() => dry.run()}>
              Dry run
            </SubmitButton>
            <SubmitButton type="button" size="sm" busy={save.busy} icon={<Save aria-hidden="true" />} onClick={() => save.run()}>
              Save
            </SubmitButton>
            {saved && (
              <span title={dirty ? "A run takes the workflow as saved: save the changes first" : undefined}>
                <SubmitButton type="button" variant="outline" size="sm" busy={run.busy} disabled={dirty} icon={<Play aria-hidden="true" />} onClick={() => run.run()}>
                  Run
                </SubmitButton>
              </span>
            )}
            {saved && readsOnlyNew(saved) && (
              <ConfirmButton
                title="Read the whole table again?"
                body="Runs read only the rows written since the last one; after a reset the next run reads them all."
                label="Reset"
                icon={<RotateCcw aria-hidden="true" />}
                busy={reset.busy}
                onConfirm={() => reset.run()}
              />
            )}
            {saved && (
              <ConfirmButton
                title={`Delete ${saved.name}?`}
                body="Its schedule and its runs go too; runs under way are cancelled first."
                busy={remove.busy}
                onConfirm={() => remove.run()}
              />
            )}
          </CardAction>
        </CardHeader>
        <CardContent className="grid gap-4 md:grid-cols-[minmax(0,1fr)_minmax(0,2fr)_7rem]">
          <Field label="Name">
            <Input value={fields.name} autoComplete="off" placeholder="Tag tickets" onChange={(e) => set({ name: e.target.value })} />
          </Field>
          <Field label="Description">
            <Input value={fields.description} autoComplete="off" onChange={(e) => set({ description: e.target.value })} />
          </Field>
          <Field label="Page size" hint="Rows at a time">
            <Input type="number" min={1} max={1000} value={fields.page_size} onChange={(e) => set({ page_size: e.target.value })} />
          </Field>
          <div className="flex flex-wrap items-end gap-3 md:col-span-3">
            <Field label="Trigger">
              <Segmented<TriggerKind>
                label="Trigger"
                size="sm"
                options={[
                  ["none", "By hand"],
                  ["cron", "Cron"],
                  ["interval", "Every"],
                ]}
                value={fields.trigger.kind}
                onChange={(kind) => set({ trigger: { ...fields.trigger, kind } })}
              />
            </Field>
            {fields.trigger.kind === "cron" && (
              <Input
                className="h-8 w-48 font-mono"
                aria-label="Cron expression"
                placeholder="0 3 * * *"
                value={fields.trigger.cron}
                onChange={(e) => set({ trigger: { ...fields.trigger, cron: e.target.value } })}
              />
            )}
            {fields.trigger.kind === "interval" && (
              <span className="flex items-center gap-2 text-sm text-muted-foreground">
                <Input
                  type="number"
                  min={60}
                  className="h-8 w-28"
                  aria-label="Seconds between runs"
                  value={fields.trigger.interval}
                  onChange={(e) => set({ trigger: { ...fields.trigger, interval: e.target.value } })}
                />
                seconds, at least 60
              </span>
            )}
            {fields.trigger.kind === "cron" && <span className="pb-2 text-xs text-muted-foreground">minute hour day-of-month month day-of-week, server time</span>}
            <label className="flex items-start gap-2 pb-2 text-sm md:ml-auto" title="Its SQL nodes read as the admin: open it only when what they read is the clients' to see">
              <Checkbox checked={fields.invokable} onCheckedChange={(on) => set({ invokable: on === true })} className="mt-0.5" />
              <span>
                Clients may invoke it
                <span className="block text-xs text-muted-foreground">POST /workflows/{"{id}"}/invoke with the client key</span>
              </span>
            </label>
          </div>
        </CardContent>
      </Card>
      <WorkflowCanvas types={types} nodes={nodes} edges={edges} setNodes={setNodes} setEdges={setEdges} counts={counts} countsOf={shownWhat} tasks={tasks.data} />
      {saved && (
        <WorkflowRuns
          runs={runs.data ?? null}
          error={runs.error}
          onRefresh={runs.refetch}
          chosen={shown?.kind === "run" ? shown.id : null}
          onChoose={(runId) => setShown(runId ? { kind: "run", id: runId } : null)}
        />
      )}
    </section>
  );
}
