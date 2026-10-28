// A run in the Evals section: its items with their outputs and scores, a
// summary per metric (MetricChip, also on EvalsCompare.tsx), and the dialogs
// that score the outputs with metrics or start the LLM judge.

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ChevronRight, CircleStop, FlaskConical, Gavel, LoaderCircle, RotateCcw } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Progress } from "@/components/ui/progress";
import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/lib/utils";
import { useAction } from "../shared/action";
import type { ApiClient } from "../shared/api";
import { failed } from "../shared/errors";
import { errorText, poll } from "../shared/query";
import { ConfirmButton, EmptyCard, Field, SelectField, StatusLabel, SubmitButton } from "../shared/components";
import { fmtInt, fmtMs, statusOf } from "../shared/format";
import { DataGrid, type GridColumn } from "../shared/grid";
import type { Dataset, MetricsInfo, Run, RunDetail, RunItem } from "../shared/types";
import {
  RUN_STATUS,
  evalsHref,
  fmtMean,
  fmtScore,
  inputPreview,
  metricLabel,
  outputPreview,
  referencePreview,
  runProgress,
  scoreTone,
} from "./evals";
import { DetailSkeleton } from "./layout";
import { MODEL_IDS_LIST } from "./panels";
import { Pane } from "./TraceDialog";

export const TONE_CLASS = { good: "text-good-text", warning: "text-warning", critical: "text-critical-text" } as const;

