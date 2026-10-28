import { describe, expect, it } from "vitest";
import type { Job, Schedule } from "../shared/types";
import {
  describeCadence,
  EMPTY_SCHEDULE_FORM,
  fmtSeconds,
  fmtUntil,
  fmtWebhookTest,
  formFromSchedule,
  intervalParts,
  intervalSeconds,
  isActive,
  jobDuration,
  jobsOff,
  jobStatus,
  POLL_ACTIVE_MS,
  POLL_IDLE_MS,
  pollInterval,
  progressValue,
  scheduleBody,
  scheduleName,
  sortJobs,
  sortSchedules,
  toggledBody,
  validateCron,
  validateScheduleForm,
} from "./jobs";

const job = (over: Partial<Job> = {}): Job => ({
  id: "j1",
  job: "train",
  status: "succeeded",
  params: {},
  progress: 1,
  message: null,
  result: null,
  error: null,
  created: 100,
  started: 110,
  finished: 170,
  schedule_id: null,
  webhook_url: null,
  priority: "normal",
  ...over,
});

const schedule = (over: Partial<Schedule> = {}): Schedule => ({
  id: "s1",
  name: "nightly",
  job: "train",
  params: { data: "x.csv" },
  cron: "0 3 * * *",
  interval_s: null,
  enabled: true,
  webhook_url: null,
  priority: "normal",
  created: 1,
  updated: 1,
  next_run: null,
  last_run: null,
  last_job_id: null,
  last_status: null,
  ...over,
});

describe("job rows", () => {
  it("sorts newest first, ties by id", () => {
    const rows = [job({ id: "b", created: 1 }), job({ id: "c", created: 3 }), job({ id: "a", created: 1 })];
    expect(sortJobs(rows).map((j) => j.id)).toEqual(["c", "a", "b"]);
    expect(rows[0].id).toBe("b");
  });

  it("polls fast only while a job is queued or running", () => {
    expect(pollInterval(null)).toBe(POLL_IDLE_MS);
    expect(pollInterval([job(), job({ status: "failed" })])).toBe(POLL_IDLE_MS);
    expect(pollInterval([job(), job({ status: "queued" })])).toBe(POLL_ACTIVE_MS);
    expect(pollInterval([job({ status: "running" })])).toBe(POLL_ACTIVE_MS);
    expect(isActive(job({ status: "cancelled" }))).toBe(false);
  });

  it("measures the duration to the end, or to now while running", () => {
    expect(jobDuration(job())).toBe(60);
    expect(jobDuration(job({ status: "running", finished: null }), 140)).toBe(30);
    expect(jobDuration(job({ status: "queued", started: null, finished: null }))).toBeNull();
    expect(fmtSeconds(12.4)).toBe("12 s");
    expect(fmtSeconds(180)).toBe("3 min");
  });

  it("fills the bar from progress, indeterminate only while running blind", () => {
    expect(progressValue(job({ status: "running", progress: 0.4 }))).toBe(0.4);
    expect(progressValue(job({ status: "running", progress: null }))).toBeNull();
    expect(progressValue(job({ status: "running", progress: 1.7 }))).toBe(1);
    expect(progressValue(job({ status: "queued", progress: null }))).toBe(0);
    expect(progressValue(job({ status: "succeeded", progress: null }))).toBe(1);
    expect(progressValue(job({ status: "failed", progress: 0.5 }))).toBe(0.5);
    expect(progressValue(job({ status: "failed", progress: null }))).toBe(0);
  });

  it("orders schedules by name", () => {
    expect(sortSchedules([schedule({ name: "b" }), schedule({ name: "a", id: "s2" })]).map((s) => s.name)).toEqual(["a", "b"]);
  });

  it("names the schedule that started a job", () => {
    expect(scheduleName(job(), [schedule()])).toBeNull();
    expect(scheduleName(job({ schedule_id: "s1" }), [schedule()])).toBe("nightly");
    expect(scheduleName(job({ schedule_id: "gone" }), [schedule()])).toBe("gone");
  });

  it("labels every status, unknown ones as themselves", () => {
    expect(jobStatus("succeeded").label).toBe("Succeeded");
    expect(jobStatus("weird").label).toBe("weird");
  });

  it("recognizes the server's jobs-off detail", () => {
    expect(jobsOff("Jobs are off: JOBS_DIR or ARCADEDB_PATH is empty")).toBe(true);
    expect(jobsOff("HTTP 500")).toBe(false);
  });
});

