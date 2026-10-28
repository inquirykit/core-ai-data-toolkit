import { useEffect, useState, type FormEvent, type KeyboardEvent, type ReactNode } from "react";
import {
  Boxes,
  ChevronRight,
  CircleAlert,
  Gauge,
  HardDrive,
  Layers,
  Timer,
  TriangleAlert,
  Zap,
  type LucideIcon,
} from "lucide-react";
import { motion } from "motion/react";
import { toast } from "sonner";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { Input } from "@/components/ui/input";
import { Progress } from "@/components/ui/progress";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, TableBody, TableCell, TableHead, TableRow } from "@/components/ui/table";
import { cn } from "@/lib/utils";
import { useAction } from "../shared/action";
import type { ApiClient } from "../shared/api";
import { ConfirmButton, Field, Notice, SelectField, StatusLabel, SubmitButton } from "../shared/components";
import type { Status } from "../shared/types";
import { fmtAgo, fmtCompact, fmtDuration, fmtGB, fmtInt, fmtMs, fmtPct, fmtTime, statusOf } from "../shared/format";
import { DataGrid, type GridColumn } from "../shared/grid";
import type { AdminConfig, LoadedModel, ModelEvent, ModelStats, Stats } from "../shared/types";
import type { RequestItem } from "./activity";

/** /models/list, as the forms use it. */
export interface CatalogInfo {
  tasks: string[];
  /** The task each listed model ID is registered for (the first, if several). */
  taskOf: Record<string, string>;
}

export const MODEL_IDS_LIST = "model-ids";

/** Two cards side by side on a wide screen: the charts, the activity. */
export const GRID_2 = "grid gap-5 lg:grid-cols-2";

/** A figure that slides in afresh each time it changes. */
function TileValue({ value }: { value: string }) {
  return (
    <CardTitle className="text-2xl font-semibold tabular-nums @[200px]/card:text-3xl">
      <motion.span
        key={value}
        className="inline-block"
        initial={{ opacity: 0, y: 8, filter: "blur(4px)" }}
        animate={{ opacity: 1, y: 0, filter: "blur(0px)" }}
        transition={{ duration: 0.35, ease: [0.22, 1, 0.36, 1] }}
      >
        {value}
      </motion.span>
    </CardTitle>
  );
}

