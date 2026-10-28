import { describe, expect, it } from "vitest";

import { fmtBytes, statusOf } from "./format";

describe("fmtBytes", () => {
  it("picks the nearest unit", () => {
    expect(fmtBytes(0)).toBe("0 B");
    expect(fmtBytes(512)).toBe("512 B");
    expect(fmtBytes(65536)).toBe("64 KB");
    expect(fmtBytes(327680)).toBe("320 KB");
    expect(fmtBytes(1.5 * 1024 * 1024)).toBe("1.5 MB");
    expect(fmtBytes(2.3 * 1024 ** 3)).toBe("2.3 GB");
    expect(fmtBytes(null)).toBe("–");
  });
});

describe("statusOf", () => {
  it("falls back to the key itself, in the fallback color", () => {
    const statuses = { ok: { label: "OK", color: "var(--good)" } };
    expect(statusOf(statuses, "ok")).toEqual({ label: "OK", color: "var(--good)" });
    expect(statusOf(statuses, "new")).toEqual({ label: "new", color: "var(--axis)" });
    expect(statusOf(statuses, "new", "red")).toEqual({ label: "new", color: "red" });
  });
});

