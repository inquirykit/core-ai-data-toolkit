import { describe, expect, it } from "vitest";
import {
  chartData,
  clampWidth,
  dashboardDraftOf,
  dashboardSpecOf,
  newDashboardDraft,
  displayOf,
  draftOf,
  fmtFigure,
  guessDisplay,
  missingFields,
  moveItem,
  newDraft,
  pieSlices,
  refreshMs,
  sharedParams,
  specOf,
  valuesFor,
  valuesFromHash,
  valuesToHash,
  withDefaults,
  type ViewSpec,
} from "./views";

const PARAMS = {
  type: "object",
  properties: {
    year: { type: "integer" },
    share: { type: "number" },
    open: { type: "boolean" },
    region: { type: "string" },
    tags: { type: "array", items: { type: "string" } },
  },
};

describe("the params' values", () => {
  it("read from the hash by their types, and back", () => {
    const raw = { year: "2025", share: "0.5", open: "true", region: "north", tags: '["a","b"]', other: "x" };
    const values = valuesFromHash(PARAMS, raw);
    expect(values).toEqual({ year: 2025, share: 0.5, open: true, region: "north", tags: ["a", "b"] });
    expect(valuesToHash(values)).toEqual({ year: "2025", share: "0.5", open: "true", region: "north", tags: '["a","b"]' });
    // What does not read as its type stays text, for the server to say what is wrong; text that looks like a number
    // stays text when the param is text.
    expect(valuesFromHash(PARAMS, { year: "soon", region: "2025" })).toEqual({ year: "soon", region: "2025" });
    // Unset and empty ones are left out of the hash.
    expect(valuesToHash({ a: undefined, b: null, c: "", d: 0, e: false })).toEqual({ d: "0", e: "false" });
  });

  it("read back with the type their schema gives, through a $ref, an enum or a const", () => {
    const back = (params: Record<string, unknown>, values: Record<string, unknown>) => valuesFromHash(params, valuesToHash(values));
    const viaRef = { type: "object", $defs: { year: { type: "integer" } }, properties: { year: { $ref: "#/$defs/year" } } };
    expect(back(viaRef, { year: 2025 })).toEqual({ year: 2025 });
    expect(back({ type: "object", properties: { year: { enum: [2025, 2026] } } }, { year: 2025 })).toEqual({ year: 2025 });
    expect(back({ type: "object", properties: { region: { enum: ["north", "2025"] } } }, { region: "2025" })).toEqual({ region: "2025" });
    expect(back({ type: "object", properties: { on: { const: true } } }, { on: true })).toEqual({ on: true });
    // An optional one (anyOf with null), and one that may be text or null: text stays text.
    expect(back({ type: "object", properties: { year: { anyOf: [{ type: "integer" }, { type: "null" }] } } }, { year: 2025 })).toEqual({ year: 2025 });
    expect(back({ type: "object", properties: { code: { type: ["string", "null"] } } }, { code: "2025" })).toEqual({ code: "2025" });
    expect(back({ type: "object", properties: { code: { type: ["string", "null"] } } }, { code: "null" })).toEqual({ code: "null" });
    // One that does not say its type: JSON when it reads as JSON, else text.
    expect(back({ type: "object", properties: { any: {} } }, { any: 2025 })).toEqual({ any: 2025 });
    expect(back({ type: "object", properties: { any: {} } }, { any: "north" })).toEqual({ any: "north" });
  });

  it("a dashboard's defaults fill in what the hash does not give, as its form shows them", () => {
    const params = {
      type: "object",
      $defs: { region: { type: "string", default: "north" } },
      properties: { year: { type: "integer", default: 2026 }, region: { $ref: "#/$defs/region" }, limit: { type: "integer" }, note: { type: ["string", "null"], default: null } },
    };
    expect(withDefaults(params, {})).toEqual({ year: 2026, region: "north" });
    expect(withDefaults(params, { year: 2025, limit: 3 })).toEqual({ year: 2025, region: "north", limit: 3 });
    expect(withDefaults(undefined, { year: 2025 })).toEqual({ year: 2025 });
  });

  it("go to each of a dashboard's views by their names", () => {
    expect(valuesFor({ type: "object", properties: { year: {} } }, { year: 2025, region: "north" })).toEqual({ year: 2025 });
    expect(valuesFor(undefined, { year: 2025 })).toEqual({});
  });

  it("a dashboard's views' params merge into one schema", () => {
    const merged = sharedParams([
      { params: { type: "object", properties: { year: { type: "integer" } } } },
      { params: { type: "object", properties: { year: { type: "string" }, region: { type: "string" } } } },
    ]);
    expect(merged).toEqual({ type: "object", properties: { year: { type: "integer" }, region: { type: "string" } } });
    // A $ref into a view's own $defs is followed, so the merged schema points at nothing outside itself.
    const viaRef = sharedParams([{ params: { type: "object", $defs: { year: { type: "integer", default: 2025 } }, properties: { year: { $ref: "#/$defs/year" } } } }]);
    expect(viaRef).toEqual({ type: "object", properties: { year: { type: "integer", default: 2025 } } });
  });

  it("a dashboard refreshes no faster than every 5 s", () => {
    expect(refreshMs(0)).toBe(0);
    expect(refreshMs(null)).toBe(0);
    expect(refreshMs(2)).toBe(5000);
    expect(refreshMs(30)).toBe(30000);
  });
});

