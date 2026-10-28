// The Activity & logs page's arithmetic: the time range it looks at, how it
// zooms and pans, how its times read, the rows of its requests table and the
// log filter. No React here, so it is tested directly (activity.test.ts).

import { parseHash } from "../shared/browser";
import type { LogLine, RequestRow, TraceSummary } from "../shared/types";

/** Unix seconds. */
export interface Range {
  since: number;
  until: number;
}

/**
 * What the page looks at: the ``span`` seconds up to ``until``, or up to now
 * when ``until`` is null. A live view slides along as time passes.
 */
export interface View {
  span: number;
  until: number | null;
}

export const PRESETS: [key: string, label: string, span: number][] = [
  ["15m", "15 min", 900],
  ["1h", "1 hour", 3600],
  ["6h", "6 hours", 6 * 3600],
  ["24h", "24 hours", 86400],
  ["7d", "7 days", 7 * 86400],
];
export const DEFAULT_SPAN = 3600;
/** No zooming in past a minute, nor out past the server's limit (admin_routes.MAX_SPAN_S). */
const MIN_SPAN_S = 60;
const MAX_SPAN_S = 90 * 86400;
/** A range ending this close to now is live: it follows the clock. */
const LIVE_TOLERANCE_S = 20;
/** The navigator strip shows at least this long. */
export const CONTEXT_SPAN_S = 7 * 86400;

export const presetView = (span: number): View => ({ span, until: null });

export function rangeOf(view: View, now: number): Range {
  const until = view.until ?? now;
  return { since: until - view.span, until };
}

/** The view showing ``range``, within the span limits and never past now; live when it reaches now. */
export function viewOf(range: Range, now: number): View {
  const span = Math.min(MAX_SPAN_S, Math.max(MIN_SPAN_S, range.until - range.since));
  const until = Math.min(range.until, now);
  return { span, until: now - until <= LIVE_TOLERANCE_S ? null : until };
}

const clampRange = (range: Range, now: number): Range => rangeOf(viewOf(range, now), now);

/** ``range`` scaled by ``factor`` (below 1 zooms in) about the point ``at`` (0 = its start, 1 = its end). */
export function zoomRange(range: Range, factor: number, at: number, now: number): Range {
  const span = range.until - range.since;
  const next = Math.min(MAX_SPAN_S, Math.max(MIN_SPAN_S, span * factor));
  const pivot = range.since + span * Math.min(1, Math.max(0, at));
  const since = pivot - next * at;
  return clampRange({ since, until: since + next }, now);
}

/** ``range`` moved by ``dt`` seconds (negative: into the past), never past now. */
export const panRange = (range: Range, dt: number, now: number): Range =>
  clampRange({ since: range.since + dt, until: range.until + dt }, now);

/** The preset a view is, if it is one. */
export function presetOf(view: View): string | null {
  if (view.until != null) return null;
  return PRESETS.find(([, , span]) => span === view.span)?.[0] ?? null;
}

/** How many points to ask for, so each is a few pixels wide. */
export const bucketsFor = (plotWidth: number): number => Math.max(24, Math.min(240, Math.round(plotWidth / 6)));

// ------------------------------------------------------------------- time
/** A duration in the largest unit that reads well: "45 s", "1.5 min", "7 d". */
function fmtSpan(s: number): string {
  const unit = (value: number, name: string) => `${Number.isInteger(value) ? value : value.toFixed(1)} ${name}`;
  if (s < 60) return `${Math.round(s)} s`;
  if (s < 3600) return unit(Math.round(s / 6) / 10, "min");
  if (s < 86400) return unit(Math.round(s / 360) / 10, "h");
  return unit(Math.round(s / 8640) / 10, "d");
}

const sameDay = (a: number, b: number) => new Date(a * 1000).toDateString() === new Date(b * 1000).toDateString();

/**
 * A moment inside ``range``: the clock time, with seconds when the range is
 * short and the weekday when it spans more than a day.
 */
export function fmtStamp(ts: number, range: Range, withSeconds = range.until - range.since < 600): string {
  const opts: Intl.DateTimeFormatOptions = { hour: "2-digit", minute: "2-digit" };
  if (withSeconds) opts.second = "2-digit";
  if (!sameDay(range.since, range.until)) opts.weekday = "short";
  return new Date(ts * 1000).toLocaleString(undefined, opts);
}

