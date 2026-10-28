import { describe, expect, it } from "vitest";
import {
  compareHref,
  evalsFromHash,
  evalsHref,
  fmtMean,
  fmtScore,
  inputPreview,
  metricLabel,
  outputPreview,
  parseObject,
  parseReference,
  runProgress,
  scoreTone,
  textOf,
} from "./evals";
import type { Run } from "../shared/types";

describe("hashes", () => {
  it("reads the evals location", () => {
    expect(evalsFromHash("#/evals")).toEqual({ dataset: null, run: null, compare: null });
    expect(evalsFromHash("#/evals/d1")).toEqual({ dataset: "d1", run: null, compare: null });
    expect(evalsFromHash("#/evals/d1/runs/r1")).toEqual({ dataset: "d1", run: "r1", compare: null });
    expect(evalsFromHash("#/evals/d1/compare/r1/r2")).toEqual({ dataset: "d1", run: null, compare: ["r1", "r2"] });
    expect(evalsFromHash("#/evals/d1/compare/r1")).toEqual({ dataset: "d1", run: null, compare: null });
    expect(evalsFromHash("#/traces/x")).toEqual({ dataset: null, run: null, compare: null });
  });

  it("writes them back", () => {
    expect(evalsHref()).toBe("#/evals");
    expect(evalsFromHash(evalsHref("d 1", "r/1"))).toEqual({ dataset: "d 1", run: "r/1", compare: null });
    expect(evalsFromHash(compareHref("d", "a", "b")).compare).toEqual(["a", "b"]);
  });
});

describe("previews", () => {
  it("find the prompt and the answer", () => {
    expect(inputPreview({ messages: [{ role: "system", content: "be brief" }, { role: "user", content: "hello" }] })).toBe("hello");
    expect(inputPreview({ text: "a prompt", temperature: 0.7 })).toBe("a prompt");
    expect(inputPreview({ texts: ["a", "b"], embedding: [1, 2] })).toBe("a b");
    expect(textOf({ $media: { kind: "bytes" } })).toBe("");
    expect(outputPreview({ text: "the answer", tool_calls: null })).toBe("the answer");
    expect(outputPreview({ results: [1] })).toBe('{"results":[1]}');
    expect(outputPreview(null)).toBe("");
    expect(inputPreview({ text: "x".repeat(500) }, 10)).toHaveLength(10);
  });
});

describe("scores", () => {
  it("read as shares, judge marks or verdicts", () => {
    expect(fmtScore(null, "exact_match")).toBe("–");
    expect(fmtScore(0.666, "exact_match")).toBe("67%");
    expect(fmtScore(0.75, "judge", "pass")).toBe("75%");
    expect(fmtScore(1, "judge_vs_abc", "win")).toBe("win");
    expect(fmtMean({ count: 3, scored: 2, mean: 0.75 }, "judge")).toBe("75%");
    expect(fmtMean({ count: 3, scored: 3, mean: 0.5, wins: 1, ties: 1, losses: 1 }, "judge_vs_x")).toBe("1W 1T 1L");
    expect(fmtMean({ count: 3, scored: 3, mean: 0.5 }, "contains")).toBe("50%");
    expect(fmtMean(undefined, "contains")).toBe("–");
    expect([scoreTone(0.9), scoreTone(0.6), scoreTone(0.1), scoreTone(null)]).toEqual(["good", "warning", "critical", null]);
    expect(metricLabel("exact_match")).toBe("exact match");
    expect(metricLabel("judge_vs_r2", [{ id: "r2", name: "wordy" }])).toBe("vs wordy");
    expect(metricLabel("judge_vs_abcdefghijk")).toBe("vs abcdefgh");
  });

  it("describes what a run is doing", () => {
    const run = { status: "done", activity: null } as unknown as Run;
    expect(runProgress(run)).toBe("Done");
    expect(runProgress({ ...run, status: "running", activity: { kind: "running", done: 3, total: 10 } })).toBe("Running 3/10");
    expect(runProgress({ ...run, activity: { kind: "judging", done: 2, total: 10 } })).toBe("Judging 2/10");
    expect(runProgress({ ...run, activity: { kind: "judging", error: "boom" } })).toBe("Judging failed: boom");
    expect(runProgress({ ...run, status: "queued", activity: { kind: "queued" } })).toBe("Queued");
  });
});

describe("forms", () => {
  it("take JSON objects and references", () => {
    expect(parseObject('{"text": "hi"}')).toEqual({ ok: true, value: { text: "hi" } });
    expect(parseObject("")).toEqual({ ok: false, error: "The input is required" });
    expect(parseObject("[1]").ok).toBe(false);
    expect(parseObject("{oops", "Params")).toMatchObject({ ok: false, error: expect.stringContaining("Params is not valid JSON") });
    expect(parseReference("")).toBeNull();
    expect(parseReference("Paris")).toBe("Paris");
    expect(parseReference('{"text": "Paris"}')).toEqual({ text: "Paris" });
    expect(parseReference("{not json")).toBe("{not json");
  });
});
