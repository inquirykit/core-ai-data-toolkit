import { useEffect, useMemo, useState } from "react";
import { AudioLines, Image, MessagesSquare, Puzzle, Scale, TrendingUp, Type, type LucideIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { createClient, type ApiClient } from "../shared/api";
import { LoginForm, Notice, type NoticeState } from "../shared/components";
import type { CatalogTask } from "../shared/model-browser/catalog";
import { ADMIN_KEY_STORAGE, useSession } from "../shared/session";
import { errorText } from "../shared/query";
import { PageMain, Shell } from "../shared/Shell";
import { readLocal, readSession, writeLocal } from "../shared/storage";
import type { Limits } from "../shared/types";
import { CATEGORIES, categoryOf, modelsOf, shownCategories, tasksIn, type Category, type CategoryKey } from "./categories";
import { ChatView } from "./ChatView";
import { TaskView } from "./TaskView";

const KEY_STORAGE = "ai-toolkit-playground-key";
const TABS_STORAGE = "ai-toolkit-playground-tabs";

const ICONS: Record<CategoryKey, LucideIcon> = {
  chat: MessagesSquare,
  vision: Image,
  text: Type,
  audio: AudioLines,
  decision: Scale,
  prediction: TrendingUp,
  other: Puzzle,
};

/** The tab, each category's task, and each task's model (the chat's own choices are ChatView's). */
interface TabPrefs {
  category: CategoryKey;
  tasks: Partial<Record<CategoryKey, string>>;
  models: Record<string, string>;
}

const isCategory = (key: unknown): key is CategoryKey => CATEGORIES.some((c) => c.key === key);

// Until /playground/limits answers.
const DEFAULT_LIMITS: Limits = { max_models: 3, max_generation_length: 512, max_input_chars: 10000 };

/** The tabs to open on, from storage and the address; ``chatLink`` is a model for the chat's column A. */
function openingPrefs(): { prefs: TabPrefs; chatLink: string | null; linked: boolean } {
  const stored = readLocal<TabPrefs>(TABS_STORAGE);
  const prefs: TabPrefs = {
    category: isCategory(stored.category) ? stored.category : "chat",
    tasks: { ...stored.tasks },
    models: { ...stored.models },
  };
  // /playground?model=ID[&task=TASK] (the admin's model browser links here) opens on that model.
  const params = new URLSearchParams(location.search);
  const model = params.get("model");
  const task = params.get("task") ?? "text-generation";
  const linkedCategory = model ? categoryOf(task) : null;
  let chatLink: string | null = null;
  if (model && linkedCategory === "chat") {
    prefs.category = "chat";
    chatLink = model;
  } else if (model && linkedCategory) {
    prefs.category = linkedCategory;
    prefs.tasks[linkedCategory] = task;
    prefs.models[task] = model;
  }
  // #text and the like name the tab to open on.
  const hashed = location.hash.slice(1);
  if (!model && isCategory(hashed)) prefs.category = hashed;
  return { prefs, chatLink, linked: Boolean(model) };
}

export function PlaygroundApp() {
  // The admin key is accepted by the playground's routes, so a signed-in admin needs no second key.
  // Requests from here are tagged, so the requests table of /admin#/activity can filter them.
  const session = useSession(KEY_STORAGE, [ADMIN_KEY_STORAGE], { "X-Trace-Tag": "playground" });
  const { client } = session;
  // The chat's admin-only calls (adding to a dataset) send the same key, but a 401 there
  // means "needs the admin key", not "sign in again": this client leaves the form alone.
  const quiet = useMemo(
    () => createClient(() => [KEY_STORAGE, ADMIN_KEY_STORAGE].map(readSession).find(Boolean) ?? "", () => {}, { "X-Trace-Tag": "playground" }),
    [],
  );
  const [opening] = useState(openingPrefs);
  const [prefs, setPrefs] = useState<TabPrefs>(opening.prefs);

  // Start (and start over after each sign-in, which loads every query again): the limits, then the catalog.
  const limitsLoad = client.useQuery("get", "/playground/limits");
  const catalogLoad = client.useQuery("get", "/models/catalog", undefined, { select: (d) => d.tasks, enabled: limitsLoad.data !== undefined });
  const limits = limitsLoad.data ?? DEFAULT_LIMITS;
  const catalog = catalogLoad.data ?? [];
  const started = limitsLoad.data !== undefined && catalogLoad.data !== undefined;
  // After a run the model may be loaded, so the catalog is read again (onRan); if that fails, the old statuses will do.
  const failure = errorText(limitsLoad.error) || (catalogLoad.data ? "" : errorText(catalogLoad.error));
  const ready = started || Boolean(failure);
  const notice: NoticeState = failure ? { text: `Could not reach the server: ${failure}`, kind: "error" } : { text: "" };

  // Once started, the model the address names is dropped from it.
  useEffect(() => {
    if (started && opening.linked) history.replaceState(null, "", location.pathname);
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
    history.replaceState(null, "", `${location.pathname}#${key}`);
  };

  return (
    <Shell
      active="playground"
      footer={
        ready &&
        !session.login &&
        session.hasKey && (
          <Button type="button" variant="outline" size="sm" onClick={session.signOut}>
            Sign out
          </Button>
        )
      }
    >
      {session.login ? (
        <LoginForm
          title="API key"
          description={
            <>
              Enter <code>API_KEY</code> (the admin key works too). It is kept for this browser tab only.
            </>
          }
          submitLabel="Open playground"
          message={session.login.message}
          onSubmit={session.signIn}
        />
      ) : (
        ready &&
        (active ? (
          <PageMain className="flex-1 pb-0 lg:pb-0">
            <Tabs value={active.key} onValueChange={selectCategory} className="gap-5">
              <CategoryTabs categories={shown} />
              {shown.map((category) => (
                <TabsContent
                  key={category.key}
                  value={category.key}
                  // Every tab stays mounted, so a chat or a form survives a visit elsewhere.
                  forceMount
                  className={
                    category.key === "chat"
                      ? "grid grid-rows-[auto_1fr_auto] gap-5 data-[state=inactive]:hidden"
                      : "pb-6 data-[state=inactive]:hidden"
                  }
                >
                  {category.key === "chat" ? (
                    <ChatView
                      key={session.generation}
                      client={client}
                      quiet={quiet}
                      limits={limits}
                      models={modelsOf(tasksIn(category, catalog))}
                      linked={opening.chatLink}
                    />
                  ) : (
                    <CategoryView
                      category={category}
                      catalog={catalog}
                      prefs={prefs}
                      setPrefs={setPrefs}
                      client={client}
                      onRan={() => void catalogLoad.refetch()}
                    />
                  )}
                </TabsContent>
              ))}
            </Tabs>
            <Notice notice={notice} />
          </PageMain>
        ) : (
          <PageMain>
            <Notice notice={notice.text ? notice : { text: "The server offers no tasks to try." }} />
          </PageMain>
        ))
      )}
    </Shell>
  );
}

/** The categories across the top. */
function CategoryTabs({ categories }: { categories: Category[] }) {
  return (
    <TabsList aria-label="Category" className="h-10 max-w-full justify-start overflow-x-auto">
      {categories.map((c) => {
        const Icon = ICONS[c.key];
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

/** One category's TaskView, with its task and model choices kept in ``prefs``. */
function CategoryView(props: {
  category: Category;
  catalog: CatalogTask[];
  prefs: TabPrefs;
  setPrefs(update: (p: TabPrefs) => TabPrefs): void;
  client: ApiClient;
  onRan(): void;
}) {
  const { category, prefs, setPrefs } = props;
  const tasks = tasksIn(category, props.catalog);
  const task = tasks.find((t) => t.task === prefs.tasks[category.key]) ?? tasks[0];
  // The model last used for the task, else its first listed (loaded first).
  const model = prefs.models[task.task] ?? modelsOf([task])[0]?.id ?? "";
  return (
    <TaskView
      client={props.client}
      tasks={tasks}
      task={task.task}
      onTask={(next) => setPrefs((p) => ({ ...p, tasks: { ...p.tasks, [category.key]: next } }))}
      model={model}
      onModel={(id) => setPrefs((p) => ({ ...p, models: { ...p.models, [task.task]: id } }))}
      onRan={props.onRan}
    />
  );
}