/** "14:02 – 15:32 · 1.5 h", for the toolbar. */
export function fmtRange(range: Range): string {
  return `${fmtStamp(range.since, range)} – ${fmtStamp(range.until, range)} · ${fmtSpan(range.until - range.since)}`;
}

/** The interval a timeline point covers, for its tooltip. */
export function fmtBucket(start: number, bucketS: number, range: Range): string {
  const withSeconds = bucketS < 60;
  return `${fmtStamp(start, range, withSeconds)} – ${fmtStamp(start + bucketS, range, withSeconds)}`;
}

// ----------------------------------------------------------------- events
/** Groups of items whose x are within ``gap`` of their neighbour's, in order. */
export function clusterByX<T extends { x: number }>(items: T[], gap: number): T[][] {
  const sorted = [...items].sort((a, b) => a.x - b.x);
  const groups: T[][] = [];
  for (const item of sorted) {
    const last = groups[groups.length - 1];
    if (last && item.x - last[last.length - 1].x <= gap) last.push(item);
    else groups.push([item]);
  }
  return groups;
}

// --------------------------------------------------------------- requests
/** A row of the requests table: a request's metadata, or a trace when the words are searched. */
export interface RequestItem {
  key: string;
  ts: number;
  model_id: string;
  task: string;
  status: string;
  duration_ms: number;
  cold: boolean;
  error: string | null;
  /** Opens the input and output (#/activity/<id>); missing for rows recorded before tracing existed */
  trace_id: string | null;
  /** The first words of the input and output, known for traces only */
  preview: string | null;
  tags: string[];
}

export const fromRequestRow = (r: RequestRow): RequestItem => ({
  key: `r${r.id}`,
  ts: r.ts,
  model_id: r.model_id,
  task: r.task,
  status: r.status,
  duration_ms: r.duration_ms,
  cold: Boolean(r.cold),
  error: r.error,
  trace_id: r.trace_id ?? null,
  preview: null,
  tags: [],
});

export const fromTrace = (t: TraceSummary): RequestItem => ({
  key: `t${t.id}`,
  ts: t.ts,
  model_id: t.model_id,
  task: t.task,
  status: t.status,
  duration_ms: t.duration_ms,
  cold: t.cold,
  error: t.error,
  trace_id: t.id,
  preview: t.preview,
  tags: t.tags,
});

/**
 * Whether the table has to come from the traces (GET /admin/traces) rather
 * than the request metadata: only traces have words to search and tags.
 */
export const searchesTraces = (words: string, tag: string): boolean => Boolean(words.trim() || tag.trim());

/** The trace #/activity/<id> opens, if any (#/traces/<id> is the old address and still works). */
export function traceFromHash(hash: string): string | null {
  const [section, id] = parseHash(hash).parts;
  return (section === "activity" || section === "traces") && id ? id : null;
}

// ------------------------------------------------------------------- logs
export const LOG_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR"] as const;
export type LogLevel = (typeof LOG_LEVELS)[number];
const LEVEL_NO: Record<LogLevel, number> = { DEBUG: 10, INFO: 20, WARNING: 30, ERROR: 40 };

/** Lines at ``level`` or above whose logger or message contains ``search`` (any case). */
export function filterLogs(lines: LogLine[], level: LogLevel, search: string): LogLine[] {
  const floor = LEVEL_NO[level];
  const needle = search.trim().toLowerCase();
  return lines.filter(
    (l) => l.levelno >= floor && (!needle || l.message.toLowerCase().includes(needle) || l.logger.toLowerCase().includes(needle)),
  );
}

/** ``previous`` with the new lines of ``incoming`` after it, the newest ``capacity`` kept. */
export function mergeLogs(previous: LogLine[], incoming: LogLine[], capacity: number): LogLine[] {
  const lastId = previous.length ? previous[previous.length - 1].id : 0;
  const fresh = incoming.filter((l) => l.id > lastId);
  if (!fresh.length) return previous;
  return [...previous, ...fresh].slice(-capacity);
}
