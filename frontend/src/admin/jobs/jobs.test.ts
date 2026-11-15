import { describe, expect, it } from "vitest";
import type { Job, TaskStatus, Schedule } from "../../shared/types";
import {
  envsToSetUp,
  resultFacts,
  trainedModelHref,
  EMPTY_SCHEDULE_FORM,
  fmtDeliveryTries,
  fmtUntil,
  fmtWebhookTest,
  formFromSchedule,
  jobDuration,
  scheduleBody,
  scheduleName,
  sortJobs,
  sortSchedules,
  toggledBody,
  validateRetryForm,
  validateScheduleForm,
  fmtRetries,
  retryBody,
  retryFormOf,
} from "./jobs";

const job = (over: Partial<Job> = {}): Job => ({
  id: "j1",
  kind: "job",
  ref: null,
  job: "train",
  model_id: null,
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
  retries: 0,
  retry_delay_s: 30,
  timeout_s: null,
  retried: 0,
  not_before: null,
  ...over,
});

const schedule = (over: Partial<Schedule> = {}): Schedule => ({
  id: "s1",
  kind: "job",
  name: "nightly",
  job: "train",
  params: { data: "x.csv" },
  cron: "0 3 * * *",
  interval_s: null,
  enabled: true,
  webhook_url: null,
  priority: "normal",
  retries: 0,
  retry_delay_s: 30,
  timeout_s: null,
  created: 1,
  updated: 1,
  next_run: null,
  last_run: null,
  last_task_id: null,
  last_status: null,
  ...over,
});

describe("job rows", () => {
  it("sorts newest first, ties by id", () => {
    const rows = [job({ id: "b", created: 1 }), job({ id: "c", created: 3 }), job({ id: "a", created: 1 })];
    expect(sortJobs(rows).map((j) => j.id)).toEqual(["c", "a", "b"]);
    expect(rows[0].id).toBe("b");
  });

  it("measures the duration to the end, or to now while running", () => {
    expect(jobDuration(job())).toBe(60);
    expect(jobDuration(job({ status: "running", finished: null }), 140)).toBe(30);
    expect(jobDuration(job({ status: "queued", started: null, finished: null }))).toBeNull();
  });

  it("orders schedules by name", () => {
    expect(sortSchedules([schedule({ name: "b" }), schedule({ name: "a", id: "s2" })]).map((s) => s.name)).toEqual(["a", "b"]);
  });

  it("names the schedule that started a job", () => {
    expect(scheduleName(job(), [schedule()])).toBeNull();
    expect(scheduleName(job({ schedule_id: "s1" }), [schedule()])).toBe("nightly");
    expect(scheduleName(job({ schedule_id: "gone" }), [schedule()])).toBe("gone");
  });
});

describe("cadence", () => {
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
      retries: 0,
      retry_delay_s: 30,
      timeout_s: null,
    });
  });

  it("round-trips an interval schedule", () => {
    const form = formFromSchedule(schedule({ cron: null, interval_s: 5400, enabled: false, priority: "low" }));
    expect(form).toMatchObject({ cadence: "every", every: "90", unit: "minutes", enabled: false, priority: "low" });
    expect(scheduleBody(form)).toMatchObject({ cron: null, interval_s: 5400, enabled: false, webhook_url: null, priority: "low" });
  });

  it("lists what is missing", () => {
    expect(validateScheduleForm(EMPTY_SCHEDULE_FORM)).toEqual(["The schedule needs a name", "Pick a job"]);
    // A cron expression is the server's to take or refuse; only an empty one is held back here.
    const errors = validateScheduleForm({ ...EMPTY_SCHEDULE_FORM, name: "x", job: "train", cron: " ", webhook_url: "ftp://x" });
    expect(errors).toEqual(["Give a cron expression", "The webhook URL starts with https:// or http://"]);
    expect(validateScheduleForm({ ...EMPTY_SCHEDULE_FORM, name: "x", job: "train", cron: "* * *" })).toEqual([]);
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
      retries: 0,
      retry_delay_s: 30,
      timeout_s: null,
    });
  });
});

