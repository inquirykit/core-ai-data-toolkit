import { useEffect, useId, useRef, useState } from "react";
import { CircleAlert, Inbox, Loader2 } from "lucide-react";
import { motion } from "motion/react";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { cn } from "@/lib/utils";
import type { ApiClient } from "../api";
import { errorText, useReloadOn } from "../query";
import { defaultTask, type CatalogTask, type ModelFilter } from "./catalog";
import { ModelDetails } from "./ModelDetails";
import { ModelList } from "./ModelList";
import { EmptyState, type PickedModel } from "./parts";

export type { PickedModel } from "./parts";

const NO_FILTER: ModelFilter = { query: "", status: "all", tag: "" };

interface BrowseOptions {
  client: ApiClient;
  /** Only these tasks are offered (every task when unset). */
  tasks?: string[];
  /** The task to open on. */
  task?: string;
  pick?: { label: string; onPick(model: PickedModel): void };
  load?: { onChange(): void };
}

/**
 * Finding a model (GET /models/catalog): the tasks on the left, and for the
 * chosen task its models as cards (ModelList), with a search, filters, and
 * a box for any other ID the task takes.
 *
 * With ``pick``, choosing a model hands it back (then ``onDone``). With
 * ``load`` (admin), choosing one opens its details in place of the list
 * (ModelDetails): what loading it would take and do, and Load or Unload.
 * A change of ``refreshKey`` (say, the loaded models) fetches the catalog again.
 */
export function ModelBrowserPanel(props: BrowseOptions & { onDone?(): void; refreshKey?: string }) {
  const { client, pick, load } = props;
  const listing = client.useQuery("get", "/models/catalog", undefined, {
    select: (d) => d.tasks.filter((t) => !props.tasks || props.tasks.includes(t.task)),
  });
  useReloadOn(props.refreshKey, listing.refetch);
  const catalog = listing.data;
  const listError = errorText(listing.error);
  const [chosen, setChosen] = useState(props.task ?? "");
  // The task chosen, or the default one while the catalog has no such task.
  const task = catalog?.some((t) => t.task === chosen) ? chosen : defaultTask(catalog ?? []);
  // Kept here, so they survive a visit to a model's details.
  const [filter, setFilter] = useState<ModelFilter>(NO_FILTER);
  const [otherId, setOtherId] = useState("");
  const [selected, setSelected] = useState<PickedModel | null>(null);

  function selectTask(next: string) {
    setChosen(next);
    setSelected(null);
    // Tags differ from task to task.
    setFilter((f) => ({ ...f, tag: "" }));
  }

  function choose(id: string) {
    if (load) setSelected({ id, task });
    else if (pick) {
      pick.onPick({ id, task });
      props.onDone?.();
    }
  }

  if (listError) {
    return (
      <p className="flex items-start gap-2 p-5 text-sm text-critical-text">
        <CircleAlert className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
        Could not list the models: {listError}
      </p>
    );
  }
  if (!catalog) {
    return (
      <p className="flex items-center gap-2 p-5 text-sm text-muted-foreground">
        <Loader2 className="size-4 animate-spin" aria-hidden="true" />
        Loading the catalog…
      </p>
    );
  }
  const current = catalog.find((t) => t.task === task);
  return (
    // A container, so the layout follows the room it has (a dialog, or a card next to the menu), not the window.
    <div className="@container flex min-h-0 flex-1 flex-col">
      <div className="flex min-h-0 flex-1 flex-col @xl:flex-row">
        <TaskNav tasks={catalog} current={task} onSelect={selectTask} />
        {current && selected && load ? (
          <ModelDetails
            client={client}
            picked={selected}
            model={current.models.find((m) => m.id === selected.id)}
            pick={pick}
            onBack={() => setSelected(null)}
            onChange={() => {
              void listing.refetch();
              load.onChange();
            }}
            onDone={() => props.onDone?.()}
          />
        ) : current ? (
          <ModelList
            task={current}
            filter={filter}
            onFilter={setFilter}
            otherId={otherId}
            onOtherId={setOtherId}
            action={load ? "Details" : (pick?.label ?? "Use")}
            onChoose={choose}
          />
        ) : (
          <EmptyState icon={Inbox} className="flex-1">
            <p>No tasks to show.</p>
          </EmptyState>
        )}
      </div>
    </div>
  );
}

/** ModelBrowserPanel in a dialog, for choosing a model without leaving the page. */
export function ModelBrowser(props: BrowseOptions & { open: boolean; onClose(): void }) {
  const { open, onClose } = props;
  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      {/* A fixed height, so the dialog does not jump as tasks and filters change what it holds. */}
      <DialogContent className="flex h-[min(88vh,780px)] w-[min(1040px,calc(100%-2rem))] max-w-none flex-col gap-0 overflow-hidden p-0 sm:max-w-none max-sm:h-[calc(100svh-2rem)] max-sm:w-[calc(100%-1rem)]">
        <DialogHeader className="flex-row items-baseline gap-3 border-b py-4 pr-12 pl-5 text-left">
          <DialogTitle className="text-base">Models</DialogTitle>
          <DialogDescription className="truncate max-sm:sr-only">
            {props.load ? "Load and unload models, and see what fits" : "Choose a model"}
          </DialogDescription>
        </DialogHeader>
        {/* The content is mounted only while open, so each opening shows current statuses. */}
        <ModelBrowserPanel {...props} onDone={onClose} />
      </DialogContent>
    </Dialog>
  );
}

/** The tasks, down the side (across the top on a narrow screen). */
function TaskNav(props: { tasks: CatalogTask[]; current: string; onSelect(task: string): void }) {
  const current = useRef<HTMLButtonElement>(null);
  // Across the top on a narrow screen, the chosen task may start out of view.
  useEffect(() => {
    // A block, not an expression: newer browsers return a Promise, which React would take for a cleanup.
    current.current?.scrollIntoView({ block: "nearest", inline: "nearest" });
  }, [props.current]);
  // Each panel its own pill, so two panels never pass it between them.
  const pill = useId();
  return (
    <nav
      className="flex shrink-0 gap-0.5 overflow-x-auto border-b bg-muted/40 p-2 @xl:w-[210px] @xl:flex-col @xl:overflow-x-visible @xl:overflow-y-auto @xl:border-r @xl:border-b-0"
      aria-label="Tasks"
    >
      {props.tasks.map((t) => {
        const active = t.task === props.current;
        return (
          <button
            key={t.task}
            ref={active ? current : undefined}
            type="button"
            aria-current={active ? "true" : undefined}
            title={t.description}
            onClick={() => props.onSelect(t.task)}
            className={cn(
              "relative flex flex-none items-center justify-between gap-3 rounded-md px-2.5 py-1.5 text-left text-sm whitespace-nowrap text-muted-foreground outline-none transition-colors hover:bg-accent hover:text-foreground focus-visible:ring-[3px] focus-visible:ring-ring/50 @xl:w-full",
              active && "font-medium text-foreground hover:bg-transparent",
            )}
          >
            {active && (
              <motion.span
                layoutId={`task-pill-${pill}`}
                className="absolute inset-0 rounded-md bg-background shadow-xs ring-1 ring-border dark:bg-input/30"
                transition={{ type: "spring", stiffness: 500, damping: 38 }}
              />
            )}
            <span className="relative truncate">{t.task}</span>
            <span className="relative text-xs font-normal text-muted-foreground tabular-nums">{t.models.length}</span>
          </button>
        );
      })}
    </nav>
  );
}
