import { useId, useState } from "react";
import { ArrowLeft, Check, Download, FlaskConical, Info, Loader2, MessagesSquare, PowerOff } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { cn } from "@/lib/utils";
import { useAction } from "../action";
import { AuthError, type ApiClient } from "../api";
import { Notice, type NoticeState } from "../components";
import { fmtGB, fmtMs } from "../format";
import { errorText, useReloadOn } from "../query";
import { TryPanel } from "../task-io/TryPanel";
import { CHAT_TASKS, memoryBar, planSummary, type CatalogModel, type LoadPlan } from "./catalog";
import { message } from "../errors";
import { kvClass, Section, StateBadge, Tags, type PickedModel } from "./parts";

const DTYPES = ["auto", "float16", "bfloat16", "float32"];

/**
 * One model, in place of the list, in two views: Overview (the model, its
 * status, the memory loading it would take, from GET /models/estimate) and
 * Try (send it requests, see TryPanel). The actions stay pinned at the bottom.
 */
export function ModelDetails(props: {
  client: ApiClient;
  picked: PickedModel;
  /** Its catalog entry; missing for an ID typed in by hand */
  model?: CatalogModel;
  pick?: { label: string; onPick(model: PickedModel): void };
  onBack(): void;
  onChange(): void;
  onDone(): void;
}) {
  const { client, picked, model, pick } = props;
  const [dtype, setDtype] = useState("auto");
  const estimate = client.useQuery("get", "/models/estimate", { params: { query: { model_id: picked.id, task: picked.task, torch_dtype: dtype } } });
  // What loading it would unload changes as models are loaded and unloaded.
  useReloadOn(model?.status, estimate.refetch);
  // A plan is for these settings only: none shows while the next one loads, or when it failed.
  const plan = estimate.isFetching || estimate.error ? null : (estimate.data ?? null);
  const planError = estimate.isFetching ? "" : errorText(estimate.error);
  const [view, setView] = useState<"overview" | "try">("overview");
  const dtypeId = useId();
  const server = model?.kind === "server";
  const loaded = model?.status === "loaded";

  /** Loads or unloads the model: the outcome goes under the buttons (the notice) and into the toast. */
  const act = useAction(
    async (kind: "load" | "unload"): Promise<NoticeState> => {
      const started = performance.now();
      // One toast per model and action, from "Loading…" to its outcome: it outlasts this view if one goes back to the list.
      const toastId = `${kind}:${picked.task}:${picked.id}`;
      toast.loading(kind === "load" ? `Loading ${picked.id}…` : `Unloading ${picked.id}…`, {
        id: toastId,
        description: kind === "load" ? "Large models can take minutes." : undefined,
      });
      try {
        if (kind === "load") {
          await client.post("/models/load", { body: { model_id: picked.id, task: picked.task, torch_dtype: dtype } });
          const took = fmtMs(performance.now() - started);
          toast.success(`Loaded ${picked.id}`, { id: toastId, description: `In ${took}.` });
          return { text: `Loaded in ${took}.`, kind: "ok" };
        }
        await client.delete("/models/{model_id}/{task}", { params: { path: { model_id: picked.id, task: picked.task } } });
        toast.success(`Unloaded ${picked.id}`, { id: toastId, description: undefined });
        return { text: "Unloaded.", kind: "ok" };
      } catch (e) {
        // The sign-in prompt answers an AuthError; anything else turns the toast into the failure and names the action in the notice.
        if (e instanceof AuthError) {
          toast.dismiss(toastId);
          throw e;
        }
        toast.error(`Could not ${kind} ${picked.id}`, { id: toastId, description: message(e) });
        throw new Error(`Could not ${kind}: ${message(e)}`);
      } finally {
        props.onChange();
      }
    },
    { notice: true, success: (outcome) => outcome },
  );
  const busy = act.busy;
  // While it runs, the notice says so; after, it says how it went.
  const notice: NoticeState = act.running ? { text: act.running[0] === "load" ? "Loading… large models can take minutes." : "Unloading…" } : act.notice;

  return (
    <section className="flex min-h-0 min-w-0 flex-1 flex-col" aria-label={picked.id}>
      <div className="flex flex-none items-start justify-between gap-3 px-5 pt-3 pb-3">
        <div className="min-w-0">
          <Button type="button" variant="ghost" size="sm" className="-ml-2 mb-1 h-7 gap-1 px-2 text-muted-foreground" onClick={props.onBack}>
            <ArrowLeft aria-hidden="true" />
            {picked.task}
          </Button>
          <h3 className="font-mono text-[15px] font-semibold wrap-anywhere">{picked.id}</h3>
          {model?.description && <p className="mt-1 text-sm text-muted-foreground">{model.description}</p>}
        </div>
        {model && <StateBadge model={model} />}
      </div>

      <Tabs value={view} onValueChange={(v) => setView(v as "overview" | "try")} className="min-h-0 flex-1 gap-0">
        <div className="flex-none border-b px-5 pb-3">
          <TabsList aria-label="View">
            <TabsTrigger value="overview" className="px-3">
              <Info aria-hidden="true" />
              Overview
            </TabsTrigger>
            <TabsTrigger value="try" className="px-3">
              <FlaskConical aria-hidden="true" />
              Try it
            </TabsTrigger>
          </TabsList>
        </div>

        <TabsContent value="try" className="min-h-0 animate-rise overflow-y-auto px-5 pt-4 pb-5">
          <TryPanel client={client} modelId={picked.id} task={picked.task} loaded={loaded} onRan={props.onChange} />
        </TabsContent>
        <TabsContent value="overview" className="stagger grid min-h-0 content-start gap-3.5 overflow-y-auto px-5 pt-4 pb-5">
          <Section title="Model">
            <dl className={kvClass}>
              <dt>Task</dt>
              <dd>{picked.task}</dd>
              <dt>Served by</dt>
              <dd>
                {model?.served_by ? (
                  <>
                    <span className="font-mono text-[13px]">{model.served_by}</span>
                    {model.generic && <span className="text-muted-foreground"> · the generic loader for this task</span>}
                  </>
                ) : (
                  <span className="text-muted-foreground">Decided when it loads</span>
                )}
              </dd>
              <dt>Runs</dt>
              <dd>{server ? "In its own server process (MODEL_SERVERS_FILE)" : "In this process"}</dd>
              <dt>Weights</dt>
              <dd>
                {server
                  ? "Managed by its server"
                  : model?.on_disk_gb != null
                    ? `${fmtGB(model.on_disk_gb)} downloaded`
                    : "Not downloaded: the first load downloads them"}
              </dd>
              {(model?.tags ?? []).length > 0 && (
                <>
                  <dt>Tags</dt>
                  <dd>
                    <Tags tags={model?.tags} />
                  </dd>
                </>
              )}
              {!model && (
                <>
                  <dt>Listed</dt>
                  <dd className="text-muted-foreground">No: an ID typed in by hand</dd>
                </>
              )}
            </dl>
          </Section>

          <Section title="Memory" aside={!server && <span className="text-xs text-muted-foreground">in {dtype}</span>}>
            <MemoryPlan plan={plan} error={planError} />
          </Section>
        </TabsContent>
      </Tabs>

      <div className="flex flex-none flex-wrap items-center gap-2 border-t bg-muted/40 px-5 py-3">
        {!server && (
          <div className="flex items-center gap-2">
            <Label htmlFor={dtypeId} className="font-normal text-muted-foreground">
              Dtype
            </Label>
            <Select value={dtype} disabled={busy || loaded} onValueChange={setDtype}>
              <SelectTrigger id={dtypeId} size="sm" className="w-28 bg-background">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {DTYPES.map((d) => (
                  <SelectItem key={d} value={d}>
                    {d}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        )}
        {loaded ? (
          <Button type="button" variant="outline" size="sm" disabled={busy} onClick={() => act.run("unload")}>
            {busy ? <Loader2 className="animate-spin" aria-hidden="true" /> : <PowerOff aria-hidden="true" />}
            Unload
          </Button>
        ) : (
          <Button type="button" size="sm" disabled={busy} onClick={() => act.run("load")}>
            {busy ? <Loader2 className="animate-spin" aria-hidden="true" /> : <Download aria-hidden="true" />}
            Load
          </Button>
        )}
        {pick && (
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() => {
              pick.onPick(picked);
              props.onDone();
            }}
          >
            <Check aria-hidden="true" />
            {pick.label}
          </Button>
        )}
        {CHAT_TASKS.includes(picked.task) && (
          <Button asChild variant="outline" size="sm">
            <a href={`/playground?model=${encodeURIComponent(picked.id)}`}>
              <MessagesSquare aria-hidden="true" />
              Chat in playground
            </a>
          </Button>
        )}
        <Notice notice={notice} className="basis-full" />
      </div>
    </section>
  );
}

/** The plan in a sentence, a bar of memory in use plus this model against the budget, and the figures. */
function MemoryPlan({ plan, error }: { plan: LoadPlan | null; error: string }) {
  const box = "rounded-lg px-3 py-2 text-sm";
  if (!plan) {
    return (
      <p className={cn(box, "flex items-center gap-2 bg-muted text-muted-foreground")}>
        {!error && <Loader2 className="size-4 animate-spin" aria-hidden="true" />}
        {error ? `No estimate: ${error}` : "Estimating…"}
      </p>
    );
  }
  const summary = planSummary(plan);
  const bar = memoryBar(plan);
  const color = { ok: "var(--series-1)", warn: "var(--warning)", bad: "var(--critical)" }[summary.kind];
  const key = "mr-2 inline-block h-2.5 w-2.5 rounded-[3px] align-[-1px]";
  return (
    <>
      <p
        className={cn(
          box,
          "animate-in duration-300 fade-in-0",
          summary.kind === "ok" && "bg-good/10 text-good-text",
          summary.kind === "warn" && "bg-warning/10 shadow-[inset_3px_0_0_var(--warning)]",
          summary.kind === "bad" && "bg-critical/10 text-critical-text shadow-[inset_3px_0_0_var(--critical)]",
        )}
        aria-live="polite"
      >
        {summary.text}
      </p>
      {bar && (
        <div className="relative flex h-2 rounded-full bg-grid" aria-hidden="true">
          <span className="rounded-l-full bg-axis transition-[width] duration-600 ease-out" style={{ width: `${bar.used * 100}%` }} />
          <span
            className="rounded-r-full transition-[width,background-color] duration-600 ease-out"
            style={{ width: `${bar.need * 100}%`, background: color }}
          />
          {bar.budget != null && bar.budget < 1 && (
            <span className="absolute -inset-y-[3px] -ml-px w-0.5 rounded-full bg-foreground" style={{ left: `${bar.budget * 100}%` }} />
          )}
        </div>
      )}
      <dl className={cn(kvClass, "tabular-nums")}>
        <dt>
          {bar && <span className={key} style={{ background: "var(--axis)" }} />}
          In use
        </dt>
        <dd>
          {fmtGB(plan.memory_used_gb)} · {plan.loaded_models} of {plan.max_models} models
        </dd>
        {plan.outcome !== "loaded" && (
          <>
            <dt>
              {bar && <span className={key} style={{ background: color }} />}
              This model
            </dt>
            <dd>{plan.estimate_gb != null ? fmtGB(plan.estimate_gb) : "unknown"}</dd>
          </>
        )}
        <dt>
          {bar?.budget != null && bar.budget < 1 && <span className={cn(key, "w-0.5 rounded-none bg-foreground")} />}
          Budget
        </dt>
        <dd>{plan.max_memory_gb > 0 ? `${fmtGB(plan.max_memory_gb)} (MAX_MEMORY_GB)` : "none, only the model count"}</dd>
        {plan.free_memory_gb != null && (
          <>
            <dt>Free on device</dt>
            <dd>{fmtGB(plan.free_memory_gb)}</dd>
          </>
        )}
      </dl>
    </>
  );
}