describe("cadence", () => {
  it("converts an interval to seconds and back", () => {
    expect(intervalSeconds("2", "hours")).toBe(7200);
    expect(intervalSeconds(3, "days")).toBe(259200);
    expect(intervalSeconds("", "minutes")).toBeNull();
    expect(intervalSeconds("0", "minutes")).toBeNull();
    expect(intervalSeconds("1.5", "hours")).toBeNull();
    expect(intervalParts(7200)).toEqual({ amount: 2, unit: "hours" });
    expect(intervalParts(86400 * 3)).toEqual({ amount: 3, unit: "days" });
    expect(intervalParts(90)).toEqual({ amount: 1.5, unit: "minutes" });
  });

  it("describes a schedule's cadence", () => {
    expect(describeCadence({ cron: " 0 3 * * * ", interval_s: null })).toBe("0 3 * * *");
    expect(describeCadence({ cron: null, interval_s: 7200 })).toBe("every 2 h");
    expect(describeCadence({ cron: null, interval_s: 900 })).toBe("every 15 min");
    expect(describeCadence({ cron: null, interval_s: 86400 })).toBe("every 1 d");
    expect(describeCadence({ cron: null, interval_s: 45 })).toBe("every 45 s");
    expect(describeCadence({ cron: null, interval_s: null })).toBe("–");
  });

  it("accepts the usual cron expressions", () => {
    for (const ok of ["0 3 * * *", "*/15 * * * *", "0 9-17 * * 1-5", "30 2 1,15 * *", "0 0 * * 0,7", "5-50/10 * * * *", "  0  0 1 1 *  "]) {
      expect(validateCron(ok)).toBeNull();
    }
  });

  it("rejects malformed and out-of-range expressions, naming the field", () => {
    expect(validateCron("0 3 * *")).toMatch(/5 fields/);
    expect(validateCron("0 3 * * * *")).toMatch(/5 fields/);
    expect(validateCron("")).toMatch(/5 fields/);
    expect(validateCron("60 * * * *")).toMatch(/minute field takes 0 to 59/);
    expect(validateCron("0 24 * * *")).toMatch(/hour field/);
    expect(validateCron("0 0 0 * *")).toMatch(/day-of-month field/);
    expect(validateCron("0 0 * 13 *")).toMatch(/month field/);
    expect(validateCron("0 0 * * 8")).toMatch(/day-of-week field/);
    expect(validateCron("10-5 * * * *")).toMatch(/backwards/);
    expect(validateCron("*/0 * * * *")).toMatch(/step/);
    expect(validateCron("a * * * *")).toMatch(/minute field "a"/);
    expect(validateCron("0 3 * * MON")).toMatch(/day-of-week/);
  });

  it("says when the next run is", () => {
    expect(fmtUntil(1000, 1000)).toBe("now");
    expect(fmtUntil(1000, 1010)).toBe("now");
    expect(fmtUntil(1300, 1000)).toBe("in 5 min");
    expect(fmtUntil(1000 + 7200, 1000)).toBe("in 2.0 h");
    expect(fmtUntil(1000 + 86400 * 2, 1000)).toBe("in 2.0 d");
  });
});

describe("the schedule form", () => {
  it("round-trips a cron schedule", () => {
    const form = formFromSchedule(schedule({ webhook_url: "https://x.test/hook" }));
    expect(form).toMatchObject({ name: "nightly", job: "train", cadence: "cron", cron: "0 3 * * *", webhook_url: "https://x.test/hook", enabled: true });
    expect(form.params).toEqual({ data: "x.csv" });
    expect(validateScheduleForm(form)).toEqual([]);
    expect(scheduleBody(form)).toEqual({
      name: "nightly",
      job: "train",
      params: { data: "x.csv" },
      cron: "0 3 * * *",
      interval_s: null,
      enabled: true,
      webhook_url: "https://x.test/hook",
      priority: "normal",
    });
  });

  it("round-trips an interval schedule", () => {
    const form = formFromSchedule(schedule({ cron: null, interval_s: 5400, enabled: false, priority: "low" }));
    expect(form).toMatchObject({ cadence: "every", every: "90", unit: "minutes", enabled: false, priority: "low" });
    expect(scheduleBody(form)).toMatchObject({ cron: null, interval_s: 5400, enabled: false, webhook_url: null, priority: "low" });
  });

  it("lists what is missing", () => {
    expect(validateScheduleForm(EMPTY_SCHEDULE_FORM)).toEqual(["The schedule needs a name", "Pick a job"]);
    const errors = validateScheduleForm({ ...EMPTY_SCHEDULE_FORM, name: "x", job: "train", cron: "* * *", webhook_url: "ftp://x" });
    expect(errors).toHaveLength(2);
    expect(errors[0]).toMatch(/5 fields/);
    expect(errors[1]).toMatch(/webhook URL/);
    expect(validateScheduleForm({ ...EMPTY_SCHEDULE_FORM, name: "x", job: "train", cadence: "every", every: "0" })).toEqual([
      "Every takes a whole number of minutes, hours or days, at least 1",
    ]);
  });

  it("tidies the cron's spacing and trims the name", () => {
    expect(scheduleBody({ ...EMPTY_SCHEDULE_FORM, name: " x ", job: "train", cron: " 0   3 * * * " })).toMatchObject({ name: "x", cron: "0 3 * * *" });
  });

  it("toggles only enabled", () => {
    expect(toggledBody(schedule({ interval_s: null }), false)).toEqual({
      name: "nightly",
      job: "train",
      params: { data: "x.csv" },
      cron: "0 3 * * *",
      interval_s: null,
      enabled: false,
      webhook_url: null,
      priority: "normal",
    });
  });
});

describe("webhook tests", () => {
  it("reports the outcome", () => {
    expect(fmtWebhookTest({ ok: true, status: 200, error: null })).toEqual({ text: "Delivered: the URL answered HTTP 200.", kind: "ok" });
    expect(fmtWebhookTest({ ok: false, status: 500, error: "Internal Server Error" })).toEqual({
      text: "Not delivered (HTTP 500: Internal Server Error).",
      kind: "error",
    });
    expect(fmtWebhookTest({ ok: false, status: null, error: "connection refused" })).toEqual({ text: "Not delivered (connection refused).", kind: "error" });
    expect(fmtWebhookTest({ ok: false, status: null, error: null })).toEqual({ text: "Not delivered.", kind: "error" });
  });
});
