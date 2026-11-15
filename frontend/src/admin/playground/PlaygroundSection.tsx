// The Playground section: a tab per category of task (shared/categories.ts).
// Chat talks to one model, or sends the same chat to several and compares
// their replies; the others run one model of the chosen task. The tab is in
// the hash (#/playground/text), and #/playground?model=ID[&task=TASK], which
// the model browser links to, opens on that model.
import { useEffect, useLayoutEffect, useMemo, useState } from "react";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { ApiProvider, useApi } from "../../shared/api";
import { Boundary } from "../../shared/boundary";
import { parseHash, useHash } from "../../shared/browser";
import { CATEGORIES, type Category, type CategoryKey } from "../../shared/categories";
import { Notice, type NoticeState } from "../../shared/components";
import { CHAT_TASKS, type CatalogTask } from "../../shared/model-browser/catalog";
import { errorText } from "../../shared/query";
import { readLocal, writeLocal } from "../../shared/storage";
import type { Limits } from "../../shared/types";
import { choiceIn, groupFor, groupsIn, modelsOf, shownCategories, tasksIn, withChoice, withLink, type TabPrefs, type TaskLink } from "./categories";
import { ChatView } from "./ChatView";
import { TaskView } from "./TaskView";

const TABS_STORAGE = "ai-toolkit-playground-tabs";

const isCategory = (key: unknown): key is CategoryKey => CATEGORIES.some((c) => c.key === key);

// Until /playground/limits answers.
const DEFAULT_LIMITS: Limits = { max_models: 3, max_generation_length: 512, max_input_chars: 10000 };

/** The model a link in the hash names, with its task, as one string that changes with them; "" for none. */
const linkIn = (hash: string): string => {
  const params = parseHash(hash).params;
  return params.has("model") ? params.toString() : "";
};

/**
 * The tabs to open on, from storage and the hash; ``chatLink`` is a model for the chat's column A, ``taskLink``
 * one for another tab, which only the catalog can name (withLink).
 */
function openingPrefs(): { prefs: TabPrefs; chatLink: string | null; taskLink: TaskLink | null; linked: boolean } {
  const stored = readLocal<TabPrefs>(TABS_STORAGE);
  const prefs: TabPrefs = {
    category: isCategory(stored.category) ? stored.category : "chat",
    tasks: { ...stored.tasks },
    models: { ...stored.models },
    groups: { ...stored.groups },
  };
  const { parts, params } = parseHash(location.hash);
  const model = params.get("model");
  const task = params.get("task") ?? "text-generation";
  let chatLink: string | null = null;
  let taskLink: TaskLink | null = null;
  if (model && CHAT_TASKS.includes(task)) {
    prefs.category = "chat";
    chatLink = model;
  } else if (model) {
    taskLink = { model, task };
  }
  // #/playground/text and the like name the tab to open on.
  if (!model && isCategory(parts[1])) prefs.category = parts[1];
  return { prefs, chatLink, taskLink, linked: Boolean(model) };
}

/** A link followed while the section shows opens it afresh, on the model the link names. */
export function PlaygroundSection() {
  const link = useHash(linkIn);
  return <Playground key={link} />;
}

