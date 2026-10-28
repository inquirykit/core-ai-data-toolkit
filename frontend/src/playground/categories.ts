// The playground's tabs: the catalog's tasks grouped into categories, and
// which of them can be used yet.
import { filterModels, type CatalogModel, type CatalogTask } from "../shared/model-browser/catalog";

export type CategoryKey = "chat" | "vision" | "text" | "audio" | "decision" | "prediction" | "other";

export interface Category {
  key: CategoryKey;
  label: string;
  blurb: string;
  /** Its tasks, in the order the task switch shows them. "other" takes every task no category names. */
  tasks: string[];
  /** Not offered yet: not shown. */
  disabled?: boolean;
}

export const CATEGORIES: Category[] = [
  { key: "chat", label: "Chat", blurb: "Chat with models and compare their replies side by side", tasks: ["text-generation", "vision-language"] },
  { key: "vision", label: "Vision", blurb: "Ask about an image", tasks: ["vision-language"] },
  { key: "text", label: "Text", blurb: "Classify text, answer questions from a passage, embed text", tasks: ["text-classification", "question-answering", "embedding"] },
  { key: "audio", label: "Audio", blurb: "Transcribe speech", tasks: ["speech-to-text"] },
  { key: "decision", label: "Decision", blurb: "Weigh options against questions", tasks: ["decision"] },
  // The AutoGluon tasks need a trained predictor first; not offered here yet,
  // so the tab is not shown.
  {
    key: "prediction",
    label: "Prediction",
    blurb: "Tabular, multimodal and time-series predictors",
    tasks: ["tabular-prediction", "multimodal-prediction", "timeseries-forecast"],
    disabled: true,
  },
  { key: "other", label: "Other", blurb: "Custom tasks", tasks: [] },
];

const NAMED = new Set(CATEGORIES.flatMap((c) => c.tasks));

/** Short names for the task switch; other tasks show their ID. */
const TASK_LABELS: Record<string, string> = {
  "text-generation": "Text generation",
  "vision-language": "Vision-language",
  "text-classification": "Classification",
  "question-answering": "Question answering",
  embedding: "Embedding",
  "speech-to-text": "Speech to text",
  decision: "Decision",
};

export const taskLabel = (task: string) => TASK_LABELS[task] ?? task;

/** The catalog's tasks that belong to ``category``, in the category's order. */
export function tasksIn(category: Category, catalog: CatalogTask[]): CatalogTask[] {
  if (category.key === "other") return catalog.filter((t) => !NAMED.has(t.task));
  return category.tasks.flatMap((name) => catalog.filter((t) => t.task === name));
}

/** The tabs to show: every category the server has a task for, except the disabled ones. */
export function shownCategories(catalog: CatalogTask[]): Category[] {
  return CATEGORIES.filter((c) => !c.disabled && tasksIn(c, catalog).length > 0);
}

/** The category a task opens in: the first usable one that has it. */
export function categoryOf(task: string): CategoryKey | null {
  if (task === "text-generation" || task === "vision-language") return "chat";
  const found = CATEGORIES.find((c) => c.tasks.includes(task));
  if (found) return found.disabled ? null : found.key;
  return "other";
}

/** ``tasks``' models with each ID once: loaded first, then downloaded, then the rest. */
export function modelsOf(tasks: CatalogTask[]): CatalogModel[] {
  const all = filterModels(
    tasks.flatMap((t) => t.models),
    { query: "", status: "all", tag: "" },
  );
  const seen = new Set<string>();
  return all.filter((m) => !seen.has(m.id) && seen.add(m.id));
}
