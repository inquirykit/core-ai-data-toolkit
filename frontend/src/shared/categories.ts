// The tabs tasks are filed under: the playground's tabs and the model browser's headings. Each task names its
// tab on the server (define_task's category in models/contracts.py, served with the catalog and GET /tasks); here
// is how each tab looks, in the order they are shown. TABS is keyed by the server's categories, so one it adds
// without a tab here fails the typecheck.
import { AudioLines, Image, MessagesSquare, Puzzle, Scale, TrendingUp, Type, type LucideIcon } from "lucide-react";
import { useApi, type Schemas } from "./api";

export type CategoryKey = Schemas["CatalogTask"]["category"];

export interface Category {
  key: CategoryKey;
  label: string;
  blurb: string;
  icon: LucideIcon;
  /** Not offered in the playground yet: not shown there */
  disabled?: boolean;
}

const TABS: Record<CategoryKey, Omit<Category, "key">> = {
  chat: { label: "Chat", blurb: "Chat with models and compare their replies side by side", icon: MessagesSquare },
  vision: { label: "Vision", blurb: "Ask about an image", icon: Image },
  text: { label: "Text", blurb: "Classify text, answer questions from a passage, embed and rerank text", icon: Type },
  audio: { label: "Audio", blurb: "Transcribe speech", icon: AudioLines },
  decision: { label: "Decision", blurb: "Weigh options against questions", icon: Scale },
  // The AutoGluon tasks need a trained predictor first; the playground does not offer them yet.
  prediction: { label: "Prediction", blurb: "Tabular and time-series predictors", icon: TrendingUp, disabled: true },
  other: { label: "Other", blurb: "Custom tasks", icon: Puzzle },
};

/** The tabs, in order. */
export const CATEGORIES: Category[] = (Object.keys(TABS) as CategoryKey[]).map((key) => ({ key, ...TABS[key] }));

/** ``tasks`` under their tabs, in the tabs' order, each keeping the order ``tasks`` gives it; tabs without one are left out. */
export function byCategory<T extends { category: CategoryKey }>(tasks: T[]): { category: Category; tasks: T[] }[] {
  return CATEGORIES.map((category) => ({ category, tasks: tasks.filter((t) => t.category === category.key) })).filter(
    (group) => group.tasks.length > 0,
  );
}

/** Where a task sits in the browser: right after the task it varies, if it varies one in ``tasks``. */
export function variantsAfterTheirTasks<T extends { task: string; group: string }>(tasks: T[]): T[] {
  const present = new Set(tasks.map((t) => t.task));
  const ordered: T[] = [];
  for (const t of tasks) {
    if (t.group !== t.task && present.has(t.group)) continue;
    ordered.push(t, ...tasks.filter((v) => v.group === t.task && v.task !== t.task));
  }
  return ordered;
}

/** A task's name as a heading: a variant's after the task it varies ("Classification · Your labels"). */
export function taskTitle(t: { task: string; label: string; group: string; group_label: string }): string {
  return t.group === t.task ? t.label : `${t.group_label} · ${t.label}`;
}

/**
 * Each task's title, from GET /tasks: a variant's after the task it varies (taskTitle), since a list of tasks shows
 * it on its own; the task's name until that answers, or for a task it does not list.
 */
export function useTaskLabels(): (task: string) => string {
  const client = useApi();
  // GET /tasks changes only with a restart: asked once per page (TryPanel shares the reply).
  const listed = client.useQuery("get", "/tasks", undefined, { staleTime: Infinity, select: (d) => d.tasks });
  return (task) => {
    const info = listed.data?.[task];
    return info ? taskTitle({ task, ...info }) : task;
  };
}
