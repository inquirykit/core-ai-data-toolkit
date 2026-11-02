// /admin#/running: the models in memory and used in the window, with a form
// that loads one (ModelsCard), and the memory limits (LimitsCard).
import { useEffect, useState, type FormEvent } from "react";
import { Boxes, Gauge } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { useAction } from "../shared/action";
import type { ApiClient } from "../shared/api";
import { useTaskLabels } from "../shared/categories";
import { ConfirmButton, Field, Notice, SelectField, StatusLabel, SubmitButton } from "../shared/components";
import { fmtAgo, fmtGB, fmtInt, fmtMs } from "../shared/format";
import { DataGrid, type GridColumn } from "../shared/grid";
import type { LoadedModel, ModelStats, Stats, Status } from "../shared/types";
import { MODEL_IDS_LIST, type CatalogInfo } from "./catalog";

export function RunningSection(props: { stats: Stats | null; client: ApiClient; catalog: CatalogInfo; refresh(): void }) {
  const { stats, client, catalog, refresh } = props;
  if (!stats) return null;
  return (
    <>
      <ModelsCard stats={stats} client={client} catalog={catalog} refresh={refresh} />
      <LimitsCard stats={stats} client={client} refresh={refresh} />
    </>
  );
}

/** Keeps ``task`` one of ``tasks``, preferring text-generation. */
function useValidTask(tasks: string[]): [string, (task: string) => void] {
  const [task, setTask] = useState("text-generation");
  useEffect(() => {
    if (tasks.length && !tasks.includes(task)) setTask(tasks.includes("text-generation") ? "text-generation" : tasks[0]);
  }, [tasks, task]);
  return [task, setTask];
}

function TaskSelect(props: { tasks: string[]; label(task: string): string; value: string; onChange(task: string): void }) {
  return (
    <SelectField
      label="Task"
      className="w-52"
      value={props.value}
      onChange={props.onChange}
      options={props.tasks.map((t) => ({ value: t, label: props.label(t) }))}
    />
  );
}

// ----------------------------------------------------------------- limits
/** The PUT /admin/limits body from the form's fields, or what is wrong with them. */
export function limitsBody(memory: string, models: string): { max_memory_gb: number; max_models: number } | string {
  const gb = Number(memory);
  const count = Number(models);
  if (!memory.trim() || !Number.isFinite(gb) || gb < 0) return "The memory budget must be 0 GB or more.";
  if (!Number.isInteger(count) || count < 1) return "Max models must be a whole number, 1 or more.";
  return { max_memory_gb: gb, max_models: count };
}

/** What the device holds, to size the budget against. */
function deviceMemory(stats: Stats): string | null {
  const gpu = stats.process.gpus[0];
  if (gpu) return `GPU ${gpu.index}: ${fmtGB(gpu.total_gb)}`;
  const host = stats.process.host_memory;
  return host ? `${stats.process.device === "mps" ? "unified" : "host"} memory: ${fmtGB(host.total_gb)}` : null;
}

function LimitsCard(props: { stats: Stats; client: ApiClient; refresh(): void }) {
  const { client, refresh } = props;
  const cache = props.stats.cache;
  const [memory, setMemory] = useState(String(cache.max_memory_gb));
  const [models, setModels] = useState(String(cache.max_models));
  // The fields follow the server's values until someone edits them.
  const [edited, setEdited] = useState(false);

  useEffect(() => {
    if (edited) return;
    setMemory(String(cache.max_memory_gb));
    setModels(String(cache.max_models));
  }, [edited, cache.max_memory_gb, cache.max_models]);

  const save = useAction(
    async (event: FormEvent) => {
      event.preventDefault();
      const body = limitsBody(memory, models);
      if (typeof body === "string") throw new Error(body);
      try {
        const result = await client.put("/admin/limits", { body });
        const evicted = result.evicted.length ? `Unloaded ${result.evicted.join(", ")} to fit.` : "";
        toast.success(`Applied and saved to ${result.env_file}.`, {
          description: [evicted, ...result.notes].filter(Boolean).join(" ") || undefined,
        });
        setEdited(false);
      } finally {
        refresh();
      }
    },
    { notice: true },
  );

  const device = deviceMemory(props.stats);
  const edit = (set: (v: string) => void) => (e: { target: { value: string } }) => {
    set(e.target.value);
    setEdited(true);
  };
  return (
    <Card>
      <CardHeader>
        <CardTitle>Memory limits</CardTitle>
        <CardDescription>
          {`${fmtGB(cache.memory_used_gb)} budgeted by ${cache.cache_size} loaded`}
          {device ? ` · ${device}` : ""}
        </CardDescription>
      </CardHeader>
      <CardContent className="grid gap-3">
        <form className="flex flex-wrap items-end gap-3" onSubmit={save.run}>
          <Field label="Memory budget (GB)">
            <Input
              type="number"
              min={0}
              step={0.5}
              required
              className="w-[140px] tabular-nums"
              value={memory}
              onChange={edit(setMemory)}
            />
          </Field>
          <Field label="Max models">
            <Input
              type="number"
              min={1}
              step={1}
              required
              className="w-[110px] tabular-nums"
              value={models}
              onChange={edit(setModels)}
            />
          </Field>
          <SubmitButton busy={save.busy} icon={<Gauge aria-hidden="true" />} disabled={!edited}>
            Apply
          </SubmitButton>
        </form>
        <p className="max-w-prose text-xs leading-relaxed text-muted-foreground">
          Models are budgeted by their estimated size; 0 GB means no budget, only the model count. Lower limits unload idle
          models at once. Saved to the settings file, so they last across restarts.
        </p>
        <Notice notice={save.notice} />
      </CardContent>
    </Card>
  );
}

