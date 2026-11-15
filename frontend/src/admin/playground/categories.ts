// The playground's tabs (shared/categories.ts): the catalog's tasks in the tab each names, and which tabs
// can be used yet.
import { CATEGORIES, type Category, type CategoryKey } from "../../shared/categories";
import { CHAT_TASKS, filterModels, type CatalogModel, type CatalogTask } from "../../shared/model-browser/catalog";

/** ``category``'s tasks, in the catalog's order. Chat takes the tasks whose models chat (CHAT_TASKS), wherever they live. */
export function tasksIn(category: Category, catalog: CatalogTask[]): CatalogTask[] {
  if (category.key === "chat") return catalog.filter((t) => CHAT_TASKS.includes(t.task));
  return catalog.filter((t) => t.category === category.key);
}

/** The tabs to show: every one the server has a task for, except the disabled ones. */
export function shownCategories(catalog: CatalogTask[]): Category[] {
  return CATEGORIES.filter((c) => !c.disabled && tasksIn(c, catalog).length > 0);
}

/** The tab a link to ``task`` opens: Chat for a chat task, else the task's own; null when the catalog lacks the task or its tab is disabled. */
export function categoryOf(task: string, catalog: CatalogTask[]): CategoryKey | null {
  if (CHAT_TASKS.includes(task)) return "chat";
  const home = catalog.find((t) => t.task === task)?.category;
  return home && !CATEGORIES.find((c) => c.key === home)?.disabled ? home : null;
}

/** An entry of a tab's task switch: a task with its variants (the same job, done by another kind of model). */
export interface TaskGroup {
  /** The task the others vary */
  key: string;
  label: string;
  tasks: CatalogTask[];
}

/** ``category``'s task switch: its tasks gathered under the group each names (the catalog's ``group``), in order. */
export function groupsIn(category: Category, catalog: CatalogTask[]): TaskGroup[] {
  const groups = new Map<string, TaskGroup>();
  for (const t of tasksIn(category, catalog)) {
    const group = groups.get(t.group) ?? { key: t.group, label: t.group_label, tasks: [] };
    group.tasks.push(t);
    groups.set(t.group, group);
  }
  return [...groups.values()];
}

/** A model, and the task it is chosen for. */
export interface Choice {
  id: string;
  task: string;
}

/** A model in a picker's list: ``task`` when the list mixes tasks (a group's), ``badge`` naming a variant's models. */
export interface PickerEntry {
  id: string;
  task?: string;
  status?: CatalogModel["status"];
  badge?: string;
}

/** ``choice`` as an entry of ``group``'s list: the listed one, else (a model chosen with Browse) one made for it. */
export function entryFor(group: TaskGroup, choice: Choice): PickerEntry {
  const listed = choicesOf(group).find((e) => e.id === choice.id && e.task === choice.task);
  const variant = choice.task === group.key ? undefined : group.tasks.find((t) => t.task === choice.task);
  return listed ?? { ...choice, badge: variant?.label };
}

/**
 * ``group``'s models, each with the task it is chosen for: loaded first, then downloaded, then the rest. A
 * variant's models carry its label as a badge; a model listed under two of the tasks is in the list twice.
 */
export function choicesOf(group: TaskGroup): PickerEntry[] {
  // Each model object belongs to one task's list, so it finds its task again after filterModels sorts them.
  const taskOf = new Map<CatalogModel, CatalogTask>();
  for (const t of group.tasks) for (const m of t.models) taskOf.set(m, t);
  return filterModels([...taskOf.keys()], { query: "", status: "all", tag: "" }).map((m) => {
    const t = taskOf.get(m)!;
    return { id: m.id, task: t.task, status: m.status, badge: t.task === group.key ? undefined : t.label };
  });
}

/** The tab, each tab's entry in its task switch (a group's key), each task's model, and each group's task. */
export interface TabPrefs {
  category: CategoryKey;
  tasks: Partial<Record<CategoryKey, string>>;
  models: Record<string, string>;
  /** The task last chosen in each group (missing: the group's own); prefs stored before groups have none */
  groups?: Record<string, string>;
}

/**
 * The model chosen for ``group``: the last one picked in it, else its own task's last model (prefs stored before
 * groups, or a picked variant since gone), else the first listed.
 */
export function choiceIn(group: TaskGroup, prefs: TabPrefs): Choice | null {
  for (const task of [prefs.groups?.[group.key], group.key]) {
    const id = task && group.tasks.some((t) => t.task === task) ? prefs.models[task] : undefined;
    if (task && id) return { id, task };
  }
  const first = choicesOf(group)[0];
  return first ? { id: first.id, task: first.task! } : null;
}

/** The task switch's entry ``stored`` names: a group's key, or (stored before groups) a task in a group; else the first. */
export function groupFor(groups: TaskGroup[], stored: string | undefined): TaskGroup {
  return groups.find((g) => g.key === stored) ?? groups.find((g) => g.tasks.some((t) => t.task === stored)) ?? groups[0];
}

/** ``prefs`` with ``choice`` picked in ``group``. */
export function withChoice(prefs: TabPrefs, group: string, choice: Choice): TabPrefs {
  return { ...prefs, models: { ...prefs.models, [choice.task]: choice.id }, groups: { ...prefs.groups, [group]: choice.task } };
}

/** A link to a model of a task: #/playground?model=ID&task=TASK. */
export interface TaskLink {
  model: string;
  task: string;
}

/** ``prefs`` opened on ``link``'s model, in its task's tab and group; as they were when the catalog has no tab for it. */
export function withLink(prefs: TabPrefs, link: TaskLink, catalog: CatalogTask[]): TabPrefs {
  const key = categoryOf(link.task, catalog);
  const group = catalog.find((t) => t.task === link.task)?.group;
  if (!key || !group) return prefs;
  return { ...withChoice(prefs, group, { id: link.model, task: link.task }), category: key, tasks: { ...prefs.tasks, [key]: group } };
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