/** A card whose body folds away under its title. */
export function CollapsibleCard(props: { title: ReactNode; defaultOpen?: boolean; children: ReactNode }) {
  return (
    <Card className="gap-0 py-0">
      <Collapsible defaultOpen={props.defaultOpen}>
        <CollapsibleTrigger className="group flex w-full items-center gap-2 rounded-xl px-6 py-4 text-left text-sm font-semibold outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50">
          <ChevronRight
            className="size-4 text-muted-foreground transition-transform duration-200 group-hover:text-foreground group-data-[state=open]:rotate-90"
            aria-hidden="true"
          />
          {props.title}
        </CollapsibleTrigger>
        <CollapsibleContent className="overflow-hidden data-[state=closed]:animate-collapsible-up data-[state=open]:animate-collapsible-down">
          <div className="px-6 pb-6">{props.children}</div>
        </CollapsibleContent>
      </Collapsible>
    </Card>
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

function TaskSelect(props: { tasks: string[]; value: string; onChange(task: string): void }) {
  return <SelectField label="Task" className="w-52" value={props.value} onChange={props.onChange} options={props.tasks.map((t) => ({ value: t, label: t }))} />;
}

// ------------------------------------------------------------------ tiles
// One column, then two (the memory card across both), then all five in a row.
const TILE_GRID = "stagger grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-5";
const MEMORY_SPAN = "sm:col-span-2 xl:col-span-1";

/** A summary card (shadcn "section card"): label and icon, the figure, a line under it. */
function Tile(props: { label: string; icon: LucideIcon; value: string; className?: string; children: ReactNode }) {
  return (
    <Card
      className={cn(
        "@container/card gap-3 bg-linear-to-t from-muted/40 to-card py-5 dark:from-primary/5 transition-[translate,box-shadow] duration-300 ease-out hover:-translate-y-0.5 hover:shadow-md",
        props.className,
      )}
    >
      <CardHeader className="px-5">
        <CardDescription>{props.label}</CardDescription>
        <TileValue value={props.value} />
        <CardAction>
          <span className="grid size-8 place-items-center rounded-md border bg-background/60 text-muted-foreground">
            <props.icon className="size-4" aria-hidden="true" />
          </span>
        </CardAction>
      </CardHeader>
      <CardFooter className="mt-auto flex-col items-stretch gap-2.5 px-5 text-xs text-muted-foreground">{props.children}</CardFooter>
    </Card>
  );
}

export function Tiles({ stats }: { stats: Stats }) {
  const t = stats.totals;
  const cache = stats.cache;
  const share = cache.max_memory_gb ? cache.memory_used_gb / cache.max_memory_gb : 0;
  const tiles = [
    {
      label: "Requests",
      icon: Zap,
      value: fmtCompact(t.requests),
      sub: `${t.requests_per_min.toFixed(t.requests_per_min < 10 ? 2 : 0)} per minute`,
    },
    {
      label: "Failed",
      icon: CircleAlert,
      value: fmtCompact(t.client_errors + t.errors),
      sub: t.requests ? `${fmtInt(t.errors)} server (${fmtPct(t.error_rate)}) · ${fmtInt(t.client_errors)} client` : "no requests",
    },
    { label: "Latency p95", icon: Timer, value: fmtMs(t.p95_ms), sub: `p50 ${fmtMs(t.p50_ms)}` },
    { label: "Loaded models", icon: Layers, value: `${cache.cache_size} / ${cache.max_models}`, sub: `on ${stats.process.device}` },
  ] satisfies { label: string; icon: LucideIcon; value: string; sub: string }[];
  return (
    <section className={TILE_GRID} aria-label="Summary">
      {tiles.map((tile) => (
        <Tile key={tile.label} label={tile.label} icon={tile.icon} value={tile.value}>
          {tile.sub}
        </Tile>
      ))}
      <Tile label="Model memory" icon={HardDrive} value={fmtGB(cache.memory_used_gb)} className={MEMORY_SPAN}>
        <div>
          {cache.max_memory_gb > 0 ? `of ${fmtGB(cache.max_memory_gb)} budget` : "no budget"}
          {cache.free_memory_gb != null ? ` · ${fmtGB(cache.free_memory_gb)} free` : ""}
          {share >= 1 ? " · over limit" : ""}
        </div>
        <Progress
          aria-hidden="true"
          value={Math.min(100, share * 100)}
          className="h-1.5 bg-primary/10"
          indicatorClassName={share >= 1 ? "bg-critical" : share >= 0.8 ? "bg-warning" : "bg-series-1"}
        />
      </Tile>
    </section>
  );
}

/** The overview's shape, shimmering, until the first figures arrive. */
export function OverviewSkeleton() {
  return (
    <>
      <section className={TILE_GRID} aria-hidden="true">
        {[0, 1, 2, 3, 4].map((i) => (
          <Card className={cn("gap-3 py-5", i === 4 && MEMORY_SPAN)} key={i}>
            <CardHeader className="px-5">
              <Skeleton className="h-4 w-20" />
              <Skeleton className="mt-1 h-7 w-24" />
              <CardAction>
                <Skeleton className="size-8" />
              </CardAction>
            </CardHeader>
            <CardFooter className="px-5">
              <Skeleton className="h-3 w-28" />
            </CardFooter>
          </Card>
        ))}
      </section>
      <section className={GRID_2} aria-hidden="true">
        {[0, 1].map((i) => (
          <Card className="gap-4" key={i}>
            <CardHeader>
              <Skeleton className="h-4 w-24" />
              <Skeleton className="h-3.5 w-44" />
            </CardHeader>
            <CardContent className="px-4">
              <Skeleton className="h-[200px]" />
            </CardContent>
          </Card>
        ))}
      </section>
    </>
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

export function LimitsCard(props: { stats: Stats; client: ApiClient; refresh(): void }) {
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

export function ModelsCard(props: { stats: Stats; client: ApiClient; catalog: CatalogInfo; refresh(): void }) {
  const { client, catalog, refresh } = props;
  const [modelId, setModelId] = useState("");
  const [task, setTask] = useValidTask(catalog.tasks);
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
          <TaskSelect tasks={catalog.tasks} value={task} onChange={setTask} />
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

// --------------------------------------------------------------- activity
/**
 * Each kind of model event: its label and colour (status hues, so the chart and the feed agree). Still tuples,
 * read through statusOf, as every status table is; the activity chart (ActivityChart.tsx) reads them too.
 */
export const EVENT_STYLE: Record<string, Status> = {
  load: { label: "Loaded", color: "var(--good)" },
  load_failed: { label: "Load failed", color: "var(--critical)" },
  evict: { label: "Evicted", color: "var(--warning)" },
  unload: { label: "Unloaded", color: "var(--axis)" },
  limits: { label: "Limits changed", color: "var(--series-1)" },
};
const EVICT_REASON: Record<string, string> = { max_models: "MAX_MODELS reached", max_memory_gb: "over MAX_MEMORY_GB" };

export function EventsFeed({ events }: { events: ModelEvent[] }) {
  return (
    <ul className="grid text-sm">
      {events.length ? (
        events.map((e) => {
          const { label, color } = statusOf(EVENT_STYLE, e.kind);
          const detail = e.kind === "evict" && e.detail ? (EVICT_REASON[e.detail] ?? e.detail) : e.detail;
          return (
            // Rows are keyed by id, so only the new ones play the entrance.
            <li
              key={e.id}
              className="grid grid-cols-[auto_minmax(0,1fr)] items-center gap-x-3 gap-y-1 border-b py-2.5 duration-500 animate-in fade-in-0 slide-in-from-top-1 last:border-0 sm:grid-cols-[4.5rem_7rem_minmax(0,1fr)]"
            >
              <span className="text-xs text-muted-foreground tabular-nums" title={new Date(e.ts * 1000).toLocaleString()}>
                {fmtTime(e.ts)}
              </span>
              <StatusLabel status={{ label, color }} className="font-medium" />
              <span className="min-w-0 [overflow-wrap:anywhere] max-sm:col-span-2">
                <span className="font-mono text-[13px]">{e.model_key}</span>
                <span className="text-muted-foreground">
                  {(detail ? ` · ${detail}` : "") + (e.duration_ms != null ? ` · ${fmtMs(e.duration_ms)}` : "")}
                </span>
              </span>
            </li>
          );
        })
      ) : (
        <li className="py-10 text-center text-muted-foreground">No model events yet</li>
      )}
    </ul>
  );
}

/** A request's outcome: its label and colour, here and in the trace dialog. */
export const STATUS_STYLE: Record<string, Status> = {
  ok: { label: "OK", color: "var(--good)" },
  client_error: { label: "Client error", color: "var(--serious)" },
  error: { label: "Server error", color: "var(--critical)" },
};

/**
 * The requests of Activity & logs. A row opens its input and output
 * (onOpen with its trace id); rows found by searching the traces also show
 * the first words of the request.
 */
export function RecentRequests({ requests, withPreview, onOpen }: { requests: RequestItem[]; withPreview: boolean; onOpen(traceId: string): void }) {
  const columns: GridColumn<RequestItem>[] = [
    { key: "time", label: "Time", className: "text-muted-foreground tabular-nums", title: (r) => new Date(r.ts * 1000).toLocaleString(), render: (r) => fmtTime(r.ts) },
    {
      key: "model",
      label: "Model",
      className: "whitespace-normal",
      render: (r) => (
        <>
          <span className="font-mono text-[13px] [overflow-wrap:anywhere]">{r.model_id}</span>
          <div className="text-xs text-muted-foreground">
            {r.task}
            {r.tags.map((tag) => (
              <Badge key={tag} variant="secondary" className="ml-1.5 px-1.5 py-0 text-[11px]">
                {tag}
              </Badge>
            ))}
          </div>
        </>
      ),
    },
    {
      key: "status",
      label: "Status",
      className: "whitespace-normal",
      render: (r) => (
        <>
          <StatusLabel status={statusOf(STATUS_STYLE, r.status)} className="whitespace-nowrap" />
          {r.error && <div className="mt-0.5 max-w-64 text-xs text-muted-foreground [overflow-wrap:anywhere]">{r.error}</div>}
        </>
      ),
    },
    {
      key: "duration",
      label: "Duration",
      align: "right",
      render: (r) => (
        <>
          {fmtMs(r.duration_ms)}
          {r.cold && <div className="text-xs text-muted-foreground">cold start</div>}
        </>
      ),
    },
  ];
  if (withPreview) {
    columns.push({
      key: "request",
      label: "Request",
      className: "max-w-md whitespace-normal",
      render: (r) => <span className="line-clamp-2 text-sm [overflow-wrap:anywhere]">{r.preview || <span className="text-muted-foreground">(no text)</span>}</span>,
    });
  }
  // A row opens its trace by click or Enter; one without a trace says why not.
  const rowProps = (r: RequestItem) => {
    const open = r.trace_id ? () => onOpen(r.trace_id!) : undefined;
    return {
      className: open && "cursor-pointer",
      tabIndex: open ? 0 : undefined,
      onClick: open,
      onKeyDown: open && ((e: KeyboardEvent) => e.key === "Enter" && open()),
      title: open ? "Input and output" : "Recorded before tracing: no input or output",
    };
  };
  return <DataGrid columns={columns} rows={requests} rowKey={(r) => r.key} rowProps={rowProps} empty={withPreview ? "No requests match" : "No requests yet"} />;
}

// ------------------------------------------------------ config and process
function KeyValues({ entries }: { entries: [string, unknown][] }) {
  return (
    <div className="overflow-hidden rounded-lg border">
      <Table>
        <TableBody>
          {entries.map(([k, v]) => (
            <TableRow key={k}>
              <TableHead className="h-auto w-[1%] bg-muted/40 py-2 pr-6 pl-3 font-mono text-xs font-normal">{k}</TableHead>
              <TableCell className="py-2 pr-3 whitespace-normal [overflow-wrap:anywhere]">
                {Array.isArray(v) ? (v.length ? v.join(", ") : "(empty)") : String(v)}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}

function processEntries(stats: Stats): [string, unknown][] {
  const p = stats.process;
  const entries: [string, unknown][] = [
    ["device", p.device],
    ["pid", p.pid],
    ["python", p.python],
    ["uptime", fmtDuration(p.uptime_s)],
  ];
  if (p.cpu_percent != null) entries.push(["cpu", `${p.cpu_percent.toFixed(0)}% of 1 core · ${p.cpu_count} cores`]);
  if (p.rss_gb != null) entries.push(["process memory", fmtGB(p.rss_gb)]);
  if (p.host_memory) {
    entries.push(["host memory", `${fmtGB(p.host_memory.available_gb)} free of ${fmtGB(p.host_memory.total_gb)}`]);
  }
  for (const g of p.gpus) entries.push([`gpu ${g.index}`, `${g.name} · ${fmtGB(g.allocated_gb)} used of ${fmtGB(g.total_gb)}`]);
  const m = stats.monitoring;
  entries.push([
    "monitoring",
    m.enabled ? `on · kept ${m.retention_days} days${m.dropped ? ` · ${m.dropped} rows dropped` : ""}` : "off",
  ]);
  return entries;
}

export function ConfigAndProcess({ config, stats, open }: { config: AdminConfig; stats: Stats | null; open?: boolean }) {
  return (
    <CollapsibleCard title="Configuration and process" defaultOpen={open}>
      <div className="grid items-start gap-5 lg:grid-cols-2">
        <KeyValues entries={Object.entries(config.settings)} />
        <div>{stats && <KeyValues entries={processEntries(stats)} />}</div>
      </div>
    </CollapsibleCard>
  );
}

export function Warnings({ warnings }: { warnings: string[] }) {
  if (!warnings.length) return null;
  return (
    <Alert variant="warning" role="note">
      <TriangleAlert aria-hidden="true" />
      <AlertTitle>Check before exposing this service</AlertTitle>
      <AlertDescription>
        <ul className="grid list-disc gap-0.5 pl-4">
          {warnings.map((w) => (
            <li key={w}>{w}</li>
          ))}
        </ul>
      </AlertDescription>
    </Alert>
  );
}
