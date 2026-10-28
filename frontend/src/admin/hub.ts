// What the Hub section (HubSection.tsx) needs that is not drawing: the
// search query, a download row's progress, merging fresh download rows into
// what is shown (to keep a speed estimate), and the order of local repos.

import type { Operation } from "../shared/api";
import type { Status } from "../shared/types";
import { fmtBytes } from "../shared/format";
import type { HubDownload, HubDownloadStatus, HubLocalRepo, HubSearchResult } from "../shared/types";

// ---------------------------------------------------------------- search
type SearchQuery = NonNullable<Operation<"/hub/search", "get">["parameters"]["query"]>;
/** A task the search can filter by, or "" for every task. */
export type TaskFilter = NonNullable<SearchQuery["task"]> | "";

/** The tasks the search can filter by; the select offers "All tasks" above them. */
export const TASK_FILTERS: Exclude<TaskFilter, "">[] = [
  "text-generation",
  "vision-language",
  "text-classification",
  "question-answering",
  "embedding",
  "rerank",
  "speech-to-text",
];

/** GET /hub/search's query for ``q``, filtered to ``task`` unless it is "". */
export function searchQuery(q: string, task: TaskFilter, limit = 30): SearchQuery {
  return { q: q.trim(), task: task || undefined, limit };
}

export const hubUrl = (id: string): string => `https://huggingface.co/${id}`;

type SearchAction = "download" | "on_disk" | "downloading" | "gated";

/** What the row's button offers: the weights are there, on their way, locked behind a token, or to be fetched. */
export function searchAction(result: HubSearchResult, downloads: HubDownload[], localIds: Set<string>): SearchAction {
  if (result.on_disk || localIds.has(result.id)) return "on_disk";
  if (downloads.some((d) => d.repo_id === result.id && isActive(d))) return "downloading";
  if (result.gated) return "gated";
  return "download";
}

// ------------------------------------------------------------- downloads
export const DOWNLOAD_STATUS: Record<HubDownloadStatus, Status> = {
  queued: { label: "Queued", color: "var(--axis)" },
  running: { label: "Running", color: "var(--series-1)" },
  done: { label: "Done", color: "var(--good)" },
  failed: { label: "Failed", color: "var(--critical)" },
  cancelled: { label: "Cancelled", color: "var(--warning)" },
};

export const isActive = (d: HubDownload): boolean => d.status === "queued" || d.status === "running";

export interface Progress {
  /** 0..1, or null when the total is unknown (an indeterminate bar) */
  fraction: number | null;
  /** "1.2 GB of 4.5 GB", or "1.2 GB so far" without a total */
  label: string;
}

export function downloadProgress(d: HubDownload): Progress {
  if (d.total_bytes == null || d.total_bytes <= 0) {
    // Done without a reported total: everything that arrived is the whole.
    if (d.status === "done") return { fraction: 1, label: fmtBytes(d.done_bytes) };
    return { fraction: null, label: `${fmtBytes(d.done_bytes)} so far` };
  }
  const fraction = Math.min(1, Math.max(0, d.done_bytes / d.total_bytes));
  return { fraction, label: `${fmtBytes(d.done_bytes)} of ${fmtBytes(d.total_bytes)}` };
}

/** A download row with what the polls add to it. */
export interface DownloadView {
  row: HubDownload;
  /** Bytes per second between the last two polls that moved; null until known or when not running */
  speed: number | null;
  /** When ``row`` was last sampled (ms) */
  sampledAt: number;
}

/**
 * The fresh rows in the server's order, each keeping the speed estimated
 * from the previous poll while it runs. A poll less than a second after the
 * last one keeps the old estimate rather than amplifying noise.
 */
export function mergeDownloads(prev: DownloadView[], fresh: HubDownload[], now: number): DownloadView[] {
  const before = new Map(prev.map((v) => [v.row.id, v]));
  return fresh.map((row) => {
    const old = before.get(row.id);
    if (row.status !== "running") return { row, speed: null, sampledAt: now };
    if (!old) return { row, speed: null, sampledAt: now };
    const dt = (now - old.sampledAt) / 1000;
    if (dt < 1) return { row, speed: old.speed, sampledAt: old.sampledAt };
    return { row, speed: Math.max(0, row.done_bytes - old.row.done_bytes) / dt, sampledAt: now };
  });
}

/** Whether a row that was queued or running (or unseen) now reads done, so what is on disk changed. */
export function newlyFinished(prev: HubDownload[], fresh: HubDownload[]): boolean {
  const was = new Map(prev.map((d) => [d.id, d.status]));
  return fresh.some((d) => d.status === "done" && was.get(d.id) !== "done");
}

export const fmtSpeed = (bytesPerSecond: number): string => `${fmtBytes(bytesPerSecond)}/s`;

// ----------------------------------------------------------------- local
/** Loaded repos first, then the most recently used, then by id; never used sorts last. */
export function sortLocal(repos: HubLocalRepo[]): HubLocalRepo[] {
  return [...repos].sort((a, b) => {
    if (a.loaded !== b.loaded) return a.loaded ? -1 : 1;
    const used = (b.last_accessed ?? -1) - (a.last_accessed ?? -1);
    return used || a.id.localeCompare(b.id);
  });
}
