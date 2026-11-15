import { describe, expect, it } from "vitest";
import { copiedCell, copyBehind, copyStatus, copying, shortHash, type BucketRemote, uploadReport } from "./buckets";

describe("a bucket's files", () => {
  it("shows the start of a hash", () => {
    expect(shortHash("9b1c4f2e8a7d6c5b4a39281706f5e4d3c2b1a09f8e7d6c5b4a3928170615f4e3")).toBe("9b1c4f2e8a7d");
  });

  it("says how an upload of several files went", () => {
    expect(uploadReport(1, [])).toEqual({ done: "Uploaded the file.", failed: "" });
    expect(uploadReport(3, [])).toEqual({ done: "Uploaded 3 files.", failed: "" });
    expect(uploadReport(3, [["big.iso", "The file is larger than MAX_FILE_BYTES"], ["a\nb", "control characters"]])).toEqual({
      done: "Uploaded 1 of 3 files.",
      failed: "big.iso: The file is larger than MAX_FILE_BYTES; a\nb: control characters",
    });
  });

  const task = (status: NonNullable<BucketRemote["task"]>["status"], more: Partial<NonNullable<BucketRemote["task"]>> = {}) => ({
    id: "t1", kind: "copy", ref: "ws/docs", job: null, model_id: null, status, priority: "normal" as const, params: {},
    progress: null, message: null, result: null, error: null, created: 1, started: null, finished: null, schedule_id: null,
    webhook_url: null, retries: 5, retry_delay_s: 30, timeout_s: null, retried: 0, not_before: null, ...more,
  });

  it("says how its copy to its remote stands", () => {
    expect(copyStatus({ remote: null, waiting: null, task: null }).label).toMatch(/^No remote/);
    expect(copyStatus({ remote: "r2", waiting: 0, task: task("succeeded") })).toEqual({ label: "Every file is copied", color: "var(--good)" });
    expect(copyStatus({ remote: "r2", waiting: 2, task: task("queued") }).label).toBe("2 files wait to be copied");
    expect(copyStatus({ remote: "r2", waiting: 1, task: null }).label).toBe("A file waits to be copied");
    expect(copyStatus({ remote: "r2", waiting: 3, task: task("running", { message: "copied 1 of 3" }) }).label).toBe("Copying: copied 1 of 3");
    expect(copyStatus({ remote: "r2", waiting: 3, task: task("failed", { error: "The remote r2 could not write" }) })).toEqual({
      label: "Copying failed: The remote r2 could not write",
      color: "var(--critical)",
    });
    expect(copyStatus({ remote: "r2", waiting: 3, task: task("queued", { retried: 1, error: "refused" }) }).label).toBe("Retry 1 of 5 waits: refused");
    expect(copying({ task: task("running") })).toBe(true);
    expect(copying({ task: task("failed") })).toBe(false);
    expect(copying(undefined)).toBe(false);
  });

  it("offers Copy now while the copy is behind and none runs", () => {
    expect(copyBehind({ remote: "r2", waiting: 0, task: task("failed") })).toBe(true);
    expect(copyBehind({ remote: "r2", waiting: 0, task: task("cancelled") })).toBe(true);
    expect(copyBehind({ remote: "r2", waiting: 2, task: task("succeeded") })).toBe(true);
    expect(copyBehind({ remote: "r2", waiting: 2, task: null })).toBe(true);
    expect(copyBehind({ remote: "r2", waiting: 2, task: task("queued", { retried: 1 }) })).toBe(true);
    expect(copyBehind({ remote: "r2", waiting: 2, task: task("queued") })).toBe(false);
    expect(copyBehind({ remote: "r2", waiting: 2, task: task("running") })).toBe(false);
    expect(copyBehind({ remote: "r2", waiting: 0, task: task("succeeded") })).toBe(false);
    expect(copyBehind({ remote: null, waiting: null, task: task("failed") })).toBe(false);
  });

  it("says when each file was copied", () => {
    const ago = (ts: number) => `at ${ts}`;
    expect(copiedCell({ copied: 12 }, "r2", ago)).toBe("at 12");
    expect(copiedCell({}, "r2", ago)).toBe("waiting");
    expect(copiedCell({ copied: null }, null, ago)).toBe("–");
  });
});
