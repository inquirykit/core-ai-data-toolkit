// A dataset in the Evals section: its examples (added here, imported as
// JSON Lines, or saved from traces) and its runs, with the forms that start
// a run and pick two runs to compare.

import { useEffect, useState, type ChangeEvent, type FormEvent } from "react";
import { useQuery } from "@tanstack/react-query";
import { Download, GitCompare, LoaderCircle, Play, Plus, Trash2, Upload } from "lucide-react";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Button, buttonVariants } from "@/components/ui/button";
import { Card, CardAction, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Progress } from "@/components/ui/progress";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/lib/utils";
import { useAction } from "../shared/action";
import type { ApiClient } from "../shared/api";
import { downloadResponse } from "../shared/browser";
import { CodeEditor } from "../shared/code-editor";
import { failed } from "../shared/errors";
import { errorText, poll } from "../shared/query";
import { ConfirmButton, Field, Notice, SelectField, StatusLabel } from "../shared/components";
import type { Status } from "../shared/types";
import { fmtAgo, fmtInt, statusOf } from "../shared/format";
import { DataGrid, type GridColumn } from "../shared/grid";
import type { Dataset, Example, Priority, Run } from "../shared/types";
import { PRIORITY_OPTIONS } from "./jobs";
import {
  RUN_STATUS,
  compareHref,
  evalsHref,
  inputPreview,
  outputPreview,
  parseObject,
  parseReference,
  referencePreview,
  runProgress,
} from "./evals";
import { MODEL_IDS_LIST, type CatalogInfo } from "./panels";
import { Pane } from "./TraceDialog";

export function DatasetView(props: { client: ApiClient; dataset: Dataset; catalog: CatalogInfo; onChange(): Promise<unknown> }) {
  const { client, dataset } = props;
  const [tab, setTab] = useState(dataset.examples && !dataset.runs ? "examples" : dataset.runs ? "runs" : "examples");

  // Polled while runs are in progress, to keep their rows moving.
  const query = useQuery({
    queryKey: ["evals", "dataset", dataset.id],
    queryFn: async ({ signal }) => {
      const [detail, e] = await Promise.all([
        client.get("/admin/datasets/{dataset_id}", { params: { path: { dataset_id: dataset.id } }, signal }),
        client.get("/admin/datasets/{dataset_id}/examples", { params: { path: { dataset_id: dataset.id }, query: { limit: 1000 } }, signal }),
      ]);
      return { detail, examples: e.examples };
    },
    // (A judging that failed stays in the activity with its error; nothing more will happen to it.)
    ...poll((d?: { detail: { run_list: Run[] } }) =>
      d?.detail.run_list.some((r) => r.status === "queued" || r.status === "running" || (r.activity && !r.activity.error)) ? 3000 : 0,
    ),
  });
  const { data, refetch: reload } = query;
  const error = errorText(query.error);
  const detail = data?.detail ?? null;
  const examples = data?.examples ?? null;
  useEffect(() => {
    if (error) toast.error("Could not load the dataset", { description: error });
  }, [error]);

  const changed = async () => {
    await reload();
    await props.onChange();
  };

  const exportJsonl = () =>
    client
      .raw("get", "/admin/datasets/{dataset_id}/export", { params: { path: { dataset_id: dataset.id } } })
      .then((response) => downloadResponse(response, `${dataset.name}.jsonl`))
      .catch(failed("Could not export"));

  const remove = async () => {
    try {
      await client.delete("/admin/datasets/{dataset_id}", { params: { path: { dataset_id: dataset.id } } });
      await props.onChange();
      location.hash = evalsHref();
    } catch (e) {
      failed("Could not delete the dataset")(e);
    }
  };

  return (
    <Card className="gap-0 overflow-hidden py-0">
      <CardHeader className="border-b py-4">
        <CardTitle>{dataset.name}</CardTitle>
        <CardDescription>
          {dataset.description || "No description"} · {fmtInt(detail?.examples ?? dataset.examples)} examples · {fmtInt(detail?.run_list.length ?? dataset.runs)} runs
        </CardDescription>
        <CardAction className="flex gap-2">
          <Button type="button" variant="outline" size="sm" onClick={exportJsonl}>
            <Download aria-hidden="true" />
            Export JSONL
          </Button>
          <ConfirmButton
            title={`Delete ${dataset.name}?`}
            body="Its examples, every run over it and their scores go with it. Export first if you want to keep them."
            cancel="Keep"
            onConfirm={remove}
          />
        </CardAction>
      </CardHeader>
      <Tabs value={tab} onValueChange={setTab} className="gap-0">
        <div className="border-b px-4 py-3">
          <TabsList>
            <TabsTrigger value="examples">Examples</TabsTrigger>
            <TabsTrigger value="runs">Runs</TabsTrigger>
          </TabsList>
        </div>
        <TabsContent value="examples">
          <ExamplesTab client={client} dataset={dataset} examples={examples} onChange={changed} />
        </TabsContent>
        <TabsContent value="runs">
          <RunsTab client={client} dataset={dataset} runs={detail?.run_list ?? null} catalog={props.catalog} onChange={changed} />
        </TabsContent>
      </Tabs>
    </Card>
  );
}

