// What the Evidence section (EvidenceSection.tsx) needs that is not drawing:
// where the hash points, short forms of hashes and times, the words of a
// capture's time stamp state and of an enrichment item, and the verify
// report's one-line summary.

import { parseHash } from "../shared/browser";
import type { Status } from "../shared/types";
import type { CaptureSummary, EnrichmentItem, VerifyReport } from "../shared/types";

// ------------------------------------------------------------------ hash
export interface EvidenceLocation {
  /** The workspace whose ledger is shown: "?workspace=" in the hash, the default one without */
  workspace: string;
  /** The case whose captures are listed; null before one is chosen (then the inbox) */
  caseId: string | null;
  /** The capture whose record is open, within that case */
  captureId: string | null;
}

export const INBOX = "inbox";
/** The workspace that held every capture before there were workspaces; clients that name none use it. */
export const DEFAULT_WORKSPACE = "default";

/** What "#/evidence", "#/evidence/<case>" and "#/evidence/<case>/<capture>" point at, each with "?workspace=<id>"
 * for a workspace other than the default one. */
export function locationFromHash(hash: string): EvidenceLocation {
  const { parts, params } = parseHash(hash);
  const [section, caseId, captureId] = parts;
  if (section !== "evidence") return { workspace: DEFAULT_WORKSPACE, caseId: null, captureId: null };
  const workspace = params.get("workspace") || DEFAULT_WORKSPACE;
  return { workspace, caseId: caseId ?? null, captureId: (caseId && captureId) || null };
}

export function evidenceHash(workspace: string, caseId: string | null, captureId?: string | null): string {
  let hash = "#/evidence";
  if (caseId) hash += `/${encodeURIComponent(caseId)}`;
  if (caseId && captureId) hash += `/${encodeURIComponent(captureId)}`;
  return workspace === DEFAULT_WORKSPACE ? hash : `${hash}?workspace=${encodeURIComponent(workspace)}`;
}

/** The query that puts an /evidence call in ``workspace``: the routes about cases and captures act in one. */
export const workspaceQuery = (workspace: string): { workspace?: string } => ({
  workspace: workspace === DEFAULT_WORKSPACE ? undefined : workspace,
});

// ----------------------------------------------------------------- words
/** The first and last characters of a hash, for tables; the whole hash goes in a title. */
export function shortHash(hash: string | null | undefined, chars = 8): string {
  if (!hash) return "–";
  return hash.length <= chars * 2 + 1 ? hash : `${hash.slice(0, chars)}…${hash.slice(-chars)}`;
}

/** An ISO time as the browser's locale shows it, with seconds; the raw text when it does not parse. */
export function fmtIso(iso: string | null | undefined): string {
  if (!iso) return "–";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return iso;
  return new Date(t).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "medium" });
}

/** The time stamp column: granted (with its time) or not yet. */
export function timestampState(capture: Pick<CaptureSummary, "timestampedAt">): Status {
  return capture.timestampedAt ? { label: "Timestamped", color: "var(--good)" } : { label: "No timestamp", color: "var(--warning)" };
}

/** What a capture is called in lists: its title, else its URL, else its id. */
export function captureLabel(capture: Pick<CaptureSummary, "title" | "url" | "captureId">): string {
  return capture.title?.trim() || capture.url || capture.captureId;
}

/** One enrichment item in a line: what was found, or why nothing was. */
export function enrichmentLine(item: EnrichmentItem): string {
  if (item.error) return `failed: ${item.error}`;
  const merged = item.results?.merged ?? {};
  const keys = Object.keys(merged);
  if (!keys.length) {
    const n = item.results?.results.length ?? 0;
    return n ? `${n} source${n === 1 ? "" : "s"}, nothing found` : "no enricher takes this type";
  }
  return keys
    .slice(0, 6)
    .map((k) => `${k}: ${fmtValue(merged[k])}`)
    .join(" · ") + (keys.length > 6 ? ` · ${keys.length - 6} more` : "");
}

function fmtValue(value: unknown): string {
  if (value == null) return "–";
  if (typeof value === "string") return value.length > 60 ? `${value.slice(0, 60)}…` : value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  const text = JSON.stringify(value) ?? "";
  return text.length > 60 ? `${text.slice(0, 60)}…` : text;
}

/** The verify report in a sentence. */
export function verifySummary(report: VerifyReport): string {
  const errors = report.issues.filter((i) => i.severity === "error").length;
  const warnings = report.issues.length - errors;
  const records = `${report.records} record${report.records === 1 ? "" : "s"}`;
  if (!report.records) return "The ledger is empty.";
  const head = report.ok ? `All ${records} verify.` : `${errors} error${errors === 1 ? "" : "s"} in ${records}.`;
  const stamps = `${report.timestamped} of ${report.records} timestamped`;
  return warnings ? `${head} ${stamps}, ${warnings} warning${warnings === 1 ? "" : "s"}.` : `${head} ${stamps}.`;
}

/** Issues grouped by record, in log order, errors before warnings within a record. */
export function issuesBySeq(report: VerifyReport): [seq: number, issues: VerifyReport["issues"]][] {
  const groups = new Map<number, VerifyReport["issues"]>();
  for (const issue of report.issues) {
    const list = groups.get(issue.seq) ?? [];
    list.push(issue);
    groups.set(issue.seq, list);
  }
  return [...groups.entries()]
    .sort((a, b) => a[0] - b[0])
    .map(([seq, issues]) => [seq, [...issues].sort((a, b) => (a.severity === b.severity ? 0 : a.severity === "error" ? -1 : 1))]);
}