function Playground() {
  // Its requests are tagged, so the requests table of Activity & logs can filter them.
  const admin = useApi();
  const client = useMemo(() => admin.with({ "X-Trace-Tag": "playground" }), [admin]);
  const [opening] = useState(openingPrefs);
  const [prefs, setPrefs] = useState<TabPrefs>(opening.prefs);

  // The limits, then the catalog.
  const limitsLoad = client.useQuery("get", "/playground/limits");
  const catalogLoad = client.useQuery("get", "/models/catalog", undefined, { select: (d) => d.tasks, enabled: limitsLoad.data !== undefined });
  const limits = limitsLoad.data ?? DEFAULT_LIMITS;
  const catalog = catalogLoad.data ?? [];
  const started = limitsLoad.data !== undefined && catalogLoad.data !== undefined;
  // After a run the model may be loaded, so the catalog is read again (onRan); if that fails, the old statuses will do.
  const failure = errorText(limitsLoad.error) || (catalogLoad.data ? "" : errorText(catalogLoad.error));
  const ready = started || Boolean(failure);
  const notice: NoticeState = failure ? { text: `Could not reach the server: ${failure}`, kind: "error" } : { text: "" };

  // A link to a model outside the chat opens in its task's tab once the catalog says which that is: before the
  // section is first drawn, so the stored tab never shows first.
  const [taskLink, setTaskLink] = useState(opening.taskLink);
  useLayoutEffect(() => {
    if (!started || !taskLink) return;
    setPrefs((p) => withLink(p, taskLink, catalog));
    setTaskLink(null);
  }, [started, taskLink, catalog]);

  // Once started, the model the hash names is dropped from it.
  useEffect(() => {
    if (started && opening.linked) history.replaceState(null, "", "#/playground");
  }, [started, opening]);

  useEffect(() => {
    if (ready) writeLocal(TABS_STORAGE, prefs);
  }, [ready, prefs]);

  const shown = shownCategories(catalog);
  // The chosen tab, or the first one if the server has nothing for it.
  const active = shown.find((c) => c.key === prefs.category) ?? shown[0];

  const selectCategory = (key: string) => {
    if (!isCategory(key)) return;
    setPrefs((p) => ({ ...p, category: key }));
    history.replaceState(null, "", `#/playground/${key}`);
  };

  if (!ready) return null;
  if (!active) return <Notice notice={notice.text ? notice : { text: "The server offers no tasks to try." }} />;
  return (
    <ApiProvider value={client}>
      <Tabs value={active.key} onValueChange={selectCategory} className="gap-5">
        <CategoryTabs categories={shown} />
        {shown.map((category) => (
          <TabsContent
            key={category.key}
            value={category.key}
            // Every tab stays mounted, so a chat or a form survives a visit to another tab.
            forceMount
            className={
              category.key === "chat" ? "grid grid-rows-[auto_1fr_auto] gap-5 data-[state=inactive]:hidden" : "pb-6 data-[state=inactive]:hidden"
            }
          >
            {/* Every tab is drawn, shown or not, so one that throws would take the others with it. */}
            <Boundary what={category.label}>
              {category.key === "chat" ? (
                <ChatView limits={limits} models={modelsOf(tasksIn(category, catalog))} linked={opening.chatLink} />
              ) : (
                <CategoryView category={category} catalog={catalog} prefs={prefs} setPrefs={setPrefs} onRan={() => void catalogLoad.refetch()} />
              )}
            </Boundary>
          </TabsContent>
        ))}
      </Tabs>
      <Notice notice={notice} />
    </ApiProvider>
  );
}

/** The categories across the top. */
function CategoryTabs({ categories }: { categories: Category[] }) {
  return (
    <TabsList aria-label="Category" className="h-10 max-w-full justify-start overflow-x-auto">
      {categories.map((c) => {
        const Icon = c.icon;
        return (
          <TabsTrigger key={c.key} value={c.key} title={c.blurb} className="flex-none px-3">
            <Icon aria-hidden="true" />
            {c.label}
          </TabsTrigger>
        );
      })}
    </TabsList>
  );
}

/** One tab's TaskView, with its task switch's entry and each entry's model kept in ``prefs``. */
function CategoryView(props: {
  category: Category;
  catalog: CatalogTask[];
  prefs: TabPrefs;
  setPrefs(update: (p: TabPrefs) => TabPrefs): void;
  onRan(): void;
}) {
  const { category, prefs, setPrefs } = props;
  const groups = groupsIn(category, props.catalog);
  const group = groupFor(groups, prefs.tasks[category.key]);
  return (
    <TaskView
      groups={groups}
      group={group}
      onGroup={(key) => setPrefs((p) => ({ ...p, tasks: { ...p.tasks, [category.key]: key } }))}
      choice={choiceIn(group, prefs)}
      onChoice={(choice) => setPrefs((p) => withChoice(p, group.key, choice))}
      onRan={props.onRan}
    />
  );
}
