// A predictor's page in the AutoGluon section: its training job while it
// runs (progress, log, Cancel), then how good it is (the score, the
// leaderboard, the columns' importance and the classification report, or a
// forecast per series), trying it on one row, predicting a whole file (the
// autogluon-predict job), and how to call it from elsewhere.

import { useMemo, useState } from "react";
import { Download, Play, RotateCcw, ScrollText, X } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Progress } from "@/components/ui/progress";
import { useAction } from "../../shared/action";
import { useApi } from "../../shared/api";
import { downloadResponse } from "../../shared/browser";
import { CodeBlock, ConfirmButton, CopyButton, Disclosure, Facts, Notice, SelectField, SubmitButton, StatusBadge } from "../../shared/components";
import { fmtAgo, fmtInt, fmtSeconds } from "../../shared/format";
import { DataGrid, type GridColumn } from "../../shared/grid";
import { AUTOGLUON_PREFIX } from "../../shared/model-browser/catalog";
import { errorText, poll, useReloadOn } from "../../shared/query";
import { TryPanel } from "../../shared/task-io/TryPanel";
import type { Job } from "../../shared/types";
import { ClassificationReportView } from "../evals/EvalsReport";
import { DetailSkeleton } from "../layout";
import {
  autogluonHref,
  baseName,
  curlFor,
  fmtScore,
  headlineScore,
  KIND_LABEL,
  newHref,
  PREDICT_JOB,
  predictorOf,
  scoreText,
  type Predictor,
  type Report,
  type TrainResult,
} from "./autogluon";
import { ForecastChart, ImportanceChart, LeaderboardChart } from "./AutoGluonCharts";
import { DataPicker } from "./AutoGluonData";
import { isActive, progressValue } from "../../shared/tasks";

const LIVE_MS = 2000;

export function PredictorPage({ id }: { id: string }) {
  const client = useApi();
  const job = client.useQuery("get", "/jobs/{id}", { params: { path: { id } } }, poll((d?: Job) => (d && isActive(d) ? LIVE_MS : 0)));
  if (job.error) return <Notice notice={{ text: errorText(job.error), kind: "error" }} />;
  if (!job.data) return <DetailSkeleton />;
  const predictor = predictorOf(job.data);
  if (!predictor) return <Notice notice={{ text: `Job ${id} is a ${job.data.job} job, not an AutoGluon predictor's training.`, kind: "error" }} />;
  return (
    <div className="grid min-w-0 gap-5">
      <HeaderCard predictor={predictor} onChange={job.refetch} />
      {predictor.status !== "succeeded" ? <TrainingCard job={job.data} /> : predictor.result && <Trained predictor={predictor} result={predictor.result} />}
    </div>
  );
}

function HeaderCard({ predictor, onChange }: { predictor: Predictor; onChange(): unknown }) {
  const client = useApi();
  const { job } = predictor;
  const cancel = useAction(async () => {
    await client.post("/jobs/{id}/cancel", { params: { path: { id: job.id } } });
    await onChange();
  });
  const remove = useAction(
    async () => {
      await client.delete("/jobs/{id}", { params: { path: { id: job.id } } });
      location.hash = autogluonHref();
    },
    { success: `Deleted ${predictor.name}.` },
  );
  const p = job.params;
  return (
    <Card className="gap-3">
      <CardHeader>
        <CardTitle className="flex flex-wrap items-center gap-2">
          <a href={autogluonHref()} className="text-muted-foreground hover:underline">
            Predictors
          </a>
          <span className="text-muted-foreground">/</span>
          {predictor.name}
          <Badge variant="outline">{KIND_LABEL[predictor.kind]}</Badge>
          <StatusBadge status={job.status} />
        </CardTitle>
        <CardDescription>
          Predicts <strong>{predictor.predicts}</strong> from <code>{baseName(String(p.train_file ?? ""))}</code> · submitted {fmtAgo(job.created)}
        </CardDescription>
        <CardAction className="flex flex-wrap items-center gap-2">
          {isActive(job) ? (
            <SubmitButton type="button" variant="outline" size="sm" className="text-destructive hover:text-destructive" busy={cancel.busy} icon={<X aria-hidden="true" />} onClick={cancel.run}>
              Cancel
            </SubmitButton>
          ) : (
            <>
              <Button type="button" variant="outline" size="sm" asChild>
                <a href={newHref(job.id)}>
                  <RotateCcw aria-hidden="true" />
                  Retrain
                </a>
              </Button>
              <ConfirmButton
                title={`Delete ${predictor.name}?`}
                body="The predictor, its job and its files are removed, and it is unloaded. Clients and workflows that call its model ID get an error. This cannot be undone."
                busy={remove.busy}
                onConfirm={remove.run}
              />
            </>
          )}
        </CardAction>
      </CardHeader>
    </Card>
  );
}

