import { describe, expect, it } from "vitest";
import {
  describeCadence,
  intervalParts,
  intervalSeconds,
  isActive,
  POLL_ACTIVE_MS,
  POLL_IDLE_MS,
  pollInterval,
  PRIORITY_OPTIONS,
  progressValue,
  taskStatus,
} from "./tasks";

describe("a task's status", () => {
  it("polls fast only while a task is queued or running", () => {
    expect(pollInterval(null)).toBe(POLL_IDLE_MS);
    expect(pollInterval([{ status: "succeeded" }, { status: "failed" }])).toBe(POLL_IDLE_MS);
    expect(pollInterval([{ status: "succeeded" }, { status: "queued" }])).toBe(POLL_ACTIVE_MS);
    expect(pollInterval([{ status: "running" }])).toBe(POLL_ACTIVE_MS);
    expect(isActive({ status: "cancelled" })).toBe(false);
  });

  it("fills the bar from progress, indeterminate only while running blind", () => {
    expect(progressValue({ status: "running", progress: 0.4 })).toBe(0.4);
    expect(progressValue({ status: "running", progress: null })).toBeNull();
    expect(progressValue({ status: "running", progress: 1.7 })).toBe(1);
    expect(progressValue({ status: "queued", progress: null })).toBe(0);
    expect(progressValue({ status: "succeeded", progress: null })).toBe(1);
    expect(progressValue({ status: "failed", progress: 0.5 })).toBe(0.5);
    expect(progressValue({ status: "failed", progress: null })).toBe(0);
  });

  it("labels every status, unknown ones as themselves", () => {
    expect(taskStatus("succeeded").label).toBe("Succeeded");
    expect(taskStatus("weird").label).toBe("weird");
  });

  it("offers the priorities, most urgent first", () => {
    expect(PRIORITY_OPTIONS.map((o) => o.value)).toEqual(["high", "normal", "low"]);
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
});
