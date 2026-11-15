import { lazy, Suspense } from "react";
import { Plus, Search } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useApi, ApiError } from "../../shared/api";
import { useHash } from "../../shared/browser";
import { EmptyCard } from "../../shared/components";
import { fmtAgo, fmtClock } from "../../shared/format";
import { DataGrid } from "../../shared/grid";
import { errorText } from "../../shared/query";
import type { WorkflowInfo } from "../../shared/types";
import { DetailSkeleton, LoadedCard, Pager, useCursorPages } from "../layout";
import { locationFromHash, workflowHash, type WorkflowLocation } from "./workflows";
import { describeCadence } from "../../shared/tasks";

// The editor and its canvas (React Flow) load the first time one opens, so the list costs the page nothing more.
const WorkflowEditor = lazy(() => import("./WorkflowEditor").then((m) => ({ default: m.WorkflowEditor })));

/** The workflows a page of the list shows. */
const PAGE = 50;

/**
 * /admin#/workflows: the workflows, and one opened (#/workflows/<id>, or
 * #/workflows/new) in its editor, a canvas of its nodes and edges with its
 * dry run and its runs.
 */
export function WorkflowsSection() {
  const loc = useHash(locationFromHash);
  return loc.view === "list" ? <WorkflowsList /> : <OpenWorkflow key={loc.view === "edit" ? loc.id : "new"} loc={loc} />;
}

function WorkflowsList() {
  const client = useApi();
  const pages = useCursorPages(null);
  const list = client.useQuery("get", "/workflows", { params: { query: { limit: PAGE, cursor: pages.cursor } } });
  const data = list.data ?? null;
  return (
    <LoadedCard
      title="Workflows"
      description={(d: NonNullable<typeof data>) => (d.workflows.length ? "Open one to edit its graph, try it on a few rows, or run it" : "None yet")}
      data={data}
      error={errorText(list.error)}
      onRefresh={list.refetch}
      actions={
        <Button type="button" size="sm" asChild>
          <a href={workflowHash({ view: "new" })}>
            <Plus aria-hidden="true" />
            New workflow
          </a>
        </Button>
      }
      after={
        data && (pages.hasPrevious || data.next) ? (
          <Pager
            start={pages.index * PAGE}
            shown={data.workflows.length}
            count={null}
            hasPrevious={pages.hasPrevious}
            hasNext={Boolean(data.next)}
            loading={list.isFetching}
            onPrevious={pages.previous}
            onNext={() => pages.next(data.next ?? undefined)}
          />
        ) : null
      }
    >
      {(d) => (
        <DataGrid<WorkflowInfo>
          aria-label="Workflows"
          rows={d.workflows}
          rowKey={(w) => w.id}
          rowHref={(w) => workflowHash({ view: "edit", id: w.id })}
          empty="No workflows yet: make one with New workflow."
          columns={[
            {
              key: "name",
              label: "Name",
              render: (w) => (
                <>
                  <div className="font-medium">{w.name}</div>
                  {w.description && <div className="max-w-md truncate text-xs text-muted-foreground">{w.description}</div>}
                </>
              ),
            },
            { key: "nodes", label: "Nodes", align: "right", render: (w) => w.nodes.length },
            { key: "trigger", label: "Trigger", render: (w) => (w.trigger ? describeCadence({ cron: w.trigger.cron ?? null, interval_s: w.trigger.interval_s ?? null }) : "by hand") },
            { key: "updated", label: "Changed", title: (w) => fmtClock(w.updated, true), render: (w) => fmtAgo(w.updated) },
          ]}
        />
      )}
    </LoadedCard>
  );
}

/** A workflow's editor, once its node types (and, for a saved one, its spec) are here. */
function OpenWorkflow({ loc }: { loc: Exclude<WorkflowLocation, { view: "list" }> }) {
  const client = useApi();
  const id = loc.view === "edit" ? loc.id : "";
  const types = client.useQuery("get", "/workflows/nodes", undefined, { select: (d) => d.nodes, staleTime: Infinity });
  const saved = client.useQuery("get", "/workflows/{id}", { params: { path: { id } } }, { enabled: Boolean(id) });
  const error = errorText(types.error) || errorText(saved.error);
  if (saved.error instanceof ApiError && saved.error.status === 404) {
    return <EmptyCard icon={Search} title="No such workflow" body="It may have been deleted. Go back to the workflows to pick another." />;
  }
  if (error) {
    return (
      <LoadedCard title="Workflow" data={null} error={error}>
        {() => null}
      </LoadedCard>
    );
  }
  // A saved one is drawn from its own spec, not from one asked for before (the cache keeps the last while loading).
  if (!types.data || (id && saved.data?.id !== id)) return <DetailSkeleton />;
  return (
    <Suspense fallback={<DetailSkeleton />}>
      <WorkflowEditor types={types.data} saved={id ? saved.data! : null} />
    </Suspense>
  );
}
