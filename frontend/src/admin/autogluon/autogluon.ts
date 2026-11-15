// What the AutoGluon section (AutoGluonSection.tsx and its parts) needs that
// is not drawing: the jobs it runs, a predictor read from its training job,
// a file's name as an upload takes it, what a column to predict means, which
// columns look like IDs or dates, a score as the page shows it, and each
// kind's job params from the form.

import type { Schemas } from "../../shared/api";
import type { Job } from "../../shared/types";

export type Kind = "tabular" | "timeseries";
export type FileSummary = Schemas["FileSummary"];
export type ColumnSummary = Schemas["ColumnSummary"];

/** The environment AutoGluon runs in, and the job that installs it. */
export const ENV = "autogluon";
export const SETUP_JOB = "setup-env";
/** Each kind's training job, and the job that predicts a file with either. */
export const TRAIN_JOB: Record<Kind, string> = { tabular: "autogluon-tabular", timeseries: "autogluon-timeseries" };
export const PREDICT_JOB = "autogluon-predict";
export const KIND_OF_JOB: Record<string, Kind> = { "autogluon-tabular": "tabular", "autogluon-timeseries": "timeseries" };
export const KIND_LABEL: Record<Kind, string> = { tabular: "Tabular", timeseries: "Time series" };

/** The section's pages: the list, a new predictor (retraining one, maybe), and a predictor (its training job). */
export type AutoGluonLoc = { page: "list" } | { page: "new"; from: string | null } | { page: "predictor"; id: string };

