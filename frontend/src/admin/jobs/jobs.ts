// What the Jobs section (JobsSection.tsx, Jobs*.tsx) needs that is not drawing: the
// words and colors of statuses, how fast to poll, a schedule's cadence in
// words, checking a cron expression, and the schedule form to and from a
// schedule, and the outcome of a webhook test and a delivery's tries in words.
// A job's params are edited by a form made from its params schema (JobFields,
// shared/schema-form.tsx).

import type { Status } from "../../shared/types";
import { fmtDuration, statusOf } from "../../shared/format";
import type { Job, JobStatus, Priority, Schedule, ScheduleBody, WebhookDelivery, WebhookTest } from "../../shared/types";

// ------------------------------------------------------------------- jobs
const JOB_STATUS: Record<JobStatus, Status> = {
  queued: { label: "Queued", color: "var(--axis)" },
  running: { label: "Running", color: "var(--series-1)" },
  succeeded: { label: "Succeeded", color: "var(--good)" },
  failed: { label: "Failed", color: "var(--critical)" },
  cancelled: { label: "Cancelled", color: "var(--warning)" },
};

/** The label and color of a status, also one this build does not know. */
export function jobStatus(status: string): Status {
  return statusOf(JOB_STATUS, status, "var(--muted-foreground)");
}

export const isActive = (job: Pick<Job, "status">): boolean => job.status === "queued" || job.status === "running";

/** Jobs are polled fast while one is queued or running, slowly otherwise (finished rows rarely change). */
export const POLL_ACTIVE_MS = 2000;
export const POLL_IDLE_MS = 15000;

export const pollInterval = (jobs: Pick<Job, "status">[] | null): number => (jobs?.some(isActive) ? POLL_ACTIVE_MS : POLL_IDLE_MS);

/** The latest jobs the Jobs section lists; the menu counts the active ones among the same list (one request). */
export const JOBS_LIMIT = 50;

/** Newest first, by when they were submitted; equal times by id, so the order is stable. */
export function sortJobs(jobs: Job[]): Job[] {
  return [...jobs].sort((a, b) => b.created - a.created || a.id.localeCompare(b.id));
}

/**
 * The bar's fill, 0..1; null while a running job has not reported progress
 * (an indeterminate bar). A job that succeeded without reporting is full.
 */
export function progressValue(job: Pick<Job, "status" | "progress">): number | null {
  if (job.progress != null) return Math.min(1, Math.max(0, job.progress));
  if (job.status === "running") return null;
  return job.status === "succeeded" ? 1 : 0;
}

/** How long a job took, or has taken so far when it runs; null before it starts. */
export function jobDuration(job: Pick<Job, "status" | "started" | "finished">, now = Date.now() / 1000): number | null {
  if (job.started == null) return null;
  if (job.finished != null) return Math.max(0, job.finished - job.started);
  if (job.status === "running") return Math.max(0, now - job.started);
  return null;
}

/** Seconds as "12 s" under a minute, else fmtDuration ("3 min", "1.5 h"). */
export const fmtSeconds = (s: number): string => (s < 60 ? `${Math.round(s)} s` : fmtDuration(s));

/** The name of the schedule that started a job, when one did and it still exists; its id otherwise. */
export function scheduleName(job: Pick<Job, "schedule_id">, schedules: Pick<Schedule, "id" | "name">[]): string | null {
  if (!job.schedule_id) return null;
  return schedules.find((s) => s.id === job.schedule_id)?.name ?? job.schedule_id;
}

/** Schedules by name, so the table does not reorder as they run. */
export function sortSchedules(schedules: Schedule[]): Schedule[] {
  return [...schedules].sort((a, b) => a.name.localeCompare(b.name) || a.id.localeCompare(b.id));
}

/** The server's detail when jobs are off (every /jobs route answers 503 with it). */
export const jobsOff = (message: string): boolean => /^jobs are off/i.test(message.trim());

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

/** The field names of a cron expression and the values each allows (day-of-week takes 7 as Sunday too). */
const CRON_FIELDS: [name: string, min: number, max: number][] = [
  ["minute", 0, 59],
  ["hour", 0, 23],
  ["day-of-month", 1, 31],
  ["month", 1, 12],
  ["day-of-week", 0, 7],
];

const CRON_HINT = "minute hour day-of-month month day-of-week, e.g. 0 3 * * * is daily at 03:00";

/**
 * What is wrong with a cron expression, or null when it is fine: 5 fields
 * separated by spaces, each ``*``, a number, a range ``a-b`` or a list of
 * those, any of them followed by ``/step``.
 */