function ExamplesTab(props: { client: ApiClient; dataset: Dataset; examples: Example[] | null; onChange(): Promise<unknown> }) {
  const { client, dataset, examples } = props;
  const [input, setInput] = useState('{"text": ""}');
  const [reference, setReference] = useState("");
  const [task, setTask] = useState("");
  const [jsonl, setJsonl] = useState("");
  const [open, setOpen] = useState<string | null>(null);

  const add = useAction(
    async (event: FormEvent) => {
      event.preventDefault();
      const parsed = parseObject(input);
      if (!parsed.ok) throw new Error(parsed.error);
      await client.post("/admin/datasets/{dataset_id}/examples", {
        params: { path: { dataset_id: dataset.id } },
        body: { examples: [{ input: parsed.value, reference: parseReference(reference), task: task || null }] },
      });
      setReference("");
      await props.onChange();
    },
    { notice: true, success: () => ({ text: "Example added", kind: "ok" }) },
  );

  const importLines = async () => {
    try {
      const r = await client.post("/admin/datasets/{dataset_id}/examples", { params: { path: { dataset_id: dataset.id } }, body: { jsonl } });
      toast.success(`Imported ${r.added} example${r.added === 1 ? "" : "s"}`);
      setJsonl("");
      await props.onChange();
    } catch (e) {
      failed("Could not import")(e);
    }
  };

  const pickFile = (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    if (file) file.text().then(setJsonl);
    event.target.value = "";
  };

  const remove = async (id: string) => {
    try {
      await client.delete("/admin/datasets/{dataset_id}/examples/{example_id}", { params: { path: { dataset_id: dataset.id, example_id: id } } });
      await props.onChange();
    } catch (e) {
      failed("Could not delete the example")(e);
    }
  };

  // The opened example shows whole, the others cut to two lines.
  const columns: GridColumn<Example>[] = [
    {
      key: "input",
      label: "Input",
      className: "max-w-md whitespace-normal",
      render: (e) => (
        <>
          <span className={cn("[overflow-wrap:anywhere]", open !== e.id && "line-clamp-2")}>{inputPreview(e.input, 2000)}</span>
          {open === e.id && <Pane title="Input" value={e.input} className="mt-2" />}
          <div className="mt-1 text-xs text-muted-foreground">
            {e.task ?? "task of the run"}
            {e.tags.map((t) => (
              <Badge key={t} variant="secondary" className="ml-1.5 px-1.5 py-0 text-[11px]">
                {t}
              </Badge>
            ))}
          </div>
        </>
      ),
    },
    {
      key: "reference",
      label: "Reference",
      className: "max-w-xs whitespace-normal",
      render: (e) => (
        <span className={cn("[overflow-wrap:anywhere]", open !== e.id && "line-clamp-2")}>
          {referencePreview(e.reference, 2000) || <span className="text-muted-foreground">none</span>}
        </span>
      ),
    },
    {
      key: "from",
      label: "From",
      className: "max-w-xs whitespace-normal text-xs text-muted-foreground",
      render: (e) =>
        e.source_model_id ? (
          <>
            <span className="font-mono [overflow-wrap:anywhere]">{e.source_model_id}</span>
            {e.source_output && (
              <div className={cn("mt-0.5 [overflow-wrap:anywhere]", open !== e.id && "line-clamp-2")}>answered: {outputPreview(e.source_output, 2000)}</div>
            )}
          </>
        ) : (
          "added by hand"
        ),
    },
    {
      key: "remove",
      headClassName: "w-10",
      render: (e) => (
        <Button type="button" variant="ghost" size="icon-sm" aria-label="Delete example" onClick={(ev) => (ev.stopPropagation(), remove(e.id))}>
          <Trash2 aria-hidden="true" />
        </Button>
      ),
    },
  ];

  return (
    <>
      <div className="max-h-[560px] overflow-y-auto">
        <DataGrid
          columns={columns}
          rows={examples ?? []}
          rowKey={(e) => e.id}
          loading={examples === null}
          rowProps={() => ({ className: "align-top" })}
          onRow={(e) => setOpen(open === e.id ? null : e.id)}
          empty="No examples yet: add some below, or save traces to this dataset"
        />
      </div>
      <div className="grid gap-5 border-t p-4 lg:grid-cols-2">
        <form className="grid gap-3" onSubmit={add.run}>
          <h3 className="text-sm font-semibold">Add an example</h3>
          <Field label="Input (JSON, as POST /inference takes it)">
            <CodeEditor aria-label="Input" language="json" minHeight="6rem" value={input} onChange={setInput} />
          </Field>
          <Field label="Reference answer" hint="Text, or JSON for a structured result. Optional">
            <Textarea value={reference} onChange={(e) => setReference(e.target.value)} rows={2} />
          </Field>
          <Field label="Task" hint="Unset: the run's task">
            <Input value={task} onChange={(e) => setTask(e.target.value)} placeholder="text-generation" />
          </Field>
          <div className="flex items-center gap-3">
            <Button type="submit" size="sm">
              <Plus aria-hidden="true" />
              Add
            </Button>
            <Notice notice={add.notice} />
          </div>
        </form>
        <div className="grid content-start gap-3">
          <h3 className="text-sm font-semibold">Import JSON Lines</h3>
          <Field label="One example per line" hint='{"input": {...}, "reference": "...", "task": "...", "tags": [...]}, as the export writes'>
            <CodeEditor aria-label="Examples, one per line" language="jsonl" lineNumbers minHeight="9rem" value={jsonl} onChange={setJsonl} />
          </Field>
          <div className="flex items-center gap-2">
            <Button type="button" size="sm" onClick={importLines} disabled={!jsonl.trim()}>
              <Upload aria-hidden="true" />
              Import
            </Button>
            <label className={cn(buttonVariants({ variant: "outline", size: "sm" }), "cursor-pointer")}>
              Choose a file…
              <input type="file" accept=".jsonl,.json,.txt" className="sr-only" onChange={pickFile} />
            </label>
          </div>
        </div>
      </div>
    </>
  );
}

