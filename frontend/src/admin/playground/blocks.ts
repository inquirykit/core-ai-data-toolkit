// What a chat reply can draw besides its Markdown (Markdown.tsx): a fenced block in the language "chart", holding
// one JSON object, a view's bar, line or pie display (data/views/views.ts) beside the rows it draws, drawn as a view's
// answer is (RowsView). CHART_PROMPT tells a model how to write one; the chat adds it to the system prompt when asked.
import { parseJsonObject, type Parsed } from "../../shared/json";
import type { RowsDisplay } from "../data/views/views";

/** The language of a fenced block drawn as a chart. */
export const CHART_LANGUAGE = "chart";

export interface ChartBlock {
  title: string | null;
  display: Extract<RowsDisplay, { type: "bar" | "line" | "pie" }>;
  result: { columns: string[]; rows: Record<string, unknown>[] };
}

const isRow = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);
const isName = (v: unknown): v is string => typeof v === "string" && v.trim() !== "";

/** The fields of the rows, in the order first seen. */
function columnsOf(rows: Record<string, unknown>[]): string[] {
  const seen = new Set<string>();
  for (const row of rows) for (const key of Object.keys(row)) seen.add(key);
  return [...seen];
}

/** A chart block's text as the chart it describes, or what is wrong with it. One still being written is not JSON yet. */
export function chartBlock(text: string): Parsed<ChartBlock> {
  const parsed = parseJsonObject(text, { what: "The chart" });
  if (!parsed.ok) return parsed;
  const { type, title, rows, x, label, value } = parsed.value;
  if (!Array.isArray(rows) || !rows.every(isRow)) return { ok: false, error: "The chart's rows are a list of objects" };
  const block = { title: isName(title) ? title : null, result: { columns: columnsOf(rows), rows } };
  if (type === "pie") {
    if (!isName(label) || !isName(value)) return { ok: false, error: "A pie names its label and value fields" };
    return { ok: true, value: { ...block, display: { type, label, value } } };
  }
  if (type === "bar" || type === "line") {
    // One field to plot can come as its name rather than a list of one: small models write it so.
    const y = typeof parsed.value.y === "string" ? [parsed.value.y] : parsed.value.y;
    if (!isName(x) || !Array.isArray(y) || !y.length || !y.every(isName)) return { ok: false, error: `A ${type} chart names its x field and a list of the y fields to plot` };
    return { ok: true, value: { ...block, display: { type, x, y } } };
  }
  return { ok: false, error: `The chart's type is "bar", "line" or "pie", not ${JSON.stringify(type ?? null)}` };
}

/** What a model is told, in the system prompt, to draw a chart. */
export const CHART_PROMPT = `When numbers are clearer as a chart, write a fenced code block in the language ${CHART_LANGUAGE} holding one JSON object: its "type" ("bar", "line" or "pie"), an optional "title", and "rows", a list of objects. A bar or line chart names its "x" field and lists the "y" fields to plot; a pie names its "label" and "value" fields. For example:

\`\`\`${CHART_LANGUAGE}
{"type": "bar", "title": "Sales", "x": "month", "y": ["sales"], "rows": [{"month": "Jan", "sales": 120}, {"month": "Feb", "sales": 150}]}
\`\`\``;
