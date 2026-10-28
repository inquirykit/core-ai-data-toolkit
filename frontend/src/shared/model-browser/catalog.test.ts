import { describe, expect, it } from "vitest";
import {
  defaultTask,
  filterModels,
  memoryBar,
  matchesPattern,
  otherIdProblem,
  planSummary,
  tagsOf,
  type CatalogModel,
  type CatalogTask,
  type LoadPlan,
} from "./catalog";

const model = (id: string, extra: Partial<CatalogModel> = {}): CatalogModel => ({ id, status: "unloaded", ...extra });

const models = [
  model("org/chat", { description: "Small chat model", tags: ["chat", "multilingual"] }),
  model("org/loaded", { status: "loaded", tags: ["chat"] }),
  model("org/disk", { on_disk_gb: 1.2, tags: ["completion"] }),
];
const all = { query: "", status: "all", tag: "" } as const;
const ids = (list: CatalogModel[]) => list.map((m) => m.id);

describe("filterModels", () => {
  it("puts loaded, then downloaded models first", () => {
    expect(ids(filterModels(models, all))).toEqual(["org/loaded", "org/disk", "org/chat"]);
  });

  it("matches every word against the ID, description and tags", () => {
    expect(ids(filterModels(models, { ...all, query: "small CHAT" }))).toEqual(["org/chat"]);
    expect(ids(filterModels(models, { ...all, query: "multilingual" }))).toEqual(["org/chat"]);
    expect(filterModels(models, { ...all, query: "nothing" })).toEqual([]);
  });

  it("filters by status and tag", () => {
    expect(ids(filterModels(models, { ...all, status: "loaded" }))).toEqual(["org/loaded"]);
    expect(ids(filterModels(models, { ...all, status: "on_disk" }))).toEqual(["org/disk"]);
    expect(ids(filterModels(models, { ...all, tag: "chat" }))).toEqual(["org/loaded", "org/chat"]);
  });
});

describe("tagsOf", () => {
  it("lists each tag once, sorted", () => {
    expect(tagsOf(models)).toEqual(["chat", "completion", "multilingual"]);
  });
});

describe("other IDs", () => {
  const task = (extra: Partial<CatalogTask>): CatalogTask => ({ task: "decision", any_id: false, patterns: [], models: [], ...extra });

  it("matches globs like the registry", () => {
    expect(matchesPattern("Vega-Big", "vega-*")).toBe(true);
    expect(matchesPattern("me/vega-x", "*/vega-*")).toBe(true);
    expect(matchesPattern("vegan", "vega-*")).toBe(false);
    expect(matchesPattern("custom:a.b", "custom:*")).toBe(true);
  });

  it("says why a task would not take an ID", () => {
    expect(otherIdProblem(task({ any_id: true }), "any/model")).toBe("");
    expect(otherIdProblem(task({ patterns: ["vega-*"] }), "vega-9b")).toBe("");
    expect(otherIdProblem(task({ patterns: ["vega-*"] }), "gpt2")).toMatch(/must match vega-\*/);
    expect(otherIdProblem(task({}), "gpt2")).toMatch(/only takes the models listed/);
    expect(otherIdProblem(task({ any_id: true }), "")).toMatch(/Enter/);
  });
});

describe("planSummary", () => {
  const plan = (extra: Partial<LoadPlan>): LoadPlan => ({
    model_id: "m",
    task: "t",
    estimate_gb: 2,
    outcome: "fits",
    evicts: [],
    reason: null,
    memory_used_gb: 1,
    max_memory_gb: 8,
    free_memory_gb: 10,
    loaded_models: 1,
    max_models: 3,
    ...extra,
  });

  it("puts each outcome in words", () => {
    expect(planSummary(plan({}))).toEqual({ text: "Needs about 2.00 GB. Fits without unloading anything.", kind: "ok" });
    expect(planSummary(plan({ outcome: "evicts", evicts: ["a:t"] })).text).toMatch(/unloads a:t first/);
    expect(planSummary(plan({ outcome: "waits", reason: "max_models" })).text).toMatch(/3-model limit/);
    const tooBig = planSummary(plan({ outcome: "too_big", reason: "max_memory_gb", estimate_gb: null }));
    expect(tooBig.kind).toBe("bad");
    expect(tooBig.text).toMatch(/^Size unknown.*8\.00 GB budget/);
  });
});

describe("memoryBar", () => {
  const plan = (extra: Partial<LoadPlan>): LoadPlan => ({
    model_id: "m",
    task: "t",
    estimate_gb: 2,
    outcome: "fits",
    evicts: [],
    reason: null,
    memory_used_gb: 2,
    max_memory_gb: 8,
    free_memory_gb: null,
    loaded_models: 1,
    max_models: 3,
    ...extra,
  });

  it("scales to the budget while the model fits", () => {
    expect(memoryBar(plan({}))).toEqual({ used: 0.25, need: 0.25, budget: 1 });
  });

  it("scales past the budget when the model would not fit, marking where it ends", () => {
    expect(memoryBar(plan({ estimate_gb: 6, outcome: "evicts", memory_used_gb: 6 }))).toEqual({ used: 0.5, need: 0.5, budget: 8 / 12 });
  });

  it("adds nothing for a loaded model, and draws nothing with no budget and no figures", () => {
    expect(memoryBar(plan({ outcome: "loaded" }))?.need).toBe(0);
    expect(memoryBar(plan({ estimate_gb: null, memory_used_gb: 0, max_memory_gb: 0 }))).toBeNull();
  });
});

describe("defaultTask", () => {
  const t = (task: string, n: number): CatalogTask => ({ task, any_id: false, patterns: [], models: Array.from({ length: n }, (_, i) => ({ id: `${i}`, status: "unloaded" })) });

  it("prefers text-generation, then the first task with models", () => {
    expect(defaultTask([t("custom", 0), t("text-generation", 1)])).toBe("text-generation");
    expect(defaultTask([t("custom", 0), t("embedding", 2)])).toBe("embedding");
    expect(defaultTask([t("custom", 0)])).toBe("custom");
    expect(defaultTask([])).toBe("");
  });
});
