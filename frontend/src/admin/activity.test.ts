import { describe, expect, it } from "vitest";
import {
  bucketsFor,
  clusterByX,
  filterLogs,
  fmtRange,
  fromRequestRow,
  fromTrace,
  mergeLogs,
  panRange,
  presetOf,
  presetView,
  rangeOf,
  searchesTraces,
  traceFromHash,
  viewOf,
  zoomRange,
} from "./activity";
import type { LogLine, RequestRow, TraceSummary } from "../shared/types";

const NOW = 1_700_000_000;

describe("views and ranges", () => {
  it("a live view ends now and slides with it", () => {
    const view = presetView(3600);
    expect(rangeOf(view, NOW)).toEqual({ since: NOW - 3600, until: NOW });
    expect(rangeOf(view, NOW + 5)).toEqual({ since: NOW + 5 - 3600, until: NOW + 5 });
    expect(presetOf(view)).toBe("1h");
    expect(presetOf({ span: 3600, until: NOW - 100 })).toBeNull();
    expect(presetOf({ span: 1234, until: null })).toBeNull();
  });

  it("a range reaching about now becomes live, one in the past stays put", () => {
    // Within the 20 s tolerance of now.
    expect(viewOf({ since: NOW - 610, until: NOW - 10 }, NOW)).toEqual({ span: 600, until: null });
    expect(viewOf({ since: NOW - 7200, until: NOW - 3600 }, NOW)).toEqual({ span: 3600, until: NOW - 3600 });
    // Never past now, and the span is kept.
    expect(rangeOf(viewOf({ since: NOW - 100, until: NOW + 500 }, NOW), NOW)).toEqual({ since: NOW - 600, until: NOW });
    // Within the span limits: a minute to 90 days.
    expect(viewOf({ since: NOW - 1, until: NOW - 0.5 }, NOW).span).toBe(60);
    expect(viewOf({ since: 0, until: NOW }, NOW).span).toBe(90 * 86400);
  });

  it("zooms about a point", () => {
    const range = { since: NOW - 1000, until: NOW };
    // In, about the middle: the middle stays where it was.
    expect(zoomRange(range, 0.5, 0.5, NOW)).toEqual({ since: NOW - 750, until: NOW - 250 });
    // In, about the end: the end stays.
    expect(zoomRange(range, 0.5, 1, NOW)).toEqual({ since: NOW - 500, until: NOW });
    // Out, about the start, would pass now: pushed back, span kept.
    expect(zoomRange(range, 2, 0, NOW)).toEqual({ since: NOW - 2000, until: NOW });
    // Not tighter than a minute.
    expect(zoomRange(range, 0.001, 0.5, NOW).until - zoomRange(range, 0.001, 0.5, NOW).since).toBe(60);
  });

  it("pans without passing now", () => {
    const range = { since: NOW - 2000, until: NOW - 1000 };
    expect(panRange(range, -500, NOW)).toEqual({ since: NOW - 2500, until: NOW - 1500 });
    expect(panRange(range, 5000, NOW)).toEqual({ since: NOW - 1000, until: NOW });
  });

  it("asks for a few pixels per point", () => {
    expect(bucketsFor(0)).toBe(24);
    expect(bucketsFor(600)).toBe(100);
    expect(bucketsFor(10_000)).toBe(240);
  });
});

describe("time", () => {
  it("the toolbar's range ends with its span, short and readable", () => {
    // The stamps before it depend on the locale; the span does not.
    const span = (s: number) => fmtRange({ since: NOW - s, until: NOW }).split(" · ").pop();
    expect(span(45)).toBe("45 s");
    expect(span(900)).toBe("15 min");
    expect(span(90)).toBe("1.5 min");
    expect(span(5400)).toBe("1.5 h");
    expect(span(86400)).toBe("1 d");
    expect(span(7 * 86400)).toBe("7 d");
  });

});

describe("event clusters", () => {
  it("groups neighbours in order", () => {
    const groups = clusterByX([{ x: 50 }, { x: 10 }, { x: 12 }, { x: 30 }, { x: 56 }], 6);
    expect(groups).toEqual([[{ x: 10 }, { x: 12 }], [{ x: 30 }], [{ x: 50 }, { x: 56 }]]);
    expect(clusterByX([], 6)).toEqual([]);
  });
});

describe("logs", () => {
  const line = (id: number, level: string, message: string, logger = "api"): LogLine => ({
    id,
    ts: NOW + id,
    level,
    levelno: { DEBUG: 10, INFO: 20, WARNING: 30, ERROR: 40 }[level] ?? 0,
    logger,
    message,
  });
  const lines = [line(1, "DEBUG", "probe"), line(2, "INFO", "Using device: mps", "models.manager"), line(3, "ERROR", "Boom")];

  it("filters by level and text", () => {
    expect(filterLogs(lines, "DEBUG", "")).toHaveLength(3);
    expect(filterLogs(lines, "INFO", "").map((l) => l.id)).toEqual([2, 3]);
    expect(filterLogs(lines, "DEBUG", "MPS").map((l) => l.id)).toEqual([2]);
    expect(filterLogs(lines, "DEBUG", "manager").map((l) => l.id)).toEqual([2]);
    expect(filterLogs(lines, "ERROR", "boom").map((l) => l.id)).toEqual([3]);
  });

  it("appends only new lines, within the capacity", () => {
    const merged = mergeLogs(lines.slice(0, 2), lines, 10);
    expect(merged.map((l) => l.id)).toEqual([1, 2, 3]);
    expect(mergeLogs(lines, lines, 10)).toBe(lines);
    expect(mergeLogs(lines, [line(4, "INFO", "x")], 2).map((l) => l.id)).toEqual([3, 4]);
  });
});

describe("the requests table", () => {
  it("reads the open trace from the hash, at the old address too", () => {
    expect(traceFromHash("#/activity/abc123")).toBe("abc123");
    expect(traceFromHash("#/activity/a%2Fb?x=1")).toBe("a/b");
    expect(traceFromHash("#/traces/abc123")).toBe("abc123");
    expect(traceFromHash("#/activity")).toBeNull();
    expect(traceFromHash("#/evals/x")).toBeNull();
  });

  it("searches the traces only when there are words or a tag", () => {
    expect(searchesTraces("", "")).toBe(false);
    expect(searchesTraces("  ", " ")).toBe(false);
    expect(searchesTraces("paris", "")).toBe(true);
    expect(searchesTraces("", "playground")).toBe(true);
  });

  it("makes the same row of a request and of a trace", () => {
    const request: RequestRow = { id: 7, ts: 100, model_id: "m", task: "chat", status: "ok", duration_ms: 12, cold: true, error: null, trace_id: "t1" };
    const trace: TraceSummary = {
      id: "t1", ts: 100, model_id: "m", task: "chat", status: "ok", duration_ms: 12, cold: true, error: null,
      tags: ["playground"], session: null, input_chars: 5, output_chars: 3, preview: "hi → hello",
    };
    expect(fromRequestRow(request)).toEqual({
      key: "r7", ts: 100, model_id: "m", task: "chat", status: "ok", duration_ms: 12, cold: true, error: null, trace_id: "t1", preview: null, tags: [],
    });
    expect(fromRequestRow({ ...request, trace_id: undefined, cold: false })).toMatchObject({ trace_id: null, cold: false });
    expect(fromTrace(trace)).toEqual({
      key: "tt1", ts: 100, model_id: "m", task: "chat", status: "ok", duration_ms: 12, cold: true, error: null, trace_id: "t1",
      preview: "hi → hello", tags: ["playground"],
    });
  });
});
