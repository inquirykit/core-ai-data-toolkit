// What the Evals section (and the request detail on Activity & logs) needs
// that is not drawing: where the hash points, previews of inputs and
// outputs, how a score reads, and the JSON the forms take. Tested in
// evals.test.ts.

import { parseHash } from "../shared/browser";
import type { Status } from "../shared/types";
import { parseJsonObject } from "../shared/json";
import type { MetricSummary, Run, RunStatus } from "../shared/types";

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

/** A value as it is shown in a detail pane. */
export const pretty = (value: unknown): string => (typeof value === "string" ? value : JSON.stringify(value, null, 2) ?? "");

// ------------------------------------------------------------------ scores
/** A score as text: a share for 0–1 metrics (the judge's too), the verdict for a pairwise one, "–" when there is none. */
export function fmtScore(value: number | null | undefined, metric: string, label?: string | null): string {
  if (value == null) return "–";
  if (metric.startsWith("judge_vs_") && label) return label;
  return `${Math.round(value * 100)}%`;
}

/** The mean of a metric over a run, as text: a share, or wins, ties and losses for a pairwise one. */
export function fmtMean(summary: MetricSummary | undefined, metric: string): string {
  if (!summary || summary.mean == null) return "–";
  if (metric.startsWith("judge_vs_") && summary.wins != null) return `${summary.wins}W ${summary.ties ?? 0}T ${summary.losses ?? 0}L`;
  return `${Math.round(summary.mean * 100)}%`;
}

/** good above 0.8, warning above 0.5, critical below; nothing for a missing score. */
export function scoreTone(value: number | null | undefined): "good" | "warning" | "critical" | null {
  if (value == null) return null;
  return value >= 0.8 ? "good" : value >= 0.5 ? "warning" : "critical";
}

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
  done: { label: "Done", color: "var(--good)" },
  failed: { label: "Failed", color: "var(--critical)" },
  cancelled: { label: "Cancelled", color: "var(--warning)" },
  interrupted: { label: "Interrupted", color: "var(--warning)" },
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