export function validateCron(text: string): string | null {
  const fields = text.trim().split(/\s+/).filter(Boolean);
  if (fields.length !== 5) return `A cron expression has 5 fields (${CRON_HINT}), not ${fields.length}`;
  for (const [i, field] of fields.entries()) {
    const [name, min, max] = CRON_FIELDS[i];
    for (const item of field.split(",")) {
      const m = /^(\*|(\d+)(?:-(\d+))?)(?:\/(\d+))?$/.exec(item);
      if (!m) return `The ${name} field "${field}" is not *, a number, a range a-b or a list of those, optionally with /step`;
      const [, base, lo, hi, step] = m;
      if (base !== "*") {
        const a = Number(lo);
        const b = hi === undefined ? a : Number(hi);
        if (a < min || a > max || b < min || b > max) return `The ${name} field takes ${min} to ${max}, not "${item}"`;
        if (b < a) return `The ${name} range "${item}" runs backwards`;
      }
      if (step !== undefined && Number(step) < 1) return `The ${name} step in "${item}" must be at least 1`;
    }
  }
  return null;
}

// ---------------------------------------------------------------- retries
/** The most retries a job takes, and the longest wait before one (the server's MAX_RETRIES, MAX_RETRY_DELAY_S). */
export const MAX_RETRIES = 10;
export const MAX_RETRY_DELAY_S = 3600;

/** A job's retries and timeout as the dialogs edit them: text, so each is a plain input. */
export interface RetryForm {
  retries: string;
  retry_delay_s: string;
  /** Minutes; empty for no limit */
  timeout_min: string;
}

export const EMPTY_RETRY_FORM: RetryForm = { retries: "0", retry_delay_s: "30", timeout_min: "" };

/** The form prefilled from a schedule's (or a job's) settings. */
export function retryFormOf(s: Pick<Schedule, "retries" | "retry_delay_s" | "timeout_s">): RetryForm {
  return {
    retries: String(s.retries),
    retry_delay_s: String(s.retry_delay_s),
    timeout_min: s.timeout_s == null ? "" : String(Math.round((s.timeout_s / 60) * 100) / 100),
  };
}

/** Everything wrong with the retry fields; empty when they can be sent. */
export function validateRetryForm(form: RetryForm): string[] {
  const errors: string[] = [];
  const retries = Number(form.retries.trim());
  if (!form.retries.trim() || !Number.isInteger(retries) || retries < 0 || retries > MAX_RETRIES) errors.push(`Retries takes a whole number from 0 to ${MAX_RETRIES}`);
  const delay = Number(form.retry_delay_s.trim());
  if (!form.retry_delay_s.trim() || !(delay >= 1 && delay <= MAX_RETRY_DELAY_S)) errors.push(`The first retry waits 1 to ${MAX_RETRY_DELAY_S} seconds`);
  const timeout = Number(form.timeout_min.trim());
  if (form.timeout_min.trim() && !(timeout * 60 >= 1 && Number.isFinite(timeout))) errors.push("The timeout is a number of minutes, or empty for none");
  return errors;
}

/** The retry fields of POST /jobs and of a schedule's body: check them with ``validateRetryForm`` first. */
export function retryBody(form: RetryForm): Pick<ScheduleBody, "retries" | "retry_delay_s" | "timeout_s"> {
  return {
    retries: Number(form.retries.trim()),
    retry_delay_s: Number(form.retry_delay_s.trim()),
    timeout_s: form.timeout_min.trim() ? Math.round(Number(form.timeout_min.trim()) * 60) : null,
  };
}

/** A job's retries in a few words, for its row: "retry 1 of 3", or "3 retries" before any; null without any. */
export function fmtRetries(job: Pick<Job, "retries" | "retried">): string | null {
  if (!job.retries) return null;
  return job.retried ? `retry ${job.retried} of ${job.retries}` : `${job.retries} ${job.retries === 1 ? "retry" : "retries"}`;
}

// ------------------------------------------------------------------ forms
/** The priorities a job, schedule or eval run can be given, for a select. */
export const PRIORITY_OPTIONS: { value: Priority; label: string }[] = [
  { value: "high", label: "High" },
  { value: "normal", label: "Normal" },
  { value: "low", label: "Low" },
];

/** The schedule dialog's state: every field but the params as text, so each control is a plain input. */
export interface ScheduleForm extends RetryForm {
  name: string;
  job: string;
  /** The job's params, as its form (made from the job's params schema) edits them */
  params: Record<string, unknown>;
  cadence: "cron" | "every";
  cron: string;
  every: string;
  unit: IntervalUnit;
  webhook_url: string;
  priority: Priority;
  enabled: boolean;
}

