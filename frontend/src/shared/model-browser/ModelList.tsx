import type { FormEvent } from "react";
import { ArrowRight, FilterX, PackageOpen, Search, SearchX } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";
import { Field, Segmented } from "../components";
import {
  filterModels,
  otherIdProblem,
  tagsOf,
  type CatalogModel,
  type CatalogTask,
  type ModelFilter,
  type StatusFilter,
} from "./catalog";
import { diskText, EmptyState, StateBadge, Tags } from "./parts";

const STATUS: [StatusFilter, string][] = [
  ["all", "All"],
  ["loaded", "Loaded"],
  ["on_disk", "Downloaded"],
];

/**
 * One task's models. Four zones, top to bottom: the task, the search and
 * filters, the cards (the only part that scrolls), and a box for an ID
 * nobody lists.
 */
export function ModelList(props: {
  task: CatalogTask;
  filter: ModelFilter;
  onFilter(filter: ModelFilter): void;
  otherId: string;
  onOtherId(id: string): void;
  /** What choosing a card does, e.g. "Details" or "Use in column A" */
  action: string;
  onChoose(id: string): void;
}) {
  const { task, filter, onFilter, action } = props;
  const tags = tagsOf(task.models);
  const shown = filterModels(task.models, filter);
  const filtered = Boolean(filter.query || filter.status !== "all" || filter.tag);
  const otherId = props.otherId.trim();
  const problem = otherIdProblem(task, otherId);
  const canTypeId = task.any_id || task.patterns.length > 0;

  const submitOther = (event: FormEvent) => {
    event.preventDefault();
    if (!problem) props.onChoose(otherId);
  };

  return (
    <section className="flex min-h-0 min-w-0 flex-1 flex-col" aria-label={task.task}>
      <div className="flex flex-none items-start justify-between gap-3 px-5 pt-4 pb-3">
        <div className="min-w-0">
          <h3 className="text-base font-semibold tracking-tight">{task.task}</h3>
          {task.description && <p className="mt-1 text-sm text-muted-foreground">{task.description}</p>}
        </div>
        <span className="pt-0.5 text-sm whitespace-nowrap text-muted-foreground tabular-nums">
          {filtered ? `${shown.length} of ${task.models.length}` : `${task.models.length}`} model{task.models.length === 1 ? "" : "s"}
        </span>
      </div>

      {task.models.length > 0 && (
        <div className="grid flex-none gap-2.5 border-b px-5 pb-3">
          <div className="flex flex-wrap items-center gap-2">
            <div className="relative flex-[1_1_240px]">
              <Search className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-muted-foreground" aria-hidden="true" />
              <Input
                type="search"
                className="h-8 pl-8"
                placeholder="Search by name, description or tag"
                aria-label="Search models"
                value={filter.query}
                autoFocus
                onChange={(e) => onFilter({ ...filter, query: e.target.value })}
              />
            </div>
            <Segmented label="Status" size="sm" options={STATUS} value={filter.status} onChange={(status) => onFilter({ ...filter, status })} />
          </div>
          {tags.length > 0 && (
            <div className="flex flex-wrap items-center gap-1.5" role="group" aria-label="Tag">
              <span className="mr-0.5 text-xs text-muted-foreground">Tags</span>
              {tags.map((t) => {
                const on = filter.tag === t;
                return (
                  <button
                    key={t}
                    type="button"
                    aria-pressed={on}
                    onClick={() => onFilter({ ...filter, tag: on ? "" : t })}
                    className={cn(
                      "rounded-full border px-2.5 py-0.5 text-xs text-muted-foreground outline-none transition-[background-color,color,scale] hover:bg-accent hover:text-foreground focus-visible:ring-[3px] focus-visible:ring-ring/50 active:scale-95",
                      on && "border-transparent bg-primary text-primary-foreground hover:bg-primary/90 hover:text-primary-foreground",
                    )}
                  >
                    {t}
                  </button>
                );
              })}
            </div>
          )}
        </div>
      )}

      <div className="min-h-0 flex-1 overflow-y-auto px-5 pt-4 pb-5">
        {shown.length ? (
          <div className="grid grid-cols-[repeat(auto-fill,minmax(min(100%,270px),1fr))] gap-2.5">
            {shown.map((m) => (
              <ModelCard key={m.id} model={m} action={action} onClick={() => props.onChoose(m.id)} />
            ))}
          </div>
        ) : task.models.length ? (
          <EmptyState icon={SearchX}>
            <p>No model matches these filters.</p>
            <Button type="button" variant="outline" size="sm" onClick={() => onFilter({ query: "", status: "all", tag: "" })}>
              <FilterX aria-hidden="true" />
              Clear filters
            </Button>
          </EmptyState>
        ) : (
          <EmptyState icon={PackageOpen}>
            <p>
              No models are listed for {task.task}.{canTypeId ? " Enter an ID below." : ""}
            </p>
          </EmptyState>
        )}
      </div>

      {canTypeId && (
        <form className="flex flex-none flex-wrap items-end gap-x-2.5 gap-y-1.5 border-t bg-muted/40 px-5 py-3" onSubmit={submitOther}>
          <Field
            className="flex-[1_1_260px]"
            label={task.any_id ? "Not listed? Any Hub ID or local path" : `Not listed? An ID matching ${task.patterns.join(" or ")}`}
          >
            <Input
              className="bg-background font-mono"
              value={props.otherId}
              placeholder={task.any_id ? "org/model" : task.patterns[0].replace("*", "name")}
              autoComplete="off"
              spellCheck={false}
              aria-invalid={Boolean(otherId && problem) || undefined}
              onChange={(e) => props.onOtherId(e.target.value)}
            />
          </Field>
          <Button type="submit" disabled={Boolean(problem)} title={otherId ? problem : ""}>
            {action}
            <ArrowRight aria-hidden="true" />
          </Button>
          {otherId && problem && <p className="basis-full text-xs text-critical-text">{problem}</p>}
        </form>
      )}
    </section>
  );
}

/**
 * A model as a card: name and state, what it is, then its tags and where its weights are. It rises in like
 * the page's blocks; zooming in, its text jumped a pixel as the zoom settled.
 */
function ModelCard({ model, action, onClick }: { model: CatalogModel; action: string; onClick(): void }) {
  const disk = diskText(model);
  return (
    <button
      type="button"
      onClick={onClick}
      title={`${action}: ${model.id}`}
      className="flex min-w-0 animate-rise flex-col gap-2 rounded-xl border bg-card px-3.5 pt-3.5 pb-3 text-left shadow-xs outline-none transition-[translate,scale,box-shadow,border-color] duration-200 hover:-translate-y-0.5 hover:border-axis hover:shadow-md focus-visible:-translate-y-0.5 focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50 active:translate-y-0 active:scale-[0.99]"
    >
      <span className="flex items-baseline justify-between gap-2">
        <span className="font-mono text-[13px] font-semibold wrap-anywhere">{model.id}</span>
        <StateBadge model={model} />
      </span>
      <span className="flex-1 text-sm text-muted-foreground">
        {model.description || <span className="text-muted-foreground/70 italic">No description</span>}
      </span>
      <span className="flex flex-wrap items-center justify-between gap-1.5 border-t border-grid pt-2">
        <Tags tags={model.tags} />
        {disk && <span className="ml-auto text-xs whitespace-nowrap text-muted-foreground tabular-nums">{disk}</span>}
      </span>
    </button>
  );
}