describe("what a chart draws", () => {
  const rows = [
    { month: "jan", region: "north", total: 3, count: "2" },
    { month: "jan", region: "south", total: 4, count: 1 },
    { month: "feb", region: "north", total: 5, count: null },
    { month: "jan", region: "north", total: 1, count: 1 },
  ];

  it("a series per y field, rows of one x summed, a gap where none has a number", () => {
    expect(chartData(rows, { x: "month", y: ["total", "count"] })).toEqual({
      categories: ["jan", "feb"],
      series: [
        { name: "total", values: [8, 5], slot: 0 },
        { name: "count", values: [4, null], slot: 1 },
      ],
      folded: 0,
    });
  });

  it("a series per value of the series field", () => {
    expect(chartData(rows, { x: "month", y: ["total"], series: "region" }).series).toEqual([
      { name: "north", values: [4, 5], slot: 0 },
      { name: "south", values: [4, null], slot: 1 },
    ]);
  });

  it("past eight series, the rest are summed as Other, the first keeping their colors", () => {
    const many = Array.from({ length: 10 }, (_, i) => ({ x: "a", s: `s${i}`, v: i + 1 }));
    const { series, folded } = chartData(many, { x: "x", y: ["v"], series: "s" });
    expect(series.map((s) => s.name)).toEqual(["s0", "s1", "s2", "s3", "s4", "s5", "s6", "Other"]);
    expect(series.map((s) => s.slot)).toEqual([0, 1, 2, 3, 4, 5, 6, -1]);
    expect(series[7].values).toEqual([8 + 9 + 10]);
    expect(folded).toBe(3);
  });

  it("a pie's slices, largest first, past eight the rest as Other", () => {
    expect(pieSlices(rows, "region", "total")).toEqual([
      { name: "north", value: 9, slot: 0 },
      { name: "south", value: 4, slot: 1 },
    ]);
    const many = Array.from({ length: 10 }, (_, i) => ({ k: `k${i}`, v: 10 - i }));
    const slices = pieSlices([...many, { k: "neg", v: -1 }, { k: "text", v: "x" }], "k", "v");
    expect(slices).toHaveLength(8);
    expect(slices[7]).toEqual({ name: "Other", value: 3 + 2 + 1, slot: -1 });
  });

  it("says which fields the result lacks", () => {
    const result = { columns: ["month", "total"], rows: [{ month: "jan", total: 1 }] };
    expect(missingFields({ type: "bar", x: "month", y: ["total", "count"], series: "region" }, result)).toEqual(["count", "region"]);
    expect(missingFields({ type: "number", field: "total" }, result)).toEqual([]);
    expect(missingFields({ type: "table" }, result)).toEqual([]);
    expect(missingFields({ type: "pie", label: "x", value: "y" }, { columns: [], rows: [] })).toEqual([]);
  });

  it("writes a figure", () => {
    expect(fmtFigure(1234567)).toBe((1234567).toLocaleString());
    expect(fmtFigure(2.5)).toBe((2.5).toLocaleString(undefined, { maximumFractionDigits: 2 }));
    expect(fmtFigure("x")).toBe("x");
    expect(fmtFigure(undefined)).toBe("–");
  });
});

