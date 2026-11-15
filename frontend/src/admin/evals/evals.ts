// What the Evals section (and the request detail on Activity & logs) needs
// that is not drawing: where the hash points, previews of inputs and
// outputs, how a score reads, and the JSON the forms take. Tested in
// evals.test.ts.

import { parseHash } from "../../shared/browser";
import type { Status } from "../../shared/types";
import { parseJsonObject } from "../../shared/json";
import type { MetricSummary, Run, RunStatus } from "../../shared/types";

// ------------------------------------------------------------------ hashes
export interface EvalsLocation {
  dataset: string | null;
  run: string | null;
  /** Two runs shown side by side */
  compare: [string, string] | null;
}

/** #/evals, #/evals/<dataset>, #/evals/<dataset>/runs/<run> or #/evals/<dataset>/compare/<a>/<b>. */
export function evalsFromHash(hash: string): EvalsLocation {
  const { parts } = parseHash(hash);
  if (parts[0] !== "evals" || !parts[1]) return { dataset: null, run: null, compare: null };
  const dataset = parts[1];
  if (parts[2] === "runs" && parts[3]) return { dataset, run: parts[3], compare: null };
  if (parts[2] === "compare" && parts[3] && parts[4]) return { dataset, run: null, compare: [parts[3], parts[4]] };
  return { dataset, run: null, compare: null };
}

export function evalsHref(dataset?: string | null, run?: string | null): string {
  if (!dataset) return "#/evals";
  const base = `#/evals/${encodeURIComponent(dataset)}`;
  return run ? `${base}/runs/${encodeURIComponent(run)}` : base;
}

export const compareHref = (dataset: string, a: string, b: string): string =>
  `#/evals/${encodeURIComponent(dataset)}/compare/${encodeURIComponent(a)}/${encodeURIComponent(b)}`;

// ---------------------------------------------------------------- previews
const VECTOR_KEYS = new Set(["embedding", "embeddings", "vector"]);

/** The strings in a value, joined: what a reader would search for. */
export function textOf(value: unknown, limit = 400): string {
  const parts: string[] = [];
  let size = 0;
  const walk = (v: unknown) => {
    if (size >= limit) return;
    if (typeof v === "string") {
      parts.push(v);
      size += v.length + 1;
    } else if (Array.isArray(v)) v.forEach(walk);
    else if (v && typeof v === "object") {
      if ("$media" in (v as object)) return;
      for (const [k, item] of Object.entries(v as Record<string, unknown>)) if (!VECTOR_KEYS.has(k)) walk(item);
    }
  };
  walk(value);
  return parts.join(" ").slice(0, limit);
}

/** The part of an input a reader calls "the prompt": the last message, or the text field, else all its words. */
export function inputPreview(input: unknown, limit = 400): string {
  if (input && typeof input === "object" && !Array.isArray(input)) {
    const o = input as Record<string, unknown>;
    const messages = o.messages;
    if (Array.isArray(messages) && messages.length) {
      const last = messages[messages.length - 1] as Record<string, unknown> | undefined;
      if (last && typeof last.content === "string") return last.content.slice(0, limit);
      if (last) return textOf(last.content, limit);
    }
    for (const key of ["text", "query", "question", "prompt"]) if (typeof o[key] === "string") return (o[key] as string).slice(0, limit);
  }
  return textOf(input, limit);
}

/** What a model answered, as text: its text field, else the result as JSON. */
export function outputPreview(output: unknown, limit = 400): string {
  if (output == null) return "";
  if (typeof output === "string") return output.slice(0, limit);
  if (typeof output === "object" && !Array.isArray(output)) {
    const o = output as Record<string, unknown>;
    for (const key of ["text", "answer", "transcript", "label"]) if (typeof o[key] === "string") return (o[key] as string).slice(0, limit);
  }
  return JSON.stringify(output).slice(0, limit);
}

/** A reference answer as text. */
export function referencePreview(reference: unknown, limit = 400): string {
  if (reference == null) return "";
  if (typeof reference === "string") return reference.slice(0, limit);
  return outputPreview(reference, limit);
}

// ------------------------------------------------------------------ scores
/** A score as text: a share for 0–1 metrics (the judge's too), the verdict for a pairwise one, "–" when there is none. */
export function fmtScore(value: number | null | undefined, metric: string, label?: string | null): string {
  if (value == null) return "–";
  if (metric.startsWith("judge_vs_") && label) return label;
  return fmtShare(value);
}

/** The mean of a metric over a run, as text: a share, or wins, ties and losses for a pairwise one. */
export function fmtMean(summary: MetricSummary | undefined, metric: string): string {
  if (!summary || summary.mean == null) return "–";
  if (metric.startsWith("judge_vs_") && summary.wins != null) return `${summary.wins}W ${summary.ties ?? 0}T ${summary.losses ?? 0}L`;
  return fmtShare(summary.mean);
}

