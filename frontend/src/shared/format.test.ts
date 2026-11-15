import { describe, expect, it } from "vitest";

import { fmtBytes, fmtIso, fmtSeconds, plural, statusOf } from "./format";

describe("fmtSeconds", () => {
  it("says seconds under a minute, and minutes or hours above", () => {
    expect(fmtSeconds(12.4)).toBe("12 s");
    expect(fmtSeconds(180)).toBe("3 min");
  });
});

describe("fmtIso", () => {
  it("shows a time the browser's way, and other text as it is", () => {
    expect(fmtIso(null)).toBe("–");
    expect(fmtIso("not a time")).toBe("not a time");
    expect(fmtIso("2026-10-18T12:00:00.000Z")).not.toBe("2026-10-18T12:00:00.000Z");
  });
});

describe("plural", () => {
  it("counts a noun", () => {
    expect(plural(1, "example")).toBe("1 example");
    expect(plural(0, "run")).toBe("0 runs");
    expect(plural(1000, "record")).toBe("1,000 records");
  });
});

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