describe("the view editor", () => {
  const saved: ViewSpec = {
    name: "sales",
    label: "Sales",
    source: { kind: "sql", query: "SELECT region, sum(total) AS t FROM sales GROUP BY region", time_limit_ms: 2000 },
    params: { type: "object", properties: { year: { type: "integer" } } },
    display: { type: "bar", x: "region", y: ["t"], stacked: true },
    max_rows: 50,
  };

  it("round-trips a saved view", () => {
    const back = specOf(draftOf(saved));
    expect(back).toEqual({
      spec: {
        name: "sales",
        label: "Sales",
        description: undefined,
        source: { kind: "sql", query: saved.source.kind === "sql" ? saved.source.query : "", time_limit_ms: 2000 },
        params: saved.params,
        display: { type: "bar", x: "region", y: ["t"], series: undefined, stacked: true },
        max_rows: 50,
        refresh_s: undefined,
      },
    });
  });

  it("takes a rows or search source as JSON", () => {
    const draft = { ...newDraft(["orders"]), name: "top", kind: "rows" as const };
    expect(specOf(draft)).toMatchObject({ spec: { source: { kind: "rows", table: "orders", sort: "-id", limit: 100 } } });
    const rows: ViewSpec = { name: "r", source: { kind: "rows", table: "orders", filters: { region: { $param: "region" } }, sort: null, search: null, limit: 5 } };
    expect(JSON.parse(draftOf(rows).sourceJson)).toEqual({ table: "orders", filters: { region: { $param: "region" } }, limit: 5 });
  });

  it("says what is wrong first", () => {
    expect(specOf({ ...newDraft([]), name: "" })).toEqual({ error: "Give the view a name" });
    expect(specOf({ ...newDraft([]), name: "v", paramsJson: "[1]" })).toMatchObject({ error: expect.stringContaining("a JSON object") });
    expect(specOf({ ...newDraft([]), name: "v", maxRows: "0" })).toEqual({ error: "Rows at most is a whole number above 0" });
    expect(specOf({ ...newDraft([]), name: "v", kind: "search", sourceJson: "{" })).toMatchObject({ error: expect.stringContaining("not valid JSON") });
  });

  it("builds each display from its fields", () => {
    const draft = draftOf(saved).display;
    expect(displayOf({ ...draft, type: "line" })).toEqual({ type: "line", x: "region", y: ["t"], series: undefined });
    expect(displayOf({ ...draft, type: "table", columns: "a, b" })).toEqual({ type: "table", columns: ["a", "b"] });
    expect(displayOf({ ...draft, type: "table", columns: "" })).toEqual({ type: "table" });
    expect(displayOf({ ...draft, type: "files" })).toEqual({ type: "files", bucket: undefined });
  });

  it("guesses a chart's fields from a result", () => {
    const result = { columns: ["region", "note", "t"], rows: [{ region: "n", note: "x", t: 3 }] };
    const empty = draftOf({ ...saved, display: { type: "table" } }).display;
    expect(guessDisplay(empty, "bar", result)).toMatchObject({ type: "bar", x: "region", y: "t" });
    expect(guessDisplay(empty, "pie", result)).toMatchObject({ type: "pie", label: "region", value: "t" });
    expect(guessDisplay(empty, "number", result)).toMatchObject({ type: "number", field: "t" });
    // What the draft says already stays.
    expect(guessDisplay({ ...empty, x: "note" }, "line", result)).toMatchObject({ x: "note" });
  });
});

describe("a dashboard's items", () => {
  it("move up and down within the list", () => {
    expect(moveItem(["a", "b", "c"], 1, -1)).toEqual(["b", "a", "c"]);
    expect(moveItem(["a", "b", "c"], 1, 1)).toEqual(["a", "c", "b"]);
    const same = ["a", "b"];
    expect(moveItem(same, 0, -1)).toBe(same);
    expect(moveItem(same, 1, 1)).toBe(same);
  });

  it("are 1 to 12 columns wide", () => {
    expect([0, 1, 6.4, 12, 13, Number.NaN].map(clampWidth)).toEqual([1, 1, 6, 12, 12, 1]);
  });
});

describe("the dashboard editor", () => {
  it("round-trips a saved dashboard", () => {
    const spec = { name: "main", title: "Sales", params: { type: "object", properties: { year: { type: "integer" } } }, refresh_s: 30, items: [{ view: "a", width: 8 }, { view: "b", width: 4 }] };
    expect(dashboardSpecOf(dashboardDraftOf(spec))).toEqual({ spec: { ...spec, description: undefined } });
  });

  it("refreshes never, or every 5 s at least", () => {
    const draft = { ...newDashboardDraft(), name: "d", title: "D" };
    expect(dashboardSpecOf(draft)).toMatchObject({ spec: { refresh_s: 0, items: [] } });
    expect(dashboardSpecOf({ ...draft, refreshS: "3" })).toEqual({ error: "Refresh is 0 (none) or at least 5 seconds" });
    expect(dashboardSpecOf({ ...draft, refreshS: "-1" })).toMatchObject({ error: expect.stringContaining("whole number") });
    expect(dashboardSpecOf({ ...draft, title: " " })).toEqual({ error: "Give the dashboard a title" });
  });
});
