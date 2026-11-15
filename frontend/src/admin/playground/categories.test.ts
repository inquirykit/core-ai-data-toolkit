import { describe, expect, it } from "vitest";
import { CATEGORIES, type CategoryKey } from "../../shared/categories";
import type { CatalogModel, CatalogTask } from "../../shared/model-browser/catalog";
import {
  categoryOf,
  choiceIn,
  choicesOf,
  entryFor,
  groupFor,
  groupsIn,
  modelsOf,
  shownCategories,
  tasksIn,
  withChoice,
  withLink,
  type TabPrefs,
} from "./categories";

const model = (id: string, extra: Partial<CatalogModel> = {}): CatalogModel => ({ id, status: "unloaded", ...extra });
const task = (name: string, category: CategoryKey, models: CatalogModel[] = [], extra: Partial<CatalogTask> = {}): CatalogTask => ({
  task: name,
  category,
  label: name,
  group: name,
  group_label: name,
  models,
  any_id: false,
  patterns: [],
  ...extra,
});
// A variant of text-classification: its models choose among labels named in the request.
const variant = { group: "text-classification", group_label: "Classification", label: "Your labels" };
const category = (key: CategoryKey) => CATEGORIES.find((c) => c.key === key)!;

// In the catalog's order: the order the server defined the tasks.
const catalog = [
  task("text-generation", "chat", [model("org/gpt"), model("org/vlm", { status: "loaded" })]),
  task("text-classification", "text", [model("org/sentiment"), model("org/nli", { on_disk_gb: 0.5 })], { label: "Classification", group_label: "Classification" }),
  task("zero-shot-classification", "text", [model("org/nli", { status: "loaded" }), model("org/zs")], variant),
  task("vision-language", "vision", [model("org/vlm", { status: "loaded" })]),
  task("embedding", "text"),
  task("rerank", "text"),
  task("tabular-prediction", "prediction"),
  task("my-task", "other"),
];

describe("tasksIn", () => {
  it("lists the tasks that name the tab, in the catalog's order", () => {
    expect(tasksIn(category("text"), catalog).map((t) => t.task)).toEqual([
      "text-classification",
      "zero-shot-classification",
      "embedding",
      "rerank",
    ]);
    expect(tasksIn(category("other"), catalog).map((t) => t.task)).toEqual(["my-task"]);
  });

  it("lets Chat borrow the tasks whose models chat, which keep their own tab too", () => {
    expect(tasksIn(category("chat"), catalog).map((t) => t.task)).toEqual(["text-generation", "vision-language"]);
    expect(tasksIn(category("vision"), catalog).map((t) => t.task)).toEqual(["vision-language"]);
  });
});

describe("shownCategories", () => {
  it("shows tabs with tasks, never a disabled one", () => {
    const shown = shownCategories(catalog);
    expect(shown.map((c) => c.key)).toEqual(["chat", "vision", "text", "other"]);
    expect(shownCategories([])).toEqual([]);
  });
});

describe("categoryOf", () => {
  it("opens chat models in Chat, other tasks in their own tab", () => {
    expect(categoryOf("vision-language", catalog)).toBe("chat");
    expect(categoryOf("rerank", catalog)).toBe("text");
    expect(categoryOf("my-task", catalog)).toBe("other");
  });

  it("opens no disabled tab, and nothing for a task the catalog lacks", () => {
    expect(categoryOf("tabular-prediction", catalog)).toBeNull();
    expect(categoryOf("gone", catalog)).toBeNull();
  });
});

