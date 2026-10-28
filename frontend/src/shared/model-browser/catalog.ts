// The model browser's logic: what GET /models/catalog and /models/estimate
// return, filtering, and the words and bar for a load plan. The .tsx files here draw it.
import type { Schemas } from "../api";
import { fmtGB } from "../format";

export type CatalogTask = Schemas["CatalogTask"];
export type CatalogModel = Schemas["CatalogModel"];
export type LoadPlan = Schemas["LoadPlan"];

export type StatusFilter = "all" | "loaded" | "on_disk";

export interface ModelFilter {
  query: string;
  status: StatusFilter;
  /** "" for any tag */
  tag: string;
}

const rank = (m: CatalogModel) => (m.status === "unloaded" ? (m.on_disk_gb != null ? 1 : 2) : 0);

/**
 * The models that pass ``filter``: every word of the query in the ID,
 * description or tags. Loaded models come first, then downloaded ones, each
 * in catalog order (the order the model files list them).
 */
export function filterModels(models: CatalogModel[], filter: ModelFilter): CatalogModel[] {
  const words = filter.query.toLowerCase().split(/\s+/).filter(Boolean);
  return models
    .filter((m) => {
      const text = `${m.id} ${m.description ?? ""} ${(m.tags ?? []).join(" ")}`.toLowerCase();
      return (
        words.every((w) => text.includes(w)) &&
        (filter.status !== "loaded" || m.status !== "unloaded") &&
        (filter.status !== "on_disk" || m.on_disk_gb != null) &&
        (!filter.tag || (m.tags ?? []).includes(filter.tag))
      );
    })
    .map((m, i) => [m, i] as const)
    .sort(([a, i], [b, j]) => rank(a) - rank(b) || i - j)
    .map(([m]) => m);
}

/** Every tag the models use, sorted. */
export function tagsOf(models: CatalogModel[]): string[] {
  return [...new Set(models.flatMap((m) => m.tags ?? []))].sort();
}

/** Whether ``id`` fits a glob pattern, case-insensitively, as models/registry.py matches them. */
export function matchesPattern(id: string, pattern: string): boolean {
  const regex = pattern
    .toLowerCase()
    .replace(/[.+^${}()|\\]/g, "\\$&")
    .replace(/\*/g, ".*")
    .replace(/\?/g, ".")
    .replace(/\[!/g, "[^");
  return new RegExp(`^${regex}$`).test(id.toLowerCase());
}

/** Why ``task`` would not take ``id`` typed in by hand, or "" if it would. */
export function otherIdProblem(task: CatalogTask, id: string): string {
  if (!id) return "Enter a model ID.";
  if (task.any_id || task.models.some((m) => m.id === id) || task.patterns.some((p) => matchesPattern(id, p))) return "";
  if (task.patterns.length) return `IDs for ${task.task} must match ${task.patterns.join(" or ")}.`;
  return `${task.task} only takes the models listed.`;
}

const LIMIT: Record<string, (p: LoadPlan) => string> = {
  max_models: (p) => `the ${p.max_models}-model limit (MAX_MODELS)`,
  max_memory_gb: (p) => `the ${fmtGB(p.max_memory_gb)} budget (MAX_MEMORY_GB)`,
  free_memory: (p) => `free memory (${fmtGB(p.free_memory_gb)})`,
};

/** What loading would do, in a sentence, and how much it should worry the reader. */
export function planSummary(plan: LoadPlan): { text: string; kind: "ok" | "warn" | "bad" } {
  const size = plan.estimate_gb != null ? `Needs about ${fmtGB(plan.estimate_gb)}` : "Size unknown until loaded";
  const limit = plan.reason ? LIMIT[plan.reason](plan) : "";
  const evicts = plan.evicts.join(", ");
  switch (plan.outcome) {
    case "loaded":
      return { text: "Loaded now.", kind: "ok" };
    case "fits":
      return { text: `${size}. Fits without unloading anything.`, kind: "ok" };
    case "evicts":
      return { text: `${size}. Loading it unloads ${evicts} first.`, kind: "warn" };
    case "waits":
      return {
        text: `${size}. Over ${limit}${evicts ? ` even after unloading ${evicts}` : ""}, and the other models are busy, so the load waits for them.`,
        kind: "warn",
      };
    case "too_big":
      return { text: `${size}, more than ${limit} even with nothing else loaded. Loading will be tried anyway.`, kind: "bad" };
  }
}

/** The bar under a plan: memory in use and this model's share, as fractions of the bar. */
export interface MemoryBar {
  used: number;
  need: number;
  /** Where the MAX_MEMORY_GB budget falls, or null without a budget */
  budget: number | null;
}

/** Null when there is nothing to draw: no budget and nothing in use or estimated. */
export function memoryBar(plan: LoadPlan): MemoryBar | null {
  const budget = plan.max_memory_gb > 0 ? plan.max_memory_gb : null;
  const need = plan.outcome === "loaded" ? 0 : (plan.estimate_gb ?? 0);
  const scale = Math.max(budget ?? 0, plan.memory_used_gb + need);
  if (!scale) return null;
  return { used: plan.memory_used_gb / scale, need: need / scale, budget: budget == null ? null : budget / scale };
}

/** The task to open on: text-generation if shown, else the first task with models, else the first. */
export function defaultTask(tasks: CatalogTask[]): string {
  if (tasks.some((t) => t.task === "text-generation")) return "text-generation";
  return (tasks.find((t) => t.models.length) ?? tasks[0])?.task ?? "";
}

// Vision-language models also answer text-only chats.
export const CHAT_TASKS = ["text-generation", "vision-language"];