/** A job's log, polled while it runs. */
function useLog(job: Job) {
  const client = useApi();
  const live = isActive(job);
  const log = client.useQuery("get", "/jobs/{id}/log", { params: { path: { id: job.id }, query: { tail: 20000 } }, parseAs: "text" }, poll(live ? LIVE_MS : 0));
  useReloadOn(live, log.refetch);
  return log.data ?? "";
}

function TrainingCard({ job }: { job: Job }) {
  const log = useLog(job);
  const value = progressValue(job);
  const live = isActive(job);
  return (
    <Card className="gap-4">
      <CardHeader>
        <CardTitle>{live ? "Training" : job.status === "failed" ? "Training failed" : "Training was cancelled"}</CardTitle>
        <CardDescription>
          {job.status === "queued" ? "Waiting for its turn: another job is running." : (job.message ?? "")}
          {job.started && live && ` · ${fmtSeconds(Date.now() / 1000 - job.started)} so far`}
        </CardDescription>
      </CardHeader>
      <CardContent className="grid gap-3">
        {live &&
          (value === null ? (
            <div className="h-2 animate-pulse rounded-full bg-primary/40" role="progressbar" aria-label="Running" />
          ) : (
            <Progress value={value * 100} aria-label={`${Math.round(value * 100)}% done`} />
          ))}
        {job.error && <p className="text-sm text-critical-text [overflow-wrap:anywhere]">{job.error}</p>}
        <Disclosure label="Log" defaultOpen={live}>
          <CodeBlock wrap className="max-h-80 text-xs">
            {log || <span className="text-muted-foreground">Nothing printed yet.</span>}
          </CodeBlock>
        </Disclosure>
      </CardContent>
    </Card>
  );
}

function Trained({ predictor, result }: { predictor: Predictor; result: TrainResult }) {
  const client = useApi();
  const report = client.useQuery("get", "/jobs/{id}/files/{path}", { params: { path: { id: predictor.id, path: "report.json" } } }, { retry: false, staleTime: Infinity });
  const read = (report.data ?? null) as Report | null;
  return (
    <>
      <ResultsCard predictor={predictor} result={result} report={read} />
      {predictor.kind === "tabular" && read?.importance && read.importance.length > 0 && (
        <Card className="gap-3">
          <CardHeader>
            <CardTitle>What it looks at</CardTitle>
          </CardHeader>
          <CardContent>
            <ImportanceChart rows={read.importance} />
          </CardContent>
        </Card>
      )}
      {predictor.kind === "tabular" && read?.classification && (
        <Card className="gap-0 py-0">
          <CardHeader className="px-6 pt-5">
            <CardTitle>How it classifies the held-out rows</CardTitle>
          </CardHeader>
          <ClassificationReportView report={read.classification} />
        </Card>
      )}
      {predictor.kind === "timeseries" && <ForecastCard predictor={predictor} report={read} />}
      {predictor.kind === "tabular" && <TryCard predictor={predictor} />}
      <PredictFileCard predictor={predictor} />
      <ElsewhereCard predictor={predictor} report={read} />
    </>
  );
}