export function autogluonFromHash(hash: string): AutoGluonLoc {
  const [, part] = hash.replace(/^#\/?/, "").split("?")[0].split("/");
  if (!part) return { page: "list" };
  if (part === "new") return { page: "new", from: new URLSearchParams(hash.split("?")[1] ?? "").get("from") };
  return { page: "predictor", id: part };
}

export const autogluonHref = (id?: string) => (id ? `#/autogluon/${id}` : "#/autogluon");
export const newHref = (from?: string) => `#/autogluon/new${from ? `?from=${from}` : ""}`;

// ------------------------------------------------------------- qualities
export type Quality = "fast" | "good" | "best";
export const QUALITIES: [Quality, string][] = [
  ["fast", "Fast"],
  ["good", "Good"],
  ["best", "Best"],
];
/** AutoGluon's presets behind each quality, by kind. */
export const PRESETS: Record<Kind, Record<Quality, string>> = {
  tabular: { fast: "medium_quality", good: "good_quality", best: "best_quality" },
  timeseries: { fast: "medium_quality", good: "high_quality", best: "best_quality" },
};
export const qualityOf = (kind: Kind, presets: unknown): Quality =>
  (Object.entries(PRESETS[kind]).find(([, p]) => p === presets)?.[0] as Quality | undefined) ?? "fast";

/** How long training may take, in minutes. */
export const TIME_LIMITS: [string, string][] = [
  ["60", "1 minute"],
  ["300", "5 minutes"],
  ["900", "15 minutes"],
  ["3600", "1 hour"],
];

// ---------------------------------------------------------------- results
/** What a training job's result holds (jobs/autogluon/), as far as the pages read it. */
export interface TrainResult {
  model_id: string;
  task: string;
  eval_metric: string;
  best_model: string;
  validation_score: number | null;
  test_score: number | null;
  /** Missing on a predictor trained before it was added: then read as false */
  lower_is_better?: boolean;
  leaderboard: Record<string, unknown>[];
  train_rows: number;
  fit_seconds: number;
  problem_type?: string;
  features?: string[];
  classes?: unknown[] | null;
  test_rows?: number;
  prediction_length?: number;
  freq?: string | null;
  series?: number;
}

export interface ClassificationReport {
  count: number;
  accuracy: number;
  macro_f1: number;
  labels: { label: string; precision: number; recall: number; f1: number; support: number }[];
  confusion: number[][] | null;
}

export interface SeriesPoint {
  t: string;
  y?: number | null;
  mean?: number | null;
  low?: number | null;
  high?: number | null;
}

/** report.json in a training job's folder: what the pages draw. A predictor trained before it was added has none. */
export interface Report {
  kind: Kind;
  notes?: string[];
  // tabular
  label?: string;
  columns?: Record<string, string>;
  sample?: Record<string, unknown> | null;
  importance?: { feature: string; importance: number | null; stddev: number | null; p_value: number | null }[];
  classification?: ClassificationReport;
  // time series
  target?: string;
  prediction_length?: number;
  forecast_file?: string;
  forecast_skipped?: string;
  series?: { id: string | null; history: SeriesPoint[]; forecast: SeriesPoint[] }[];
  series_count?: number;
  band?: [number, number];
}

/** A predictor as the list shows it, read from its training job. */
export interface Predictor {
  id: string;
  kind: Kind;
  name: string;
  /** What it predicts: a column, or a value so many steps ahead */
  predicts: string;
  status: Job["status"];
  job: Job;
  result: TrainResult | null;
}

const text = (v: unknown): string => (typeof v === "string" ? v : "");
/** The last part of a path, without its folder. */
export const baseName = (path: string): string => path.split("/").pop() ?? path;

export function predictorOf(job: Job): Predictor | null {
  const kind = KIND_OF_JOB[job.job ?? ""];
  if (!kind) return null;
  const p = job.params;
  const column = kind === "tabular" ? text(p.label) : text(p.target);
  const result = job.status === "succeeded" && job.result && typeof job.result === "object" ? (job.result as TrainResult) : null;
  return {
    id: job.id,
    kind,
    name: text(p.name) || defaultName(baseName(text(p.train_file)), column),
    predicts: kind === "tabular" ? column : `${column}, ${p.prediction_length} steps ahead`,
    status: job.status,
    job,
    result,
  };
}

/** A predictor's name when none was given: its file and column. */
export const defaultName = (file: string, column: string): string => [file.replace(/\.[^.]+$/, ""), column].filter(Boolean).join(" · ");

/** The score the page leads with: on rows held out from training when there are, else AutoGluon's validation score. */
export function headlineScore(result: TrainResult): { value: number | null; on: "test" | "validation" } {
  return result.test_score != null ? { value: result.test_score, on: "test" } : { value: result.validation_score, on: "validation" };
}

/** A number as a score is shown: three significant digits, more for a large one. */
export function fmtScore(value: number): string {
  const abs = Math.abs(value);
  if (abs === 0) return "0";
  if (abs >= 100) return value.toFixed(1);
  if (abs >= 1) return value.toFixed(3);
  return value.toPrecision(3);
}

/** A score as shown: AutoGluon negates an error (RMSE, log loss, WQL) so higher is better; shown positive, "lower is better". */
export function scoreText(value: number | null | undefined, lowerIsBetter: boolean | undefined): { text: string; hint: string } {
  if (value == null) return { text: "not scored", hint: "" };
  return lowerIsBetter ? { text: fmtScore(Math.abs(value)), hint: "lower is better" } : { text: fmtScore(value), hint: "higher is better" };
}

// ------------------------------------------------------------------ files
/** The formats the jobs read. */
export const DATA_FILES = ".csv,.tsv,.parquet,.pq,.jsonl,.ndjson,.json";

/**
 * A file's name as POST /jobs/data takes it (letters, digits, ".", "_" and "-", not leading with a dot):
 * "Churn data (1).csv" becomes "Churn_data_1.csv".
 */
export function cleanFileName(name: string): string {
  const dot = name.lastIndexOf(".");
  const [stem, ext] = dot > 0 ? [name.slice(0, dot), name.slice(dot + 1)] : [name, ""];
  const clean = (s: string) =>
    s
      .normalize("NFKD")
      .replace(/[̀-ͯ]/g, "")
      .replace(/[^A-Za-z0-9._-]+/g, "_")
      .replace(/^[._-]+|[._-]+$/g, "");
  const suffix = clean(ext).toLowerCase();
  return `${(clean(stem) || "data").slice(0, 180)}${suffix ? `.${suffix}` : ""}`;
}

// --------------------------------------------------------------- columns
const NUMBER = /^(TINYINT|SMALLINT|INTEGER|BIGINT|HUGEINT|UTINYINT|USMALLINT|UINTEGER|UBIGINT|FLOAT|DOUBLE|REAL|DECIMAL)/;
const WHOLE = /^(TINYINT|SMALLINT|INTEGER|BIGINT|HUGEINT|UTINYINT|USMALLINT|UINTEGER|UBIGINT)/;
const DATE = /^(DATE|TIMESTAMP)/;

export const isNumber = (c: Pick<ColumnSummary, "type">) => NUMBER.test(c.type);
export const isDate = (c: Pick<ColumnSummary, "type">) => DATE.test(c.type);

/** A whole-number column with at most this many values is taken as classes, not a quantity. */
const MAX_CLASSES = 20;

/** What predicting ``column`` will be, in words: AutoGluon decides in the end. */
export function problemGuess(column: ColumnSummary): { problem: "binary" | "multiclass" | "regression"; text: string } {
  const distinct = column.distinct ?? 0;
  if (distinct <= 2) return { problem: "binary", text: "Two values: yes/no classification" };
  if (isNumber(column) && !(WHOLE.test(column.type) && distinct <= MAX_CLASSES)) {
    return { problem: "regression", text: "Many numbers: regression, predicting a quantity" };
  }
  return { problem: "multiclass", text: `About ${distinct} values: classification into ${distinct} classes` };
}

/** DuckDB's distinct count is approximate, so a column counts as one value per row from this share on. */
const ID_SHARE = 0.95;
/** Fewer rows than this, and every column would look like an ID. */
const MIN_ROWS_FOR_IDS = 20;

/** Text or whole-number columns with (about) a value per row: likely IDs, which a predictor should not learn from. */
export function idLikeColumns(summary: Pick<FileSummary, "rows" | "columns">, except: string[] = []): string[] {
  if (summary.rows < MIN_ROWS_FOR_IDS) return [];
  return summary.columns
    .filter((c) => !except.includes(c.name) && (c.type === "VARCHAR" || WHOLE.test(c.type)) && (c.distinct ?? 0) >= ID_SHARE * summary.rows)
    .map((c) => c.name);
}

/** The columns a timestamp may be in: those typed as dates first. */
export function timestampCandidates(columns: ColumnSummary[]): ColumnSummary[] {
  return [...columns.filter(isDate), ...columns.filter((c) => !isDate(c))];
}

/** Under this share of distinct timestamps per row, the file holds several series (allowing for the approximate count). */
const ONE_SERIES_SHARE = 0.9;

/** Whether the file looks like several series: its timestamps repeat. */
export const severalSeries = (rows: number, timestamp: ColumnSummary | undefined): boolean =>
  Boolean(timestamp && rows > 1 && (timestamp.distinct ?? rows) < ONE_SERIES_SHARE * rows);

// ------------------------------------------------------------ job params
export interface Common {
  file: string;
  name: string;
  /** Seconds, as a TIME_LIMITS value */
  timeLimit: string;
  quality: Quality;
  /** The params under Advanced, from the job's schema */
  advanced: Record<string, unknown>;
}

export interface TabularForm extends Common {
  label: string;
  ignored: string[];
}

export interface SeriesForm extends Common {
  timestamp: string;
  target: string;
  /** "" for one series */
  id: string;
  steps: string;
  freq: string;
  known: string[];
}

/** The params the page sets itself; Advanced shows the rest of a job's. */
export const SET_HERE: Record<Kind, string[]> = {
  tabular: ["train_file", "label", "name", "time_limit", "presets", "ignored_columns"],
  timeseries: ["train_file", "name", "time_limit", "presets", "timestamp_column", "target", "id_column", "prediction_length", "freq", "known_covariates"],
};

/** A job's params schema without what the page sets itself: the form under Advanced. */
export function advancedSchema<S extends { properties?: Record<string, unknown>; required?: string[] }>(schema: S, kind: Kind): S {
  const set = SET_HERE[kind];
  const properties = Object.fromEntries(Object.entries(schema.properties ?? {}).filter(([k]) => !set.includes(k)));
  return { ...schema, properties, required: (schema.required ?? []).filter((k) => !set.includes(k)) };
}

const named = (name: string) => (name.trim() ? { name: name.trim() } : {});

export function tabularParams(f: TabularForm): Record<string, unknown> {
  return {
    ...f.advanced,
    train_file: f.file,
    label: f.label,
    ...named(f.name),
    time_limit: Number(f.timeLimit),
    presets: PRESETS.tabular[f.quality],
    ignored_columns: f.ignored.filter((c) => c !== f.label),
  };
}

export function seriesParams(f: SeriesForm): Record<string, unknown> {
  return {
    ...f.advanced,
    train_file: f.file,
    timestamp_column: f.timestamp,
    target: f.target,
    ...(f.id ? { id_column: f.id } : {}),
    prediction_length: Number(f.steps),
    ...named(f.name),
    time_limit: Number(f.timeLimit),
    presets: PRESETS.timeseries[f.quality],
    ...(f.freq.trim() ? { freq: f.freq.trim() } : {}),
    known_covariates: f.known.filter((c) => c !== f.target && c !== f.timestamp && c !== f.id),
  };
}

/** Why the form cannot train yet, or "". */
export function formProblem(kind: Kind, f: TabularForm | SeriesForm): string {
  if (!f.file) return "Choose a data file.";
  if (kind === "tabular") return (f as TabularForm).label ? "" : "Pick the column to predict.";
  const s = f as SeriesForm;
  if (!s.timestamp) return "Pick the timestamp column.";
  if (!s.target) return "Pick the value to forecast.";
  if (s.target === s.timestamp || s.id === s.target || s.id === s.timestamp) return "The timestamp, value and series ID are three different columns.";
  const steps = Number(s.steps);
  if (!Number.isInteger(steps) || steps < 1) return "Steps ahead is a whole number, 1 or more.";
  return "";
}

// ------------------------------------------------------------ predicting
/** The curl that asks a predictor for one row (the tabular sample, or a series' history). */
export function curlFor(origin: string, modelId: string, task: string, input: unknown): string {
  const body = JSON.stringify({ model_id: modelId, task, input });
  return `curl -X POST ${origin}/inference \\\n  -H "X-API-Key: $API_KEY" -H 'content-type: application/json' \\\n  -d '${body.replace(/'/g, "'\\''")}'`;
}