describe("withLink", () => {
  const prefs: TabPrefs = { category: "chat", tasks: { text: "embedding" }, models: { embedding: "org/embed" } };

  it("opens the linked model in its task's tab", () => {
    expect(withLink(prefs, { model: "org/rank", task: "rerank" }, catalog)).toEqual({
      category: "text",
      tasks: { text: "rerank" },
      models: { embedding: "org/embed", rerank: "org/rank" },
      groups: { rerank: "rerank" },
    });
  });

  it("opens a variant's model in its group's entry", () => {
    const opened = withLink(prefs, { model: "org/zs", task: "zero-shot-classification" }, catalog);
    expect(opened.tasks).toEqual({ text: "text-classification" });
    expect(opened.groups).toEqual({ "text-classification": "zero-shot-classification" });
    expect(opened.models["zero-shot-classification"]).toBe("org/zs");
  });

  it("leaves the tabs as they were when the catalog has no tab for the task", () => {
    expect(withLink(prefs, { model: "m", task: "tabular-prediction" }, catalog)).toBe(prefs);
    expect(withLink(prefs, { model: "m", task: "gone" }, catalog)).toBe(prefs);
  });
});

describe("groupsIn", () => {
  it("gathers a task's variants into its entry of the task switch", () => {
    expect(groupsIn(category("text"), catalog).map((g) => [g.key, g.label, g.tasks.map((t) => t.task)])).toEqual([
      ["text-classification", "Classification", ["text-classification", "zero-shot-classification"]],
      ["embedding", "embedding", ["embedding"]],
      ["rerank", "rerank", ["rerank"]],
    ]);
  });
});

describe("a group's models", () => {
  const classification = groupsIn(category("text"), catalog)[0];

  it("lists every task's models, loaded first, the variant's with its badge, a model under two tasks twice", () => {
    expect(choicesOf(classification)).toEqual([
      { id: "org/nli", task: "zero-shot-classification", status: "loaded", badge: "Your labels" },
      { id: "org/nli", task: "text-classification", status: "unloaded", badge: undefined },
      { id: "org/sentiment", task: "text-classification", status: "unloaded", badge: undefined },
      { id: "org/zs", task: "zero-shot-classification", status: "unloaded", badge: "Your labels" },
    ]);
  });

  it("gives a model chosen with Browse an entry of its own, badged when a variant's", () => {
    expect(entryFor(classification, { id: "org/zs", task: "zero-shot-classification" }).status).toBe("unloaded");
    expect(entryFor(classification, { id: "other/nli", task: "zero-shot-classification" })).toEqual({
      id: "other/nli",
      task: "zero-shot-classification",
      badge: "Your labels",
    });
  });

  it("chooses the model last picked in the group, else its own task's last, else the first listed", () => {
    const base: TabPrefs = { category: "text", tasks: {}, models: {} };
    expect(choiceIn(classification, base)).toEqual({ id: "org/nli", task: "zero-shot-classification" });
    // Stored before groups: the model of the group's own task.
    expect(choiceIn(classification, { ...base, models: { "text-classification": "org/sentiment" } })).toEqual({
      id: "org/sentiment",
      task: "text-classification",
    });
    const picked = withChoice(base, "text-classification", { id: "org/zs", task: "zero-shot-classification" });
    expect(picked.groups).toEqual({ "text-classification": "zero-shot-classification" });
    expect(choiceIn(classification, picked)).toEqual({ id: "org/zs", task: "zero-shot-classification" });
  });

  it("falls back to the group's own model when the variant picked is gone", () => {
    const prefs: TabPrefs = {
      category: "text",
      tasks: {},
      models: { "text-classification": "org/sentiment", "gone-variant": "org/old" },
      groups: { "text-classification": "gone-variant" },
    };
    expect(choiceIn(classification, prefs)).toEqual({ id: "org/sentiment", task: "text-classification" });
  });
});

describe("groupFor", () => {
  const groups = groupsIn(category("text"), catalog);

  it("finds the entry by its key, or by a task in it (stored before groups), else the first", () => {
    expect(groupFor(groups, "embedding").key).toBe("embedding");
    expect(groupFor(groups, "zero-shot-classification").key).toBe("text-classification");
    expect(groupFor(groups, "gone").key).toBe("text-classification");
    expect(groupFor(groups, undefined).key).toBe("text-classification");
  });
});

describe("modelsOf", () => {
  it("lists each model once, loaded first", () => {
    expect(modelsOf(tasksIn(category("chat"), catalog)).map((m) => m.id)).toEqual(["org/vlm", "org/gpt"]);
  });
});
