import { describe, expect, it } from "vitest";
import { requestsPerBucket, sparkline } from "./menu-status";

describe("requestsPerBucket", () => {
  it("counts failed requests too", () => {
    const timeline = [
      { start: 0, ok: 3, error: 1, client_error: 2, p50_ms: 12, p95_ms: 40 },
      { start: 60, ok: 0, error: 0, client_error: 0, p50_ms: null, p95_ms: null },
    ];
    expect(requestsPerBucket({ timeline })).toEqual([6, 0]);
  });
});

describe("sparkline", () => {
  it("spans the box: the largest value at the top, 0 at the bottom", () => {
    expect(sparkline([0, 4, 2], 40, 14)).toEqual({
      line: "M0.0 13.0 L20.0 1.0 L40.0 7.0",
      area: "M0.0 13.0 L20.0 1.0 L40.0 7.0 L40 14 L0 14 Z",
    });
  });

  it("draws nothing without traffic or with one bucket", () => {
    expect(sparkline([0, 0, 0], 40, 14)).toBeNull();
    expect(sparkline([5], 40, 14)).toBeNull();
    expect(sparkline([], 40, 14)).toBeNull();
  });
});