export const EMPTY_SCHEDULE_FORM: ScheduleForm = {
  name: "",
  job: "",
  params: {},
  cadence: "cron",
  cron: "0 3 * * *",
  every: "1",
  unit: "hours",
  webhook_url: "",
  priority: "normal",
  enabled: true,
  ...EMPTY_RETRY_FORM,
};

/** The form prefilled from a schedule, to edit it. */
export function formFromSchedule(s: Schedule): ScheduleForm {
  const every = s.interval_s ? intervalParts(s.interval_s) : null;
  return {
    name: s.name,
    job: s.job,
    params: s.params ?? {},
    cadence: s.cron ? "cron" : every ? "every" : EMPTY_SCHEDULE_FORM.cadence,
    cron: s.cron ?? EMPTY_SCHEDULE_FORM.cron,
    every: every ? String(every.amount) : EMPTY_SCHEDULE_FORM.every,
    unit: every?.unit ?? EMPTY_SCHEDULE_FORM.unit,
    webhook_url: s.webhook_url ?? "",
    priority: s.priority,
    enabled: s.enabled,
    ...retryFormOf(s),
  };
}

/** Everything wrong with the form but the params (their own form checks them), in the order of its fields; empty when it can be submitted. */
export function validateScheduleForm(form: ScheduleForm): string[] {
  const errors: string[] = [];
  if (!form.name.trim()) errors.push("The schedule needs a name");
  if (!form.job) errors.push("Pick a job");
  if (form.cadence === "cron") {
    const problem = validateCron(form.cron);
    if (problem) errors.push(problem);
  } else if (intervalSeconds(form.every, form.unit) === null) errors.push("Every takes a whole number of minutes, hours or days, at least 1");
  if (form.webhook_url.trim() && !/^https?:\/\//.test(form.webhook_url.trim())) errors.push("The webhook URL starts with https:// or http://");
  errors.push(...validateRetryForm(form));
  return errors;
}

/** The body of POST or PUT /jobs/schedules: check it with ``validateScheduleForm`` first. */
export function scheduleBody(form: ScheduleForm): ScheduleBody {
  return {
    name: form.name.trim(),
    job: form.job,
    params: form.params,
    cron: form.cadence === "cron" ? form.cron.trim().split(/\s+/).join(" ") : null,
    interval_s: form.cadence === "every" ? intervalSeconds(form.every, form.unit) : null,
    enabled: form.enabled,
    webhook_url: form.webhook_url.trim() || null,
    priority: form.priority,
    ...retryBody(form),
  };
}

/** The body that keeps a schedule as it is but for ``enabled``: what the Switch sends. */
export function toggledBody(s: Schedule, enabled: boolean): ScheduleBody {
  const { name, job, params, cron, interval_s, webhook_url, priority, retries, retry_delay_s, timeout_s } = s;
  return { name, job, params, cron, interval_s, enabled, webhook_url, priority, retries, retry_delay_s, timeout_s };
}

// --------------------------------------------------------------- webhooks
/** The outcome of POST /jobs/webhooks/test as a notice. */
export function fmtWebhookTest(result: WebhookTest): { text: string; kind: "ok" | "error" } {
  if (result.ok) return { text: result.status ? `Delivered: the URL answered HTTP ${result.status}.` : "Delivered.", kind: "ok" };
  const status = result.status ? `HTTP ${result.status}` : "";
  const error = result.error ?? "";
  const why = [status, error].filter(Boolean).join(": ");
  return { text: why ? `Not delivered (${why}).` : "Not delivered.", kind: "error" };
}

/**
 * A delivery's tries so far, and when the next goes while one waits: "2 tries · next in 4 min". ``retried``
 * counts the retries queued, so a queued delivery has had that many tries and a finished one one more.
 */
export function fmtDeliveryTries(d: Pick<WebhookDelivery, "status" | "retried" | "not_before">, now = Date.now() / 1000): string {
  if (d.status === "running") return "sending";
  const tries = d.retried + (d.status === "succeeded" || d.status === "failed" ? 1 : 0);
  const counted = `${tries} ${tries === 1 ? "try" : "tries"}`;
  if (d.status !== "queued") return counted;
  if (!tries) return "waiting";
  return d.not_before && d.not_before > now ? `${counted} · next ${fmtUntil(d.not_before, now)}` : `${counted} · next now`;
}

/** A future Unix time as "in 5 min" or "in 2.0 h"; "now" once it has passed. */
export function fmtUntil(ts: number, now = Date.now() / 1000): string {
  const s = ts - now;
  if (s < 30) return "now";
  if (s < 3600) return `in ${Math.round(s / 60)} min`;
  if (s < 86400) return `in ${(s / 3600).toFixed(1)} h`;
  return `in ${(s / 86400).toFixed(1)} d`;
}