// ----------------------------------------------------------------- models
interface ModelRow {
  key: string;
  model_id: string;
  task: string;
  loaded?: LoadedModel;
  stats?: ModelStats;
}

/** Loaded models and models used in the window, loaded first, then by requests. */
function modelRows(stats: Stats): ModelRow[] {
  const rows = new Map<string, ModelRow>();
  for (const [key, info] of Object.entries(stats.cache.model_details)) {
    rows.set(key, { key, model_id: info.model_id, task: info.task, loaded: info });
  }
  for (const m of stats.models) {
    const row = rows.get(m.model_key) ?? { key: m.model_key, model_id: m.model_id, task: m.task };
    rows.set(m.model_key, { ...row, stats: m });
  }
  return [...rows.values()].sort(
    (a, b) => Number(Boolean(b.loaded)) - Number(Boolean(a.loaded)) || (b.stats?.requests ?? 0) - (a.stats?.requests ?? 0),
  );
}

const DTYPES = ["auto", "float16", "bfloat16", "float32"];

const failedOf = (s?: ModelStats): number => (s?.errors ?? 0) + (s?.client_errors ?? 0);
const LOADED: Status = { label: "Loaded", color: "var(--good)" };
const NOT_LOADED: Status = { label: "Not loaded", color: "var(--axis)" };

