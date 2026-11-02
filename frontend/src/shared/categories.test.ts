import { describe, expect, it } from "vitest";
import { byCategory, CATEGORIES, taskTitle, variantsAfterTheirTasks, type CategoryKey } from "./categories";

describe("CATEGORIES", () => {
  it("lists every tab once, Chat first and Other last", () => {
    const keys = CATEGORIES.map((c) => c.key);
    expect(new Set(keys).size).toBe(keys.length);
    expect(keys[0]).toBe("chat");
    expect(keys.at(-1)).toBe("other");
  });
});

describe("byCategory", () => {
  const t = (task: string, category: CategoryKey) => ({ task, category });

  it("files tasks under their tabs in the tabs' order, each keeping the order given, and drops empty tabs", () => {
    const groups = byCategory([t("embedding", "text"), t("my-task", "other"), t("rerank", "text"), t("text-generation", "chat")]);
    expect(groups.map((g) => [g.category.key, g.tasks.map((x) => x.task)])).toEqual([
      ["chat", ["text-generation"]],
      ["text", ["embedding", "rerank"]],
      ["other", ["my-task"]],
    ]);
    expect(byCategory([])).toEqual([]);
  });
});

describe("variants", () => {
  const t = (task: string, group = task) => ({ task, group });

  it("sit right after the task they vary, or where they were when it is not listed", () => {
    const tasks = [t("text-classification"), t("embedding"), t("zero-shot-classification", "text-classification"), t("zs-ner", "ner")];
    expect(variantsAfterTheirTasks(tasks).map((x) => x.task)).toEqual(["text-classification", "zero-shot-classification", "embedding", "zs-ner"]);
  });

  it("are titled after the task they vary", () => {
    const named = { task: "zero-shot-classification", label: "Your labels", group: "text-classification", group_label: "Classification" };
    expect(taskTitle(named)).toBe("Classification · Your labels");
    expect(taskTitle({ ...named, task: "text-classification", label: "Classification" })).toBe("Classification");
  });
});