export function RunView(props: { client: ApiClient; dataset: Dataset; runId: string; onChange(): Promise<unknown> }) {
  const { client, dataset, runId } = props;
  const [open, setOpen] = useState<string | null>(null);

  // Polled while the run is queued, running or being judged.
  const query = useQuery({
    queryKey: ["evals", "run", runId, dataset.id],
    queryFn: async ({ signal }) => {
      const [run, s] = await Promise.all([
        client.get("/admin/runs/{run_id}", { params: { path: { run_id: runId } }, signal }),
        client.get("/admin/runs", { params: { query: { dataset_id: dataset.id } }, signal }),
      ]);
      return { run, siblings: s.runs };
    },
    ...poll((d?: { run: RunDetail }) => (d && (d.run.status === "queued" || d.run.status === "running" || (d.run.activity && !d.run.activity.error)) ? 2000 : 0)),
  });
  const { data, refetch: reload } = query;
  const error = errorText(query.error);
  const run = data?.run ?? null;
  const siblings = data?.siblings ?? [];
  const { data: metrics } = client.useQuery("get", "/admin/evals/metrics");

  const cancel = () => client.post("/admin/runs/{run_id}/cancel", { params: { path: { run_id: runId } } }).then(() => reload(), failed("Could not cancel"));
  const resume = () => client.post("/admin/runs/{run_id}/resume", { params: { path: { run_id: runId } } }).then(() => reload(), failed("Could not resume"));
  const remove = async () => {
    try {
      await client.delete("/admin/runs/{run_id}", { params: { path: { run_id: runId } } });
      await props.onChange();
      location.hash = evalsHref(dataset.id);
    } catch (e) {
      failed("Could not delete the run")(e);
    }
  };

  if (error) return <EmptyCard icon={FlaskConical} title="Could not load the run" body={error} />;
  if (!run) return <DetailSkeleton />;
  const metricNames = Object.keys(run.summary).sort();
  const status = statusOf(RUN_STATUS, run.status);
  return (
    <Card className="gap-0 overflow-hidden py-0">
      <CardHeader className="border-b py-4">
        <CardTitle className="flex flex-wrap items-center gap-2">
          <a href={evalsHref(dataset.id)} className="text-muted-foreground hover:text-foreground">
            {dataset.name}
          </a>
          <ChevronRight className="size-4 text-muted-foreground" aria-hidden="true" />
          {run.name}
        </CardTitle>
        <CardDescription>
          <span className="flex flex-wrap items-center gap-x-3 gap-y-1">
            <span className="font-mono [overflow-wrap:anywhere]">{run.model_id}</span>
            {run.task && <span>{run.task}</span>}
            {Object.keys(run.params ?? {}).length > 0 && <span className="font-mono text-xs">{JSON.stringify(run.params)}</span>}
            <StatusLabel status={{ label: runProgress(run), color: status.color }} className="gap-1.5" />
            <span className="tabular-nums">
              {fmtInt(run.done)}/{fmtInt(run.total)} done{run.failed ? `, ${fmtInt(run.failed)} failed` : ""}
            </span>
            {run.started && run.finished && <span className="tabular-nums">took {fmtMs((run.finished - run.started) * 1000)}</span>}
          </span>
        </CardDescription>
        <CardAction className="flex flex-wrap gap-2">
          {(run.status === "queued" || run.status === "running") && (
            <Button type="button" variant="outline" size="sm" onClick={cancel}>
              <CircleStop aria-hidden="true" />
              Cancel
            </Button>
          )}
          {(run.status === "interrupted" || run.status === "failed" || run.status === "cancelled") && (
            <Button type="button" variant="outline" size="sm" onClick={resume} title="Goes on from where it stopped; the examples answered already are kept">
              <RotateCcw aria-hidden="true" />
              Resume
            </Button>
          )}
          {run.status === "done" && metrics && <ScoreDialog client={client} run={run} metrics={metrics} onDone={reload} />}
          {run.status === "done" && metrics && <JudgeDialog client={client} run={run} metrics={metrics} others={siblings.filter((r) => r.id !== run.id && r.status === "done")} onStarted={reload} />}
          <ConfirmButton
            iconOnly
            label="Delete run"
            action="Delete"
            title="Delete this run?"
            body="Its outputs and scores are removed. The dataset stays."
            cancel="Keep"
            onConfirm={remove}
          />
        </CardAction>
      </CardHeader>

      {run.activity && (run.activity.kind === "judging" || run.activity.kind === "running") && (
        <div className="flex items-center gap-3 border-b px-6 py-2 text-sm">
          {!run.activity.error && <LoaderCircle className="size-4 animate-spin text-muted-foreground" aria-hidden="true" />}
          <span className={cn(run.activity.error && "text-critical-text")}>{runProgress(run)}</span>
          {run.activity.total ? <Progress value={((run.activity.done ?? 0) / run.activity.total) * 100} className="h-1.5 max-w-xs flex-1" /> : null}
        </div>
      )}

      {metricNames.length > 0 && (
        <div className="flex flex-wrap gap-2 border-b px-6 py-3" aria-label="Summary">
          {metricNames.map((m) => (
            <MetricChip key={m} metric={m} summary={run.summary[m]} runs={siblings} />
          ))}
        </div>
      )}

      <div className="max-h-[70svh] overflow-y-auto">
        <DataGrid
          columns={itemColumns(metricNames, siblings)}
          rows={run.items}
          rowKey={(item) => item.id}
          rowProps={() => ({ className: "align-top" })}
          onRow={(item) => setOpen(open === item.id ? null : item.id)}
          detail={(item) => open === item.id && <ItemDetail item={item} />}
          empty={run.status === "queued" ? "Waiting for earlier runs to finish" : "No outputs yet"}
        />
      </div>
      <CardFooter className="border-t py-3 text-xs text-muted-foreground">
        Click a row for the full input, output and the judge's reasoning. Scores replace earlier ones of the same metric; an output the judge could not grade shows why, and judging again asks for it afresh.
      </CardFooter>
    </Card>
  );
}

export function MetricChip({ metric, summary, runs }: { metric: string; summary: RunDetail["summary"][string]; runs: Run[] }) {
  const tone = scoreTone(summary.mean);
  return (
    <div className="rounded-md border px-2.5 py-1.5 text-sm">
      <span className="text-xs text-muted-foreground">{metricLabel(metric, runs)}</span>
      <div className={cn("font-semibold tabular-nums", tone && TONE_CLASS[tone])}>
        {fmtMean(summary, metric)}
        <span className="ml-1.5 text-xs font-normal text-muted-foreground">
          {summary.scored}/{summary.count}
        </span>
      </div>
    </div>
  );
}

/** An item's score of ``metric``: the newest, when there are several. */
const scoreOf = (item: RunItem, metric: string) => item.scores.filter((s) => s.metric === metric).pop();