const BOARD_COLUMNS = (lower: boolean): GridColumn<Record<string, unknown>>[] => {
  const score = (v: unknown) => (typeof v === "number" ? fmtScore(lower ? Math.abs(v) : v) : "–");
  return [
    { key: "model", label: "Model", className: "font-mono text-[13px]", render: (r) => String(r.model) },
    { key: "score_test", label: "Test score", align: "right", render: (r) => score(r.score_test) },
    { key: "score_val", label: "Validation score", align: "right", render: (r) => score(r.score_val) },
    { key: "pred_time_val", label: "Prediction time", align: "right", render: (r) => (typeof r.pred_time_val === "number" ? `${fmtScore(r.pred_time_val)} s` : "–") },
    { key: "fit_time", label: "Fit time", align: "right", render: (r) => (typeof r.fit_time === "number" ? fmtSeconds(r.fit_time) : "–") },
  ];
};

function ResultsCard({ predictor, result, report }: { predictor: Predictor; result: TrainResult; report: Report | null }) {
  const lower = Boolean(result.lower_is_better);
  const head = headlineScore(result);
  const shown = scoreText(head.value, result.lower_is_better);
  const series = predictor.kind === "timeseries";
  const on =
    head.on === "test"
      ? series
        ? "on the test file's last steps, which it never saw"
        : `on ${fmtInt(result.test_rows ?? 0)} rows held out from training`
      : series
        ? "Validation score: on the last steps of each series, held out from training; the best model was chosen on it"
        : "Validation score: on rows AutoGluon held out to choose the models";
  const columns = BOARD_COLUMNS(lower).filter((c) => c.key !== "score_test" || result.leaderboard.some((r) => typeof r.score_test === "number"));
  return (
    <Card className="gap-4">
      <CardHeader>
        <CardTitle>How good it is</CardTitle>
      </CardHeader>
      <CardContent className="grid gap-4">
        <div className="flex flex-wrap items-end gap-x-8 gap-y-3">
          <div>
            <div className="text-xs text-muted-foreground">{result.eval_metric.replace(/_/g, " ")}</div>
            <div className="text-3xl font-semibold tabular-nums">{shown.text}</div>
            <div className="text-xs text-muted-foreground">{shown.hint}</div>
          </div>
          <p className="max-w-md text-sm text-muted-foreground">{on}.</p>
        </div>
        <Facts
          rows={[
            ["Best model", <code>{result.best_model}</code>],
            ...(head.on === "test" && result.validation_score != null ? [["Validation score", scoreText(result.validation_score, result.lower_is_better).text] as [string, string]] : []),
            ["Trained on", `${fmtInt(result.train_rows)} rows${series && result.series ? `, ${fmtInt(result.series)} series` : ""}`],
            ["Training took", fmtSeconds(result.fit_seconds)],
            ...(result.problem_type ? [["Problem", result.problem_type] as [string, string]] : []),
            ...(result.classes?.length ? [["Classes", result.classes.map(String).join(", ")] as [string, string]] : []),
          ]}
        />
        {result.leaderboard.length > 0 && (
          <>
            <LeaderboardChart rows={result.leaderboard} best={result.best_model} lowerIsBetter={lower} />
            <Disclosure label={`Leaderboard: ${result.leaderboard.length} models`}>
              <div className="overflow-x-auto rounded-lg border">
                <DataGrid columns={columns} rows={result.leaderboard} rowKey={(r) => String(r.model)} headerClassName="bg-muted/50" empty="No models" />
              </div>
            </Disclosure>
          </>
        )}
        {report?.notes?.map((note) => (
          <p key={note} className="text-xs text-muted-foreground">
            Not in the report: {note}
          </p>
        ))}
      </CardContent>
    </Card>
  );
}

