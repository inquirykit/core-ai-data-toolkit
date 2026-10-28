import { describe, expect, it } from "vitest";
import type { CatalogModel, CatalogTask } from "../shared/model-browser/catalog";
import { CATEGORIES, categoryOf, modelsOf, shownCategories, tasksIn } from "./categories";

const model = (id: string, extra: Partial<CatalogModel> = {}): CatalogModel => ({ id, status: "unloaded", ...extra });
const task = (name: string, models: CatalogModel[] = []): CatalogTask => ({ task: name, models, any_id: false, patterns: [] });
const category = (key: string) => CATEGORIES.find((c) => c.key === key)!;

const catalog = [
  task("text-generation", [model("org/gpt"), model("org/vlm", { status: "loaded" })]),
  task("vision-language", [model("org/vlm", { status: "loaded" })]),
  task("embedding"),
  task("text-classification"),
  task("tabular-prediction"),
  task("my-task"),
];

describe("tasksIn", () => {
  it("lists the category's tasks in its own order", () => {
    expect(tasksIn(category("text"), catalog).map((t) => t.task)).toEqual(["text-classification", "embedding"]);
  });

  it("puts tasks no category names under Other", () => {
    expect(tasksIn(category("other"), catalog).map((t) => t.task)).toEqual(["my-task"]);
  });
});

describe("shownCategories", () => {
  it("shows categories with tasks, never a disabled one", () => {
    const shown = shownCategories(catalog);
    expect(shown.map((c) => c.key)).toEqual(["chat", "vision", "text", "other"]);
    expect(shown.some((c) => c.key === "prediction")).toBe(false);
    expect(shownCategories([])).toEqual([]);
  });
});

describe("categoryOf", () => {
  it("opens chat models in Chat, other tasks in their category", () => {
    expect(categoryOf("vision-language")).toBe("chat");
    expect(categoryOf("question-answering")).toBe("text");
    expect(categoryOf("my-task")).toBe("other");
  });

  it("opens no disabled category", () => {
    expect(categoryOf("timeseries-forecast")).toBeNull();
  });
});

describe("modelsOf", () => {
  it("lists each model once, loaded first", () => {
    expect(modelsOf(tasksIn(category("chat"), catalog)).map((m) => m.id)).toEqual(["org/vlm", "org/gpt"]);
  });
});