/** The items' columns: the fixed ones, then one per metric scored (named after the other run, for a pairwise one). */
function itemColumns(metrics: string[], runs: Run[]): GridColumn<RunItem>[] {
  return [
    { key: "n", label: "#", headClassName: "w-8", className: "text-muted-foreground tabular-nums", render: (_, i) => i + 1 },
    {
      key: "input",
      label: "Input",
      className: "min-w-44 max-w-xs whitespace-normal",
      render: (item) => <span className="line-clamp-3 [overflow-wrap:anywhere]">{inputPreview(item.input)}</span>,
    },
    {
      key: "output",
      label: "Output",
      className: "min-w-52 max-w-sm whitespace-normal",
      render: (item) =>
        item.status === "ok" ? (
          <span className="line-clamp-3 [overflow-wrap:anywhere]">{outputPreview(item.output)}</span>
        ) : (
          <span className="text-critical-text [overflow-wrap:anywhere]">{item.error ?? "failed"}</span>
        ),
    },
    {
      key: "reference",
      label: "Reference",
      className: "min-w-36 max-w-xs whitespace-normal text-muted-foreground",
      render: (item) => <span className="line-clamp-3 [overflow-wrap:anywhere]">{referencePreview(item.reference) || "–"}</span>,
    },
    {
      key: "time",
      label: "Time",
      align: "right",
      render: (item) => (
        <>
          {fmtMs(item.duration_ms)}
          {item.cold && <div className="text-xs text-muted-foreground">cold</div>}
        </>
      ),
    },
    ...metrics.map(
      (m): GridColumn<RunItem> => ({
        key: m,
        label: metricLabel(m, runs),
        align: "right",
        headClassName: "whitespace-nowrap",
        className: (item) => {
          const tone = scoreTone(scoreOf(item, m)?.value);
          return tone ? TONE_CLASS[tone] : undefined;
        },
        title: (item) => scoreOf(item, m)?.rationale ?? undefined,
        render: (item) => {
          const score = scoreOf(item, m);
          return fmtScore(score?.value, m, score?.label);
        },
      }),
    ),
  ];
}

/** Under an opened item: its full input, output, reference and baseline, and the judge's reasoning. */
function ItemDetail({ item }: { item: RunItem }) {
  const rationales = item.scores.filter((s) => s.rationale);
  return (
    <>
      <div className="grid gap-4 py-2 lg:grid-cols-2">
        <Pane title="Input" value={item.input} />
        <Pane title="Output" value={item.output} empty={item.error ?? "No output"} />
        {item.reference != null && <Pane title="Reference" value={item.reference} />}
        {item.source_output && <Pane title={`Baseline (${item.source_model_id ?? "the traced model"})`} value={item.source_output} />}
      </div>
      {rationales.length > 0 && (
        <dl className="grid gap-2 py-2 text-sm">
          {rationales.map((s) => (
            <div key={s.id} className="grid gap-0.5">
              <dt className="text-xs font-medium text-muted-foreground">
                {metricLabel(s.metric)} · {fmtScore(s.value, s.metric, s.label)}
                {s.judge ? ` · judged by ${s.judge}` : ""}
              </dt>
              <dd className="[overflow-wrap:anywhere]">{s.rationale}</dd>
            </div>
          ))}
        </dl>
      )}
    </>
  );
}

type MetricName = MetricsInfo["metrics"][number]["name"];