function ModelsCard(props: { stats: Stats; client: ApiClient; catalog: CatalogInfo; refresh(): void }) {
  const { client, catalog, refresh } = props;
  const [modelId, setModelId] = useState("");
  const [task, setTask] = useValidTask(catalog.tasks);
  const taskLabel = useTaskLabels(client);
  const [dtype, setDtype] = useState("auto");

  // ``from`` names the button that asked (the form, or a row's key), to disable it alone.
  const load = useAction(
    async (id: string, forTask: string, torchDtype: string, _from: string) => {
      // One toast from start to finish, turning into the outcome: a load can take minutes.
      const toastId = toast.loading(`Loading ${id} for ${forTask}…`, { description: "Large models can take minutes." });
      const started = performance.now();
      try {
        await client.post("/models/load", { body: { model_id: id, task: forTask, torch_dtype: torchDtype } });
        toast.success(`Loaded ${id} in ${fmtMs(performance.now() - started)}.`, { id: toastId, description: undefined });
      } catch (e) {
        toast.dismiss(toastId);
        throw e;
      } finally {
        refresh();
      }
    },
    { failed: (id) => `Could not load ${id}` },
  );

  const unload = useAction(
    (row: ModelRow) => client.delete("/models/{model_id}/{task}", { params: { path: { model_id: row.model_id, task: row.task } } }).finally(refresh),
    { success: (_, row) => `Unloaded ${row.model_id}.`, failed: (row) => `Could not unload ${row.model_id}` },
  );

  // Asked to confirm first, by the dialog around the "Unload all" button.
  const clearAll = useAction(() => client.delete("/models/clear").finally(refresh), { success: "Unloaded all models.", failed: "Could not unload" });

  // The row (or form) whose request is running, to disable its button.
  const busy = load.running?.[3] ?? unload.running?.[0].key ?? (clearAll.busy ? "all" : null);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    load.run(modelId.trim(), task, dtype, "form");
  };
  const rows = modelRows(props.stats);

  const columns: GridColumn<ModelRow>[] = [
    { key: "model", label: "Model", headClassName: "pl-4", className: "pl-4 font-mono text-[13px] font-medium", render: (r) => r.model_id },
    { key: "task", label: "Task", className: "text-muted-foreground", render: (r) => r.task },
    {
      key: "state",
      label: "State",
      render: (r) =>
        r.loaded ? (
          <>
            <StatusLabel status={LOADED} live />
            <div className="mt-0.5 pl-4 text-xs text-muted-foreground">
              {`${r.loaded.kind === "server" ? "server" : r.loaded.torch_dtype} · ${r.loaded.memory_usage != null ? `${fmtGB(r.loaded.memory_usage)} · ` : ""}${fmtAgo(r.loaded.loaded_at)}`}
            </div>
          </>
        ) : (
          <StatusLabel status={NOT_LOADED} className="text-muted-foreground" />
        ),
    },
    { key: "requests", label: "Requests", align: "right", render: (r) => fmtInt(r.stats?.requests ?? 0) },
    {
      key: "failed",
      label: "Failed",
      align: "right",
      className: (r) => (failedOf(r.stats) ? "text-critical-text" : "text-muted-foreground"),
      render: (r) => (failedOf(r.stats) ? fmtInt(failedOf(r.stats)) : "–"),
    },
    { key: "p50", label: "p50", align: "right", render: (r) => fmtMs(r.stats?.p50_ms) },
    { key: "p95", label: "p95", align: "right", render: (r) => fmtMs(r.stats?.p95_ms) },
    { key: "cold", label: "Cold starts", align: "right", render: (r) => fmtInt(r.stats?.cold_starts ?? 0) },
    {
      key: "last",
      label: "Last used",
      className: "text-muted-foreground",
      render: (r) => fmtAgo(Math.max(r.loaded?.last_used ?? 0, r.stats?.last_request ?? 0)),
    },
    {
      key: "actions",
      headClassName: "pr-4",
      className: "pr-4 text-right",
      render: (r) => {
        // Offer a load only for models known to work: listed, or answered at least once.
        const loadable = (r.stats?.requests ?? 0) > failedOf(r.stats) || catalog.taskOf[r.model_id] === r.task;
        if (r.loaded) {
          return (
            <Button type="button" variant="outline" size="sm" disabled={busy === r.key} onClick={() => unload.run(r)}>
              Unload
            </Button>
          );
        }
        return loadable ? (
          <Button type="button" variant="outline" size="sm" disabled={busy === r.key} onClick={() => load.run(r.model_id, r.task, "auto", r.key)}>
            Load
          </Button>
        ) : null;
      },
    },
  ];

  return (
    <Card>
      <CardHeader>
        <CardTitle>Loaded and recently used</CardTitle>
        <CardDescription>Models in memory, and models that answered requests in this window</CardDescription>
        <CardAction className="flex flex-wrap justify-end gap-2">
          <Button asChild variant="outline" size="sm">
            <a href="#/models">
              <Boxes aria-hidden="true" />
              Browse models
            </a>
          </Button>
          <ConfirmButton
            label="Unload all"
            title="Unload every model from memory?"
            body="Requests will reload them as needed."
            disabled={busy === "all"}
            onConfirm={clearAll.run}
          />
        </CardAction>
      </CardHeader>
      <CardContent className="grid gap-5">
        <form className="flex flex-wrap items-end gap-3" onSubmit={submit}>
          <Field label="Model ID" className="min-w-56 flex-1">
            <Input
              list={MODEL_IDS_LIST}
              placeholder="Qwen/Qwen2.5-0.5B-Instruct, org/model, vega-4b…"
              required
              autoComplete="off"
              value={modelId}
              onChange={(e) => {
                setModelId(e.target.value);
                const known = catalog.taskOf[e.target.value.trim()];
                if (known) setTask(known);
              }}
            />
          </Field>
          <TaskSelect tasks={catalog.tasks} label={taskLabel} value={task} onChange={setTask} />
          <SelectField label="Dtype" className="w-32" value={dtype} onChange={setDtype} options={DTYPES.map((d) => ({ value: d, label: d }))} />
          <SubmitButton busy={busy === "form"}>Load</SubmitButton>
        </form>
        <div className="overflow-hidden rounded-lg border">
          <DataGrid columns={columns} rows={rows} rowKey={(r) => r.key} headerClassName="bg-muted/50" empty="No models loaded or used in this window" />
        </div>
      </CardContent>
    </Card>
  );
}