const RUN_COLUMNS: GridColumn<Run>[] = [
  {
    key: "run",
    label: "Run",
    className: "whitespace-normal",
    render: (r) => (
      <>
        <span className="font-medium">{r.name}</span>
        <div className="font-mono text-xs text-muted-foreground [overflow-wrap:anywhere]">
          {r.model_id}
          {r.task ? ` · ${r.task}` : ""}
          {Object.keys(r.params ?? {}).length ? ` · ${JSON.stringify(r.params)}` : ""}
        </div>
      </>
    ),
  },
  { key: "status", label: "Status", render: (r) => <RunStatus run={r} /> },
  { key: "done", label: "Done", align: "right", render: (r) => `${fmtInt(r.done)}/${fmtInt(r.total)}` },
  { key: "failed", label: "Failed", align: "right", className: (r) => (r.failed > 0 ? "text-critical-text" : undefined), render: (r) => fmtInt(r.failed) },
  { key: "started", label: "Started", className: "text-muted-foreground tabular-nums", render: (r) => (r.started ? fmtAgo(r.started) : "–") },
];

function RunsTab(props: { client: ApiClient; dataset: Dataset; runs: Run[] | null; catalog: CatalogInfo; onChange(): Promise<unknown> }) {
  const { client, dataset, runs } = props;
  const [model, setModel] = useState("");
  const [task, setTask] = useState("");
  const [params, setParams] = useState("{}");
  const [name, setName] = useState("");
  const [priority, setPriority] = useState<Priority>("normal");
  const [pair, setPair] = useState<[string, string]>(["", ""]);
  const done = runs?.filter((r) => r.status === "done") ?? [];

  const start = useAction(
    async (event: FormEvent) => {
      event.preventDefault();
      const parsed = parseObject(params || "{}", "Params");
      if (!parsed.ok) throw new Error(parsed.error);
      const run = await client.post("/admin/runs", {
        body: { dataset_id: dataset.id, model_id: model.trim(), task: task || null, params: parsed.value, name: name.trim(), priority },
      });
      setName("");
      await props.onChange();
      location.hash = evalsHref(dataset.id, run.id);
    },
    { notice: true },
  );

  return (
    <>
      <DataGrid columns={RUN_COLUMNS} rows={runs ?? []} rowKey={(r) => r.id} loading={runs === null} rowHref={(r) => evalsHref(dataset.id, r.id)} empty="No runs yet" />
      <div className="grid gap-5 border-t p-4 lg:grid-cols-2">
        <form className="grid gap-3" onSubmit={start.run}>
          <h3 className="text-sm font-semibold">New run</h3>
          <Field label="Model" hint="Any model this service can load">
            <Input value={model} onChange={(e) => setModel(e.target.value)} list={MODEL_IDS_LIST} placeholder="org/model" required className="font-mono text-sm" />
          </Field>
          <div className="grid gap-3 sm:grid-cols-2">
            <SelectField
              label="Task"
              hint="Unset: each example's, else text-generation"
              value={task}
              onChange={setTask}
              none="The examples' own"
              options={props.catalog.tasks.map((t) => ({ value: t, label: t }))}
            />
            <Field label="Name" hint="Unset: the model ID">
              <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. 7B, temperature 0" />
            </Field>
          </div>
          <Field label="Params (JSON, added to every input)" hint='e.g. {"temperature": 0.2, "max_length": 256}'>
            <CodeEditor aria-label="Params" language="json" minHeight="3.5rem" value={params} onChange={setParams} />
          </Field>
          <SelectField
            label="Priority"
            hint="Queued runs start the most urgent first, then one whose model is loaded; live requests are high"
            value={priority}
            onChange={(v) => setPriority(v as Priority)}
            options={PRIORITY_OPTIONS}
          />
          <div className="flex items-center gap-3">
            <Button type="submit" size="sm" disabled={!model.trim() || !dataset.examples}>
              <Play aria-hidden="true" />
              Start run
            </Button>
            {!dataset.examples && <span className="text-xs text-muted-foreground">Add examples first</span>}
            <Notice notice={start.notice} />
          </div>
        </form>
        <div className="grid content-start gap-3">
          <h3 className="text-sm font-semibold">Compare two runs</h3>
          {[0, 1].map((i) => (
            <SelectField
              key={i}
              label={i ? "B" : "A"}
              value={pair[i]}
              onChange={(v) => setPair((p) => (i ? [p[0], v] : [v, p[1]]))}
              none="Pick a finished run…"
              options={done.map((r) => ({ value: r.id, label: r.name }))}
            />
          ))}
          <Button
            type="button"
            size="sm"
            variant="outline"
            disabled={!pair[0] || !pair[1] || pair[0] === pair[1]}
            onClick={() => (location.hash = compareHref(dataset.id, pair[0], pair[1]))}
          >
            <GitCompare aria-hidden="true" />
            Compare
          </Button>
        </div>
      </div>
    </>
  );
}

/** A run's row: what it is doing, with a spinner while it runs or is judged, a dot otherwise. */
function RunStatus({ run }: { run: Run }) {
  const a = run.activity;
  const busy = a?.kind === "running" || a?.kind === "judging";
  const status: Status = { label: runProgress(run), color: a?.error ? "var(--critical)" : statusOf(RUN_STATUS, run.status).color };
  return (
    <div className="min-w-32">
      {busy ? (
        <span className="flex items-center gap-2 whitespace-nowrap text-sm">
          <LoaderCircle className="size-3.5 animate-spin text-muted-foreground" aria-hidden="true" />
          {status.label}
        </span>
      ) : (
        <StatusLabel status={status} className="whitespace-nowrap text-sm" />
      )}
      {busy && a?.total ? <Progress value={((a.done ?? 0) / a.total) * 100} className="mt-1.5 h-1" /> : null}
      {run.error && <div className="mt-0.5 text-xs text-critical-text [overflow-wrap:anywhere]">{run.error}</div>}
    </div>
  );
}