function ForecastCard({ predictor, report }: { predictor: Predictor; report: Report | null }) {
  const client = useApi();
  const [index, setIndex] = useState("0");
  const series = report?.series ?? [];
  const chosen = series[Number(index)] ?? series[0];
  const download = useAction(
    async () => downloadResponse(await client.raw("get", "/jobs/{id}/files/{path}", { params: { path: { id: predictor.id, path: "forecast.csv" } } }), `${predictor.id}-forecast.csv`),
    { failed: "Could not download the forecast" },
  );
  return (
    <Card className="gap-4">
      <CardHeader>
        <CardTitle>What comes next</CardTitle>
        <CardDescription>
          {report?.forecast_skipped
            ? `${report.forecast_skipped}: forecast from newer data below, with them.`
            : report?.series
              ? `The next ${report.prediction_length} steps of each series, from the end of its training data.`
              : "This predictor's training wrote no forecast. Forecast from newer data below."}
        </CardDescription>
        {report?.forecast_file && (
          <CardAction>
            <SubmitButton type="button" variant="outline" size="sm" busy={download.busy} icon={<Download aria-hidden="true" />} onClick={download.run}>
              forecast.csv
            </SubmitButton>
          </CardAction>
        )}
      </CardHeader>
      {chosen && report?.band && (
        <CardContent className="grid gap-3">
          {series.length > 1 && (
            <SelectField
              label="Series"
              className="max-w-xs"
              value={index}
              onChange={setIndex}
              options={series.map((s, i) => ({ value: String(i), label: s.id ?? `series ${i + 1}` }))}
              hint={report.series_count && report.series_count > series.length ? `the first ${series.length} of ${fmtInt(report.series_count)}; forecast.csv has them all` : undefined}
            />
          )}
          <ForecastChart history={chosen.history} forecast={chosen.forecast} band={report.band} target={report.target ?? "value"} />
        </CardContent>
      )}
    </Card>
  );
}

function TryCard({ predictor }: { predictor: Predictor }) {
  const [ran, setRan] = useState(false);
  return (
    <Card className="gap-4">
      <CardHeader>
        <CardTitle>Predict one row</CardTitle>
        <CardDescription>Through POST /inference, as a client would: the first request starts the predictor, which then stays loaded while used.</CardDescription>
      </CardHeader>
      <CardContent>
        <TryPanel modelId={AUTOGLUON_PREFIX + predictor.id} task="tabular-prediction" loaded={ran} onRan={() => setRan(true)} />
      </CardContent>
    </Card>
  );
}

const PREDICT_LIMIT = 50;