/** good above 0.8, warning above 0.5, critical below (for a metric where less is better, good below 0.2, warning
 * below 0.5); nothing for a missing score. */
export function scoreTone(value: number | null | undefined, lowerIsBetter = false): "good" | "warning" | "critical" | null {
  if (value == null) return null;
  const v = lowerIsBetter ? 1 - value : value;
  return v >= 0.8 ? "good" : v >= 0.5 ? "warning" : "critical";
}

/** The text colour of a tone. */
export const TONE_CLASS = { good: "text-good-text", warning: "text-warning", critical: "text-critical-text" } as const;

/** A share as a whole percentage, "–" for none. */
export const fmtShare = (value: number | null | undefined): string => (value == null ? "–" : `${Math.round(value * 100)}%`);

/** "exact match", "vs <the other run's name>", "judge". */
export function metricLabel(metric: string, runs: Pick<Run, "id" | "name">[] = []): string {
  if (metric.startsWith("judge_vs_")) {
    const other = metric.slice("judge_vs_".length);
    return `vs ${runs.find((r) => r.id === other)?.name ?? other.slice(0, 8)}`;
  }
  return metric.replace(/_/g, " ");
}

export const RUN_STATUS: Record<RunStatus, Status> = {
  queued: { label: "Queued", color: "var(--axis)" },
  running: { label: "Running", color: "var(--series-1)" },
  succeeded: { label: "Done", color: "var(--good)" },
  failed: { label: "Failed", color: "var(--critical)" },
  cancelled: { label: "Cancelled", color: "var(--warning)" },
};

/** What a run is doing now, for its row: "Running 3/10", "Judging 2/10", "Queued", or its status. */
export function runProgress(run: Run): string {
  const a = run.activity;
  if (a?.error) return `Judging failed: ${a.error}`;
  if (a?.kind === "judging") return `Judging ${a.done ?? 0}/${a.total ?? 0}`;
  if (a?.kind === "running") return `Running ${a.done ?? 0}/${a.total ?? 0}`;
  if (a?.kind === "queued") return "Queued";
  return RUN_STATUS[run.status]?.label ?? run.status;
}

// ------------------------------------------------------------------- forms
/** The JSON object a form field holds, or what is wrong with it. */
export const parseObject = (text: string, what = "The input") => parseJsonObject(text, { what });

/** A reference typed into a form: JSON when it parses as an object or array, else the text itself; empty is none. */
export function parseReference(text: string): unknown {
  const t = text.trim();
  if (!t) return null;
  if (/^[[{]/.test(t)) {
    try {
      return JSON.parse(t);
    } catch {
      /* plain text then */
    }
  }
  return t;
}

// ---------------------------------------------------------------- labeling
/** Label choices typed as a list: split at commas and new lines, trimmed, each once. */
export function parseLabels(text: string): string[] {
  return [...new Set(text.split(/[,\n]/).map((l) => l.trim()).filter(Boolean))];
}

export interface Suggestion {
  /** The label a classifier gave, if it gave one */
  label: string | null;
  confidence: number | null;
  /** The answer as text: what a reference typed from it starts as */
  text: string;
}

/** What a suggestion (a model's output for the example) proposes; null for none. */
export function suggestionOf(output: unknown): Suggestion | null {
  if (output == null) return null;
  const o = typeof output === "object" && !Array.isArray(output) ? (output as Record<string, unknown>) : {};
  const label = typeof o.label === "string" ? o.label : null;
  const confidence = typeof o.confidence === "number" ? o.confidence : null;
  return { label, confidence, text: label ?? outputPreview(output, 100_000) };
}

/** The label a key picks: 1 to 9 for the first nine choices; null for any other key. */
export function labelForKey(key: string, labels: string[]): string | null {
  const n = /^[1-9]$/.test(key) ? Number(key) : 0;
  return n && n <= labels.length ? labels[n - 1] : null;
}

/** The choice a suggestion's label is, matched ignoring case; null when it is none of them. */
export function matchingLabel(suggestion: Suggestion | null, labels: string[]): string | null {
  const label = suggestion?.label?.trim().toLowerCase();
  return label ? (labels.find((l) => l.toLowerCase() === label) ?? null) : null;
}

// ---------------------------------------------------------------- training
/** The job's params the dataset gives itself: its training file, and the fields of each row in it. */
export const TRAINING_FILE_FIELDS = ["train_file", "text_column", "label_column"];

/** A job's params schema without the fields the dataset's training file fills in. */
export function trainingSchema<S extends { properties?: Record<string, unknown>; required?: string[] }>(schema: S): S {
  const properties = Object.fromEntries(Object.entries(schema.properties ?? {}).filter(([k]) => !TRAINING_FILE_FIELDS.includes(k)));
  return { ...schema, properties, required: (schema.required ?? []).filter((k) => !TRAINING_FILE_FIELDS.includes(k)) };
}
