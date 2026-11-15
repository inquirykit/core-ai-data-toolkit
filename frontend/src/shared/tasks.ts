// The one queue of background work (GET /queue, tasks/runner.py) as every section that lists tasks shows it (Jobs,
// Running, AutoGluon, Workflows, Evals): the words and colors of a status, whether a task is still going, how fast to
// poll, its progress, a schedule's cadence (tasks/schedule.py) and the priorities.

import { statusOf } from "./format";
import type { Priority, Schedule, Status, Task, TaskStatus } from "./types";

// ----------------------------------------------------------------- status
const TASK_STATUS: Record<TaskStatus, Status> = {
  queued: { label: "Queued", color: "var(--axis)" },
  running: { label: "Running", color: "var(--series-1)" },
  succeeded: { label: "Succeeded", color: "var(--good)" },
  failed: { label: "Failed", color: "var(--critical)" },
  cancelled: { label: "Cancelled", color: "var(--warning)" },
};

/** The label and color of a status, also one this build does not know. */
export function taskStatus(status: string): Status {
  return statusOf(TASK_STATUS, status, "var(--muted-foreground)");
}

export const isActive = (task: Pick<Task, "status">): boolean => task.status === "queued" || task.status === "running";

/** A list of tasks is polled fast while one is queued or running, slowly otherwise (finished rows rarely change). */
export const POLL_ACTIVE_MS = 2000;
export const POLL_IDLE_MS = 15000;

export const pollInterval = (tasks: Pick<Task, "status">[] | null): number => (tasks?.some(isActive) ? POLL_ACTIVE_MS : POLL_IDLE_MS);

/**
 * The bar's fill, 0..1; null while a running task has not reported progress
 * (an indeterminate bar). A task that succeeded without reporting is full.
 */
export function progressValue(task: Pick<Task, "status" | "progress">): number | null {
  if (task.progress != null) return Math.min(1, Math.max(0, task.progress));
  if (task.status === "running") return null;
  return task.status === "succeeded" ? 1 : 0;
}

// ---------------------------------------------------------------- cadence
export type IntervalUnit = "minutes" | "hours" | "days";

export const INTERVAL_UNITS: [IntervalUnit, string][] = [
  ["minutes", "minutes"],
  ["hours", "hours"],
  ["days", "days"],
];

const UNIT_SECONDS: Record<IntervalUnit, number> = { minutes: 60, hours: 3600, days: 86400 };

/** ``amount`` of ``unit`` in seconds, or null unless it is a whole positive number. */
export function intervalSeconds(amount: string | number, unit: IntervalUnit): number | null {
  const n = typeof amount === "number" ? amount : Number(String(amount).trim());
  if (!String(amount).trim() || !Number.isInteger(n) || n <= 0) return null;
  return n * UNIT_SECONDS[unit];
}

/** Seconds as the largest unit that divides them: 7200 is 2 hours, 90 is 90 seconds (shown as minutes, 1.5). */
export function intervalParts(seconds: number): { amount: number; unit: IntervalUnit } {
  for (const unit of ["days", "hours", "minutes"] as IntervalUnit[]) {
    if (seconds % UNIT_SECONDS[unit] === 0 && seconds >= UNIT_SECONDS[unit]) return { amount: seconds / UNIT_SECONDS[unit], unit };
  }
  return { amount: Math.round((seconds / 60) * 100) / 100, unit: "minutes" };
}

/** A schedule's cadence in a few words: its cron expression, or "every 2 h" from ``interval_s``. */
export function describeCadence(s: Pick<Schedule, "cron" | "interval_s">): string {
  if (s.cron) return s.cron.trim();
  if (s.interval_s == null || s.interval_s <= 0) return "–";
  if (s.interval_s < 60) return `every ${s.interval_s} s`;
  const { amount, unit } = intervalParts(s.interval_s);
  const short = unit === "minutes" ? "min" : unit === "hours" ? "h" : "d";
  return `every ${amount} ${short}`;
}

// --------------------------------------------------------------- priority
/** The priorities a task, schedule or eval run can be given, for a select. */
const PRIORITY_LABEL: Record<Priority, string> = { high: "High", normal: "Normal", low: "Low" };
export const PRIORITY_OPTIONS = (Object.keys(PRIORITY_LABEL) as Priority[]).map((value) => ({ value, label: PRIORITY_LABEL[value] }));
