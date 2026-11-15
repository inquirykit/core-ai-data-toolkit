import { describe, expect, it } from "vitest";
import type { Task } from "../shared/types";
import { limitsBody, queueRows } from "./RunningSection";

describe("limitsBody", () => {
  it("turns the fields into the request, or says what is wrong", () => {
    expect(limitsBody("6.5", "4")).toEqual({ max_memory_gb: 6.5, max_models: 4 });
    expect(limitsBody("0", "1")).toEqual({ max_memory_gb: 0, max_models: 1 });
    expect(limitsBody("", "3")).toMatch(/memory budget/);
    expect(limitsBody("-1", "3")).toMatch(/memory budget/);
    expect(limitsBody("8", "2.5")).toMatch(/Max models/);
    expect(limitsBody("8", "0")).toMatch(/Max models/);
  });
});

describe("queueRows", () => {
  const task = (id: string, status: Task["status"]): Task => ({
    id, kind: "copy", ref: "ws/docs", job: null, model_id: null, status, priority: "normal", params: {}, progress: null,
    message: null, result: null, error: null, created: 1, started: null, finished: null, schedule_id: null,
    webhook_url: null, retries: 0, retry_delay_s: 30, timeout_s: null, retried: 0, not_before: null,
  });

  it("puts the work not done yet first, each task once", () => {
    const running = task("r", "running");
    const rows = queueRows([running, task("q", "queued")], [task("new", "succeeded"), running, task("old", "failed")]);
    expect(rows.map((t) => t.id)).toEqual(["r", "q", "new", "old"]);
  });

  it("leaves out a task the newest saw unfinished but the active list no longer holds", () => {
    expect(queueRows([], [task("x", "running"), task("y", "cancelled")]).map((t) => t.id)).toEqual(["y"]);
  });
});
