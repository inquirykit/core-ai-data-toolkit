// What the Evidence section (EvidenceSection.tsx) needs that is not drawing:
// where the hash points (a case, a capture, or a search across the cases),
// short forms of hashes and times, the words of a capture's time stamp state
// and of an enrichment item, a search's words to mark in a snippet, and the
// verify report's one-line summary.

import { parseHash } from "../../shared/browser";
import { plural } from "../../shared/format";
import type { Status } from "../../shared/types";
import type { CaptureSummary, EnrichmentItem, VerifyReport } from "../../shared/types";

// ------------------------------------------------------------------ hash
export interface EvidenceLocation {
  /** The workspace whose ledger is shown: "?workspace=" in the hash, the default one without */
  workspace: string;
  /** The case whose captures are listed; null before one is chosen (then the inbox) */
  caseId: string | null;
  /** The capture whose record is open, within that case */
  captureId: string | null;
  /** The search across the workspace's cases shown instead ("#/evidence/_search?q=..."); null for none */
  search: EvidenceSearch | null;
}

export type SearchSort = "" | "relevance" | "newest" | "oldest";

/** A search of the workspace's captures (GET /evidence/search), each part empty when not used. */
export interface EvidenceSearch {
  q: string;
  case: string;
  tag: string;
  /** Dates (YYYY-MM-DD, UTC), as a date input gives them */
  since: string;
  until: string;
  sort: SearchSort;
}

export const EMPTY_SEARCH: EvidenceSearch = { q: "", case: "", tag: "", since: "", until: "", sort: "" };
const SEARCH_KEYS = Object.keys(EMPTY_SEARCH) as (keyof EvidenceSearch)[];
const SORTS: SearchSort[] = ["relevance", "newest", "oldest"];
/** The hash part that holds a search; case ids are UUIDs or "inbox", so it never names a case. */
const SEARCH_PART = "_search";

export const INBOX = "inbox";
/** The workspace that held every capture before there were workspaces; clients that name none use it. */
export const DEFAULT_WORKSPACE = "default";

/** What "#/evidence", "#/evidence/<case>" and "#/evidence/<case>/<capture>" point at, each with "?workspace=<id>"
 * for a workspace other than the default one. */
export function locationFromHash(hash: string): EvidenceLocation {
  const { parts, params } = parseHash(hash);
  const [section, caseId, captureId] = parts;
  if (section !== "evidence") return { workspace: DEFAULT_WORKSPACE, caseId: null, captureId: null, search: null };
  const workspace = params.get("workspace") || DEFAULT_WORKSPACE;
  if (caseId === SEARCH_PART) {
    const search = { ...EMPTY_SEARCH };
    for (const key of SEARCH_KEYS) if (key !== "sort") search[key] = params.get(key) ?? "";
    const sort = params.get("sort") as SearchSort;
    search.sort = SORTS.includes(sort) ? sort : "";
    return { workspace, caseId: null, captureId: null, search };
  }
  return { workspace, caseId: caseId ?? null, captureId: (caseId && captureId) || null, search: null };
}

/** Where a search of ``workspace`` is shown: its non-empty parts in the hash, so it can be linked to. */
export function searchHash(workspace: string, search: EvidenceSearch): string {
  const params = new URLSearchParams();
  if (workspace !== DEFAULT_WORKSPACE) params.set("workspace", workspace);
  for (const key of SEARCH_KEYS) if (search[key].trim()) params.set(key, search[key].trim());
  const query = params.toString();
  return `#/evidence/${SEARCH_PART}${query ? `?${query}` : ""}`;
}

/** The query of GET /evidence/search: the workspace and the search's non-empty parts. */
export function searchQuery(workspace: string, search: EvidenceSearch, cursor?: string) {
  const parts = Object.fromEntries(SEARCH_KEYS.map((key) => [key, search[key].trim() || undefined]));
  return { ...workspaceQuery(workspace), ...parts, sort: (search.sort || undefined) as Exclude<SearchSort, ""> | undefined, cursor };
}

/**
 * What a search looks for, to mark in a snippet: its words and "phrases", lowercased, without the -excluded
 * ones, and the start of a word*.
 */
export function searchTerms(q: string): string[] {
  const terms: string[] = [];
  for (const [, minus, phrase, word] of q.matchAll(/(-?)(?:"([^"]*)"?|(\S+))/g)) {
    const term = (phrase ?? word ?? "").replace(/\*+$/, "").replace(/"/g, " ").trim().replace(/\s+/g, " ").toLowerCase();
    if (!minus && /\w/.test(term)) terms.push(term);
  }
  return terms;
}

/** ``text`` cut into the parts that hold one of ``terms`` (``hit``) and those that do not, in order. */
export function marked(text: string, terms: string[]): { text: string; hit: boolean }[] {
  terms = terms.filter(Boolean);
  const lowered = text.toLowerCase();
  const parts: { text: string; hit: boolean }[] = [];
  let at = 0;
  while (at < text.length) {
    let next = -1;
    let length = 0;
    for (const term of terms) {
      const i = lowered.indexOf(term, at);
      if (i >= 0 && (next < 0 || i < next || (i === next && term.length > length))) [next, length] = [i, term.length];
    }
    if (next < 0) break;
    if (next > at) parts.push({ text: text.slice(at, next), hit: false });
    parts.push({ text: text.slice(next, next + length), hit: true });
    at = next + length;
  }
  if (at < text.length) parts.push({ text: text.slice(at), hit: false });
  return parts;
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
    return n ? `${plural(n, "source")}, nothing found` : "no enricher takes this type";
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
  const records = plural(report.records, "record");
  if (!report.records) return "The ledger is empty.";
  const head = report.ok ? `All ${records} verify.` : `${plural(errors, "error")} in ${records}.`;
  const stamps = `${report.timestamped} of ${report.records} timestamped`;
  return warnings ? `${head} ${stamps}, ${plural(warnings, "warning")}.` : `${head} ${stamps}.`;
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
