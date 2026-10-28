// Number, time and size formatting shared by the pages. Missing values show as "–".
import type { Status } from "./types";

export const fmtInt = (n: number | null | undefined): string => (n == null ? "–" : Math.round(n).toLocaleString());

export function fmtCompact(n: number | null | undefined): string {
  if (n == null) return "–";
  if (n < 1000) return String(Math.round(n));
  return new Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 }).format(n);
}

export function fmtMs(ms: number | null | undefined): string {
  if (ms == null) return "–";
  if (ms > 0 && ms < 1) return "<1 ms";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(ms < 10000 ? 2 : 1)} s`;
  return `${(ms / 60000).toFixed(1)} min`;
}

/** Bytes as the nearest unit: "0 B", "640 KB", "1.5 MB", "2.3 GB". */
export function fmtBytes(n: number | null | undefined): string {
  if (n == null) return "–";
  if (n < 1024) return `${Math.round(n)} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = n / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit++;
  }
  return `${value.toFixed(value < 10 ? 1 : 0)} ${units[unit]}`;
}

export const fmtGB = (gb: number | null | undefined): string => (gb == null ? "–" : `${gb.toFixed(gb < 10 ? 2 : 1)} GB`);

export const fmtPct = (x: number): string => `${(x * 100).toFixed(x > 0 && x < 0.01 ? 2 : 1)}%`;

/** A Unix time as a clock time, with the weekday when ``withDay``. */
export function fmtClock(ts: number, withDay = false): string {
  const opts: Intl.DateTimeFormatOptions = { hour: "2-digit", minute: "2-digit" };
  if (withDay) opts.weekday = "short";
  return new Date(ts * 1000).toLocaleString(undefined, opts);
}

export const fmtTime = (ts: number): string =>
  new Date(ts * 1000).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" });

export function fmtAgo(ts: number | null | undefined): string {
  if (!ts) return "–";
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return `${Math.round(s)}s ago`;
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${(s / 3600).toFixed(1)} h ago`;
  return `${(s / 86400).toFixed(1)} d ago`;
}

export function fmtDuration(s: number): string {
  if (s < 3600) return `${Math.round(s / 60)} min`;
  if (s < 86400) return `${(s / 3600).toFixed(1)} h`;
  return `${(s / 86400).toFixed(1)} d`;
}

/** A status's label and colour from ``statuses``; one this build does not know is shown as it is, in ``fallback``. */
export function statusOf(statuses: Record<string, Status>, key: string, fallback = "var(--axis)"): Status {
  return statuses[key] ?? { label: key, color: fallback };
}