function ScoreDialog(props: { client: ApiClient; run: RunDetail; metrics: MetricsInfo; onDone(): Promise<unknown> }) {
  const [open, setOpen] = useState(false);
  const [chosen, setChosen] = useState<MetricName[]>(["exact_match", "contains"]);
  const toggle = (name: MetricName, on: boolean) => setChosen((c) => (on ? [...new Set([...c, name])] : c.filter((m) => m !== name)));
  const score = useAction(
    async () => {
      await props.client.post("/admin/runs/{run_id}/score", { params: { path: { run_id: props.run.id } }, body: { metrics: chosen } });
      await props.onDone();
      setOpen(false);
    },
    { failed: "Could not score" },
  );
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <Button type="button" variant="outline" size="sm" onClick={() => setOpen(true)}>
        <FlaskConical aria-hidden="true" />
        Score
      </Button>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Score with metrics</DialogTitle>
          <DialogDescription>These need no model. Metrics that compare with a reference score nothing on examples without one.</DialogDescription>
        </DialogHeader>
        <div className="grid gap-3">
          {props.metrics.metrics.map((m) => (
            <label key={m.name} className="flex items-start gap-3 text-sm">
              <Checkbox checked={chosen.includes(m.name)} onCheckedChange={(v) => toggle(m.name, v === true)} className="mt-0.5" />
              <span>
                <span className="font-medium">{metricLabel(m.name)}</span>
                <span className="block text-xs text-muted-foreground">{m.description}</span>
              </span>
            </label>
          ))}
        </div>
        <DialogFooter>
          <SubmitButton type="button" onClick={score.run} busy={score.busy} disabled={!chosen.length}>
            Score {fmtInt(props.run.items.length)} outputs
          </SubmitButton>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function JudgeDialog(props: { client: ApiClient; run: RunDetail; metrics: MetricsInfo; others: Run[]; onStarted(): Promise<unknown> }) {
  const [open, setOpen] = useState(false);
  const [mode, setMode] = useState<"rubric" | "pairwise">("rubric");
  const [against, setAgainst] = useState("");
  const [model, setModel] = useState(props.metrics.judge.model);
  const [where, setWhere] = useState<"local" | "remote">(props.metrics.judge.base_url ? "remote" : "local");
  const [baseUrl, setBaseUrl] = useState(props.metrics.judge.base_url);
  const [apiKey, setApiKey] = useState("");
  const [rubric, setRubric] = useState(props.metrics.judge.rubric);

  const start = useAction(
    async () => {
      await props.client.post("/admin/runs/{run_id}/judge", {
        params: { path: { run_id: props.run.id } },
        body: {
          against: mode === "pairwise" ? against : null,
          model: model.trim(),
          base_url: where === "remote" ? baseUrl.trim() : "",
          // Unset keeps JUDGE_API_KEY; a key typed here is used instead.
          api_key: where === "remote" && apiKey ? apiKey : null,
          rubric,
        },
      });
      await props.onStarted();
      setOpen(false);
      toast.success("Judging started", { description: "Scores appear when it finishes." });
    },
    { failed: "Could not start the judge" },
  );

  const ready = model.trim() && (mode === "rubric" || against) && (where === "local" || baseUrl.trim());
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <Button type="button" variant="outline" size="sm" onClick={() => setOpen(true)}>
        <Gavel aria-hidden="true" />
        Judge
      </Button>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Judge with a model</DialogTitle>
          <DialogDescription>
            A chat model scores each output against the rubric, or says which of two runs answered better (asked in both orders; disagreement is a tie).
          </DialogDescription>
        </DialogHeader>
        <div className="grid gap-4">
          <SelectField
            label="What to judge"
            value={mode}
            onChange={(v) => setMode(v as "rubric" | "pairwise")}
            options={[
              { value: "rubric", label: "Score each output against the rubric" },
              { value: "pairwise", label: "Compare with another run", disabled: !props.others.length },
            ]}
          />
          {mode === "pairwise" && (
            <SelectField label="Against" value={against} onChange={setAgainst} none="Pick a run…" options={props.others.map((r) => ({ value: r.id, label: r.name }))} />
          )}
          <SelectField
            label="Judge runs"
            value={where}
            onChange={(v) => setWhere(v as "local" | "remote")}
            options={[
              { value: "local", label: "On this service (a model it can load, through its own /v1)" },
              { value: "remote", label: "Behind an OpenAI-compatible endpoint" },
            ]}
          />
          <Field label="Judge model">
            <Input value={model} onChange={(e) => setModel(e.target.value)} list={where === "local" ? MODEL_IDS_LIST : undefined} placeholder={where === "local" ? "org/model" : "the endpoint's model name"} className="font-mono text-sm" />
          </Field>
          {where === "remote" && (
            <div className="grid gap-3 sm:grid-cols-[1fr_auto]">
              <Field label="Base URL">
                <Input value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} placeholder="https://host/v1" className="font-mono text-sm" />
              </Field>
              <Field label="API key" hint={props.metrics.judge.has_api_key ? "Unset: JUDGE_API_KEY" : undefined}>
                <Input type="password" value={apiKey} onChange={(e) => setApiKey(e.target.value)} autoComplete="off" className="w-40" />
              </Field>
            </div>
          )}
          <Field label="Rubric">
            <Textarea value={rubric} onChange={(e) => setRubric(e.target.value)} rows={3} />
          </Field>
        </div>
        <DialogFooter>
          <SubmitButton type="button" onClick={start.run} busy={start.busy} disabled={!ready}>
            Start judging
          </SubmitButton>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