function PredictFileCard({ predictor }: { predictor: Predictor }) {
  const client = useApi();
  const [file, setFile] = useState("");
  const [future, setFuture] = useState("");
  const series = predictor.kind === "timeseries";
  const known = Array.isArray(predictor.job.params.known_covariates) && predictor.job.params.known_covariates.length > 0;
  // This predictor's own (its job ID is their ref), the newest first.
  const jobs = client.useQuery("get", "/jobs", { params: { query: { job: [PREDICT_JOB], ref: predictor.id, limit: PREDICT_LIMIT } } }, {
    select: (d) => d.jobs,
    ...poll((d?: { jobs: Job[] }) => (d?.jobs.some(isActive) ? LIVE_MS : 0)),
  });
  const run = useAction(
    async () => {
      if (!file) throw new Error("Choose a file");
      if (series && known && !future) throw new Error("This forecaster has known covariates: choose the file with their future values");
      await client.post("/jobs", {
        body: { job: PREDICT_JOB, priority: "high", params: { predictor: predictor.id, data_file: file, ...(future ? { known_covariates_file: future } : {}) } },
      });
      await jobs.refetch();
    },
    { notice: true },
  );
  const download = useAction(
    async (j: Job) => {
      const out = (j.result as { file?: string } | null)?.file ?? "predictions.csv";
      downloadResponse(await client.raw("get", "/jobs/{id}/files/{path}", { params: { path: { id: j.id, path: out } } }), `${predictor.id}-${out}`);
    },
    { failed: "Could not download the file" },
  );
  const columns: GridColumn<Job>[] = [
    { key: "file", label: "File", className: "font-mono text-[13px] [overflow-wrap:anywhere]", render: (j) => baseName(String(j.params.data_file ?? "")) },
    { key: "status", label: "State", render: (j) => (j.status === "queued" ? <span className="text-sm text-muted-foreground">waiting behind a running job</span> : <StatusBadge status={j.status} />) },
    { key: "rows", label: "Rows", align: "right", render: (j) => fmtInt((j.result as { rows?: number } | null)?.rows) },
    { key: "when", label: "When", className: "whitespace-nowrap text-muted-foreground", render: (j) => fmtAgo(j.finished ?? j.created) },
    {
      key: "get",
      headClassName: "w-0",
      render: (j) =>
        j.status === "succeeded" ? (
          <SubmitButton type="button" variant="outline" size="sm" busy={download.running?.[0].id === j.id} icon={<Download aria-hidden="true" />} onClick={() => void download.run(j)}>
            Download
          </SubmitButton>
        ) : j.error ? (
          <span className="block max-w-56 truncate text-xs text-critical-text" title={j.error}>
            {j.error}
          </span>
        ) : null,
    },
  ];
  return (
    <Card className="gap-4">
      <CardHeader>
        <CardTitle>{series ? "Forecast from newer data" : "Predict a file"}</CardTitle>
        <CardDescription>
          {series
            ? "A file with the series' history up to now (the same columns as the training data) gives the next steps of each, as forecast.csv."
            : "The file's rows come back with a prediction column, and a probability column per class, as predictions.csv."}{" "}
          It runs as a job at high priority, after any job already running.
        </CardDescription>
      </CardHeader>
      <CardContent className="grid gap-3">
        <DataPicker label={series ? "History" : "Rows to predict"} value={file} onChange={setFile} />
        {series && known && <DataPicker label="Future values of the known covariates" value={future} onChange={setFuture} optional />}
        <div className="flex flex-wrap items-center gap-3">
          <SubmitButton type="button" busy={run.busy} disabled={!file} icon={<Play aria-hidden="true" />} onClick={run.run}>
            {series ? "Forecast" : "Predict"}
          </SubmitButton>
          <Notice notice={run.notice} />
        </div>
        {jobs.data && jobs.data.length > 0 && (
          <div className="overflow-x-auto rounded-lg border">
            <DataGrid columns={columns} rows={jobs.data} rowKey={(j) => j.id} headerClassName="bg-muted/50" empty="None yet" />
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function ElsewhereCard({ predictor, report }: { predictor: Predictor; report: Report | null }) {
  const modelId = AUTOGLUON_PREFIX + predictor.id;
  const p = predictor.job.params;
  const curl = useMemo(() => {
    if (predictor.kind === "tabular") {
      return curlFor(location.origin, modelId, "tabular-prediction", { rows: [report?.sample ?? { column: "value" }], probabilities: true });
    }
    const first = report?.series?.[0];
    const ts = String(p.timestamp_column ?? "timestamp");
    const target = String(p.target ?? "target");
    const rows = (first?.history ?? [{ t: "2026-01-01", y: 1 }]).map((h) => ({
      ...(p.id_column && first?.id != null ? { [String(p.id_column)]: first.id } : {}),
      [ts]: h.t.replace("T00:00:00", ""),
      [target]: h.y,
    }));
    return curlFor(location.origin, modelId, "timeseries-forecast", { rows });
  }, [predictor, report, modelId, p]);
  return (
    <Card className="gap-4">
      <CardHeader>
        <CardTitle>Use it elsewhere</CardTitle>
        <CardDescription>
          Its model ID works wherever a model is chosen: an eval run, a workflow's task node, a client of POST /inference. Training again gives a new ID.
        </CardDescription>
      </CardHeader>
      <CardContent className="grid gap-3">
        <div className="flex flex-wrap items-center gap-2">
          <code className="rounded-md bg-muted px-2 py-1 text-sm">{modelId}</code>
          <CopyButton value={modelId} />
          <Button type="button" variant="outline" asChild>
            <a href="#/jobs">
              <ScrollText aria-hidden="true" />
              Its job
            </a>
          </Button>
        </div>
        <Disclosure label="curl">
          <div className="grid gap-2">
            <CodeBlock wrap className="text-xs">
              {curl}
            </CodeBlock>
            <CopyButton value={curl} />
          </div>
        </Disclosure>
      </CardContent>
    </Card>
  );
}
