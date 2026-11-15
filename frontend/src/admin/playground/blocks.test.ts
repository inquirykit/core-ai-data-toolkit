import { describe, expect, it } from "vitest";
import { CHART_LANGUAGE, CHART_PROMPT, chartBlock } from "./blocks";

const rows = [
  { month: "Jan", sales: 120 },
  { month: "Feb", sales: 150, returns: 4 },
];
const json = (value: unknown) => JSON.stringify(value);

describe("chartBlock", () => {
  it("reads a bar chart, its columns in the order first seen", () => {
    expect(chartBlock(json({ type: "bar", title: "Sales", x: "month", y: ["sales", "returns"], rows }))).toEqual({
      ok: true,
      value: { title: "Sales", display: { type: "bar", x: "month", y: ["sales", "returns"] }, result: { columns: ["month", "sales", "returns"], rows } },
    });
  });

  it("takes one y field as its name, and no title", () => {
    const parsed = chartBlock(json({ type: "line", x: "month", y: "sales", rows }));
    expect(parsed.ok && parsed.value.display).toEqual({ type: "line", x: "month", y: ["sales"] });
    expect(parsed.ok && parsed.value.title).toBeNull();
  });

  it("reads a pie", () => {
    const parsed = chartBlock(json({ type: "pie", label: "month", value: "sales", rows }));
    expect(parsed.ok && parsed.value.display).toEqual({ type: "pie", label: "month", value: "sales" });
  });

  it("refuses a block cut off, or not an object", () => {
    expect(chartBlock('{"type": "bar", "rows": [').ok).toBe(false);
    expect(chartBlock("[1, 2]")).toEqual({ ok: false, error: "The chart is a JSON object" });
  });

  it("says what a chart lacks", () => {
    expect(chartBlock(json({ type: "bar", x: "month", rows }))).toEqual({ ok: false, error: "A bar chart names its x field and a list of the y fields to plot" });
    expect(chartBlock(json({ type: "pie", label: "month", rows }))).toEqual({ ok: false, error: "A pie names its label and value fields" });
    expect(chartBlock(json({ type: "bar", x: "month", y: ["sales"], rows: [1, 2] }))).toEqual({ ok: false, error: "The chart's rows are a list of objects" });
    expect(chartBlock(json({ type: "radar", rows }))).toEqual({ ok: false, error: 'The chart\'s type is "bar", "line" or "pie", not "radar"' });
  });

  it("reads the prompt's own example", () => {
    const example = new RegExp("```" + CHART_LANGUAGE + "\\n(.*)\\n```").exec(CHART_PROMPT)?.[1];
    expect(example && chartBlock(example).ok).toBe(true);
  });
});