describe("retries and timeout", () => {
  it("round-trip through the form, the timeout in minutes", () => {
    const form = retryFormOf({ retries: 3, retry_delay_s: 60, timeout_s: 5400 });
    expect(form).toEqual({ retries: "3", retry_delay_s: "60", timeout_min: "90" });
    expect(validateRetryForm(form)).toEqual([]);
    expect(retryBody(form)).toEqual({ retries: 3, retry_delay_s: 60, timeout_s: 5400 });
    expect(retryBody({ ...form, timeout_min: " " })).toMatchObject({ timeout_s: null });
    expect(retryBody({ ...form, timeout_min: "0.5" })).toMatchObject({ timeout_s: 30 });
  });

  it("lists what is not a number, leaving the ranges to the server", () => {
    expect(validateRetryForm({ retries: "11", retry_delay_s: "7200", timeout_min: "" })).toEqual([]);
    expect(validateRetryForm({ retries: "x", retry_delay_s: "", timeout_min: "-1" })).toHaveLength(3);
    expect(validateRetryForm({ retries: "1.5", retry_delay_s: "30", timeout_min: "" })).toEqual(["Retries takes a whole number"]);
    expect(validateScheduleForm({ ...EMPTY_SCHEDULE_FORM, name: "x", job: "train", retries: "" })).toEqual(["Retries takes a whole number"]);
  });

  it("are shown on a job's row once it has any", () => {
    expect(fmtRetries(job())).toBeNull();
    expect(fmtRetries(job({ retries: 3 }))).toBe("3 retries");
    expect(fmtRetries(job({ retries: 1 }))).toBe("1 retry");
    expect(fmtRetries(job({ retries: 3, retried: 2 }))).toBe("retry 2 of 3");
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

describe("webhook deliveries", () => {
  const d = (status: TaskStatus, retried = 0, not_before: number | null = null) => ({ status, retried, not_before });
  it("count the tries and say when the next goes", () => {
    expect(fmtDeliveryTries(d("queued"), 1000)).toBe("waiting");
    expect(fmtDeliveryTries(d("running", 1), 1000)).toBe("sending");
    expect(fmtDeliveryTries(d("succeeded"), 1000)).toBe("1 try");
    expect(fmtDeliveryTries(d("succeeded", 2), 1000)).toBe("3 tries");
    expect(fmtDeliveryTries(d("queued", 2, 1300), 1000)).toBe("2 tries · next in 5 min");
    expect(fmtDeliveryTries(d("queued", 1, 900), 1000)).toBe("1 try · next now");
    expect(fmtDeliveryTries(d("failed", 10), 1000)).toBe("11 tries");
    expect(fmtDeliveryTries(d("cancelled", 1), 1000)).toBe("1 try");
  });
});

describe("what a job made", () => {
  const done = (result: unknown, status: Job["status"] = "succeeded") => ({ id: "ab12cd34", status, result });

  it("opens the model it trained: a predictor on its page, a classifier in the Models section", () => {
    expect(trainedModelHref(done({ model_id: "autogluon:ab12cd34", task: "tabular-prediction" }))).toBe("#/autogluon/ab12cd34");
    expect(trainedModelHref(done({ model_id: "trained:ab12cd34", task: "text-classification" }))).toBe("#/models?model=trained%3Aab12cd34&task=text-classification");
    expect(trainedModelHref(done({ file: "predictions.csv", rows: 3 }))).toBeNull();
    expect(trainedModelHref(done({ model_id: "trained:x", task: "t" }, "failed"))).toBeNull();
  });

  it("lists a result's plain fields", () => {
    expect(resultFacts({ model_id: "m", test_score: 0.987512, rows: 400, leaderboard: [{}], lower_is_better: false, freq: null })).toEqual([
      ["model_id", "m"], ["test_score", "0.9875"], ["rows", "400"], ["lower_is_better", "false"], ["freq", "–"],
    ]);
    expect(resultFacts(null)).toEqual([]);
  });

  it("names each environment to set up once, and whether it is an update", () => {
    const types = [
      { env: "autogluon", env_ready: false, env_current: false },
      { env: "autogluon", env_ready: false, env_current: false },
      { env: "default", env_ready: true, env_current: true },
      { env: "other", env_ready: true, env_current: false },
      { env: "fine", env_ready: true, env_current: true },
    ];
    expect(envsToSetUp(types)).toEqual([{ env: "autogluon", update: false }, { env: "other", update: true }]);
  });
});
