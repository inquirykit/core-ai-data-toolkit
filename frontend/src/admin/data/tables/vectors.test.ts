import { describe, expect, it } from "vitest";
import { distanceMeaning, fmtDistance, nearestQuery } from "./vectors";

describe("the nearest rows", () => {
  const base = { list: false, required: false, indexed: false, unique: false, fulltext: false };
  const raw = { ...base, name: "v", type: "vector" as const, dimensions: 3 };
  const embedded = { ...raw, model: "m/e", from: "text" };
  const form = (text: string, k = 10, words = "") => ({ text, k, words });

  it("asks for a text with a model, else for a vector of the field's size", () => {
    expect(nearestQuery(embedded, form(" cats ", 5, " pet* "))).toEqual({ query: { text: "cats", k: 5, q: "pet*" } });
    expect(nearestQuery(embedded, form("  "))).toHaveProperty("error");
    expect(nearestQuery(embedded, { ...form("cats"), hybrid: true })).toEqual({ query: { text: "cats", k: 10, hybrid: true } });
    expect(nearestQuery(embedded, { ...form("cats"), rerank: " m/r " })).toEqual({ query: { text: "cats", k: 10, rerank: "m/r" } });
    expect(nearestQuery(embedded, { ...form("cats"), rerank: " " })).toEqual({ query: { text: "cats", k: 10 } });
    // A vector has no words to rank by.
    expect(nearestQuery(raw, { ...form("[1, 0, 0.5]"), hybrid: true })).toEqual({ query: { vector: [1, 0, 0.5], k: 10 } });
    expect(nearestQuery(raw, form("[1, 0, 0.5]"))).toEqual({ query: { vector: [1, 0, 0.5], k: 10 } });
    expect(nearestQuery(raw, form("[1, 0]"))).toEqual({ error: "v holds 3 numbers, not 2" });
    expect(nearestQuery(raw, form('[1, "0", 0]'))).toHaveProperty("error");
    expect(nearestQuery(raw, form("cats"))).toHaveProperty("error");
  });

  it("shows a distance, and what it means", () => {
    expect(fmtDistance(0)).toBe("0");
    expect(fmtDistance(0.123456)).toBe("0.1235");
    expect(fmtDistance(0.00001234)).toBe("1.2e-5");
    // DOT_PRODUCT's are 0 or below.
    expect(fmtDistance(-0.95)).toBe("-0.9500");
    expect(fmtDistance(-1)).toBe("-1.0000");
    expect(fmtDistance(-0.00001234)).toBe("-1.2e-5");
    expect(distanceMeaning("DOT_PRODUCT")).toContain("-1 for the same unit vector");
    expect(distanceMeaning(undefined)).toContain("COSINE");
  });
});
