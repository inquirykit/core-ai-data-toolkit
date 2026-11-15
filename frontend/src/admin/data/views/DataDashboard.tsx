// A workspace's dashboard in the Data section (#/data/<ws>/_dashboards/<name>?<params>):
// its title, the params its views share as one form at the top (the hash keeps
// their values, its defaults fill in the rest), and its views in order on a
// 12-column grid (one column on a phone), each as wide as its item says, each run
// with the values of its own params' names and drawn by its display, refreshed
// at the dashboard's interval (poll(), never faster than every 5 s); and its
// editor: title, params, refresh, and the items (add a view, its width, move it
// up or down, remove it).
import { useMemo, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { ArrowDown, ArrowUp, LayoutDashboard, Pencil, Plus, Save, Trash2, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";
import { useAction } from "../../../shared/action";
import { useApi } from "../../../shared/api";
import { CodeEditor } from "../../../shared/code-editor";
import { ConfirmButton, EmptyState, Field, Notice, SelectInput, SubmitButton } from "../../../shared/components";
import { errorText, poll } from "../../../shared/query";
import { DetailSkeleton } from "../../layout";
import { dataHash, NEW_ENTRY, type Workspace } from "../data";
import { invalidateViews, ParamsForm, RanAt, useWorkspaceViews } from "./DataViews";
import { ResultView } from "./ViewDisplay";
import {
  clampWidth,
  dashboardDraftOf,
  dashboardSpecOf,
  GRID_COLUMNS,
  moveItem,
  newDashboardDraft,
  refreshLabel,
  refreshMs,
  sharedParams,
  valuesFor,
  valuesFromHash,
  valuesToHash,
  withDefaults,
  type DashboardDraft,
  type DashboardItem,
  type DashboardSpec,
  type ParamValues,
  type ViewInfo,
} from "./views";

// Tailwind finds class names whole in the source: each width's, written out.
const SPANS: Record<number, string> = {
  1: "md:col-span-1",
  2: "md:col-span-2",
  3: "md:col-span-3",
  4: "md:col-span-4",
  5: "md:col-span-5",
  6: "md:col-span-6",
  7: "md:col-span-7",
  8: "md:col-span-8",
  9: "md:col-span-9",
  10: "md:col-span-10",
  11: "md:col-span-11",
  12: "md:col-span-12",
};

/** A dashboard's page; ``name`` NEW_ENTRY for a new one, which opens in the editor. */
export function DashboardPage({ workspace, name, values }: { workspace: Workspace; name: string; values: Record<string, string> }) {
  const client = useApi();
  const ws = workspace.name;
  const isNew = name === NEW_ENTRY;
  const saved = client.useQuery("get", "/db/{workspace}/_dashboards/{name}", { params: { path: { workspace: ws, name } } }, { enabled: !isNew });
  const { views, error: viewsError } = useWorkspaceViews(ws);
  const [draft, setDraft] = useState<DashboardDraft | null>(() => (isNew ? newDashboardDraft() : null));
  // While editing, the page shows the draft as far as it reads.
  const drafted = useMemo(() => (draft ? dashboardSpecOf({ ...draft, name: draft.name || "draft", title: draft.title || "Untitled" }) : null), [draft]);
  const board: DashboardSpec | undefined = drafted && "spec" in drafted ? drafted.spec : saved.data;
  const byName = useMemo(() => new Map((views ?? []).map((v) => [v.name, v])), [views]);
  // The hash's values, and the dashboard's defaults for the rest, as its form shows them: what its views run with.
  const params = useMemo(() => withDefaults(board?.params, valuesFromHash(board?.params, values)), [board?.params, values]);
  const every = refreshMs(board?.refresh_s);

  if (!isNew && saved.isPending) return <DetailSkeleton />;
  if (!isNew && !saved.data) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>No dashboard named {name}</CardTitle>
          <CardDescription>{errorText(saved.error) || "It may have been deleted."}</CardDescription>
        </CardHeader>
      </Card>
    );
  }
  const items = board?.items ?? [];
  return (
    <div className="grid min-w-0 gap-5">
      <Card className="min-w-0 gap-4">
        <CardHeader>
          <CardTitle className="flex min-w-0 flex-wrap items-center gap-1.5 text-lg">
            <a className="font-mono text-muted-foreground hover:text-foreground" href={dataHash({ workspace: ws })}>
              {ws}
            </a>
            <span className="text-muted-foreground">/</span>
            <LayoutDashboard className="size-4 text-muted-foreground" aria-hidden="true" />
            <span className="truncate">{board?.title || (isNew ? "New dashboard" : name)}</span>
          </CardTitle>
          <CardDescription>
            {board?.description ? `${board.description} ` : ""}
            {refreshLabel(board?.refresh_s)}.
          </CardDescription>
          <CardAction className="flex gap-2">
            {!isNew &&
              (draft ? (
                <Button type="button" variant="ghost" size="sm" onClick={() => setDraft(null)}>
                  <X aria-hidden="true" />
                  Close the editor
                </Button>
              ) : (
                <Button type="button" variant="outline" size="sm" onClick={() => saved.data && setDraft(dashboardDraftOf(saved.data))}>
                  <Pencil aria-hidden="true" />
                  Edit
                </Button>
              ))}
          </CardAction>
        </CardHeader>
        <CardContent className="grid min-w-0 gap-4">
          <ParamsForm
            schema={board?.params}
            values={params}
            label="Apply"
            onRun={(next) => {
              location.hash = dataHash({ workspace: ws, dashboard: name, values: valuesToHash(next) });
            }}
          />
          {viewsError && <Notice notice={{ kind: "error", text: viewsError }} />}
          {items.length ? (
            <div className="grid min-w-0 grid-cols-1 gap-4 md:grid-cols-12">
              {items.map((item, i) => (
                <Tile key={`${i}/${item.view}`} workspace={workspace} item={item} view={byName.get(item.view)} known={views !== null} values={params} every={every} />
              ))}
            </div>
          ) : (
            <EmptyState icon={LayoutDashboard}>No views on it yet{draft ? ": add them below." : "."}</EmptyState>
          )}
        </CardContent>
      </Card>
      {draft && <DashboardEditor workspace={ws} saved={saved.data ?? null} draft={draft} onChange={setDraft} views={views ?? []} onSaved={() => setDraft(null)} />}
    </div>
  );
}

/** One view of the dashboard, as wide as its item says: run with its own params' values, at the dashboard's interval. */
function Tile(props: { workspace: Workspace; item: DashboardItem; view: ViewInfo | undefined; known: boolean; values: ParamValues; every: number }) {
  const client = useApi();
  const { item, view } = props;
  const ws = props.workspace.name;
  const params = useMemo(() => valuesFor(view?.params, props.values), [view?.params, props.values]);
  const run = client.useQuery(
    "post",
    "/db/{workspace}/_views/{name}",
    { params: { path: { workspace: ws, name: item.view } }, body: { params } },
    { enabled: Boolean(view), ...poll(props.every) },
  );
  return (
    <Card className={cn("min-w-0 gap-3 py-4", SPANS[clampWidth(item.width ?? 6)])} aria-label={view?.label ?? item.view}>
      <CardHeader className="px-4">
        <CardTitle className="truncate text-sm">
          <a className="hover:underline" href={dataHash({ workspace: ws, viewName: item.view, values: valuesToHash(params) })}>
            {view?.label ?? item.view}
          </a>
        </CardTitle>
        {run.data && (
          <CardDescription>
            <RanAt result={run.data} fetching={run.isFetching} />
          </CardDescription>
        )}
      </CardHeader>
      <CardContent className="min-w-0 px-4">
        {!view ? (
          <p className="text-sm text-muted-foreground">{props.known ? `No view named ${item.view} any more.` : "Loading…"}</p>
        ) : run.error ? (
          <Notice notice={{ kind: "error", text: errorText(run.error) }} />
        ) : run.data ? (
          <ResultView workspace={props.workspace} spec={view} result={run.data} height={220} compact />
        ) : (
          <DetailSkeleton />
        )}
      </CardContent>
    </Card>
  );
}

/** The editor of a dashboard: its title, params, refresh and items; Save and Delete. */
function DashboardEditor(props: {
  workspace: string;
  saved: DashboardSpec | null;
  draft: DashboardDraft;
  views: ViewInfo[];
  onChange(draft: DashboardDraft): void;
  onSaved(): void;
}) {
  const client = useApi();
  const { draft, saved, workspace: ws } = props;
  const set = (part: Partial<DashboardDraft>) => props.onChange({ ...draft, ...part });
  const setItems = (items: DashboardItem[]) => set({ items });
  const queries = useQueryClient();
  const labelOf = (name: string) => props.views.find((v) => v.name === name)?.label ?? name;
  const options = props.views.map((v) => ({ value: v.name, label: v.label ? `${v.label} (${v.name})` : v.name }));

  const save = useAction(
    async () => {
      const made = dashboardSpecOf(draft);
      if ("error" in made) throw new Error(made.error);
      const done = await client.put("/db/{workspace}/_dashboards/{name}", { params: { path: { workspace: ws, name: made.spec.name } }, body: made.spec });
      invalidateViews(queries);
      props.onSaved();
      location.hash = dataHash({ workspace: ws, dashboard: done.name });
      return done;
    },
    { notice: true, success: (done) => `Saved ${done.name}.` },
  );
  const remove = useAction(
    async () => {
      if (!saved) return;
      await client.delete("/db/{workspace}/_dashboards/{name}", { params: { path: { workspace: ws, name: saved.name } } });
      invalidateViews(queries);
      location.hash = dataHash({ workspace: ws });
    },
    { success: () => `Deleted ${saved?.name}.`, failed: () => `Could not delete ${saved?.name}` },
  );
  const fromViews = () => {
    const shown = draft.items.flatMap((i) => props.views.filter((v) => v.name === i.view));
    set({ paramsJson: JSON.stringify(sharedParams(shown), null, 2) });
  };

  return (
    <Card className="min-w-0 gap-4">
      <CardHeader>
        <CardTitle className="text-base">{saved ? `Edit ${saved.name}` : "The new dashboard"}</CardTitle>
        <CardDescription>The page above shows what is typed here; Save keeps it.</CardDescription>
      </CardHeader>
      <CardContent className="grid min-w-0 gap-5">
        <div className="flex flex-wrap gap-3">
          <Field label="Name" hint={saved ? "Fixed once saved" : "Letters, digits and single underscores"} className="w-56">
            <Input
              aria-label="Dashboard name"
              className="h-8 font-mono text-sm"
              placeholder="sales"
              pattern="[A-Za-z][A-Za-z0-9]*(_[A-Za-z0-9]+)*"
              maxLength={48}
              readOnly={Boolean(saved)}
              value={draft.name}
              onChange={(e) => set({ name: e.target.value })}
            />
          </Field>
          <Field label="Title" className="min-w-56 flex-1">
            <Input aria-label="Dashboard title" className="h-8" placeholder="Sales this year" maxLength={200} value={draft.title} onChange={(e) => set({ title: e.target.value })} />
          </Field>
          <Field label="Refresh (s)" hint="0 for none, else 5 at least" className="w-40">
            <Input aria-label="Refresh seconds" inputMode="numeric" className="h-8" value={draft.refreshS} onChange={(e) => set({ refreshS: e.target.value })} />
          </Field>
        </div>
        <Field label="Description">
          <Input aria-label="Dashboard description" className="h-8" maxLength={2000} value={draft.description} onChange={(e) => set({ description: e.target.value })} />
        </Field>

        <section className="grid gap-2" aria-label="Views on the dashboard">
          <h3 className="text-sm font-medium">Views</h3>
          {draft.items.map((item, i) => (
            <div key={`${i}/${item.view}`} className="flex flex-wrap items-center gap-2 rounded-lg border px-3 py-2">
              <span className="w-6 text-right text-xs text-muted-foreground tabular-nums">{i + 1}</span>
              <SelectInput
                size="sm"
                className="w-64"
                aria-label={`View ${i + 1}`}
                value={item.view}
                onChange={(view) => setItems(draft.items.map((it, j) => (j === i ? { ...it, view } : it)))}
                options={options.some((o) => o.value === item.view) ? options : [{ value: item.view, label: `${item.view} (not there)` }, ...options]}
              />
              <label className="flex items-center gap-2 text-xs text-muted-foreground">
                Width
                <Input
                  aria-label={`Width of ${labelOf(item.view)}`}
                  type="number"
                  min={1}
                  max={GRID_COLUMNS}
                  className="h-8 w-16"
                  value={item.width ?? 6}
                  onChange={(e) => setItems(draft.items.map((it, j) => (j === i ? { ...it, width: clampWidth(Number(e.target.value)) } : it)))}
                />
                of {GRID_COLUMNS}
              </label>
              <span className="ml-auto inline-flex gap-1">
                <Button type="button" variant="ghost" size="icon-sm" aria-label={`Move ${labelOf(item.view)} up`} disabled={i === 0} onClick={() => setItems(moveItem(draft.items, i, -1))}>
                  <ArrowUp aria-hidden="true" />
                </Button>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon-sm"
                  aria-label={`Move ${labelOf(item.view)} down`}
                  disabled={i === draft.items.length - 1}
                  onClick={() => setItems(moveItem(draft.items, i, 1))}
                >
                  <ArrowDown aria-hidden="true" />
                </Button>
                <Button type="button" variant="ghost" size="icon-sm" aria-label={`Remove ${labelOf(item.view)}`} onClick={() => setItems(draft.items.filter((_, j) => j !== i))}>
                  <Trash2 aria-hidden="true" />
                </Button>
              </span>
            </div>
          ))}
          <div className="flex flex-wrap items-center gap-2">
            <Plus className="size-4 text-muted-foreground" aria-hidden="true" />
            <SelectInput
              size="sm"
              className="w-64"
              aria-label="Add a view"
              placeholder={options.length ? "Add a view…" : "No views in the workspace yet"}
              disabled={!options.length}
              value=""
              onChange={(view) => view && setItems([...draft.items, { view, width: 6 }])}
              options={options}
            />
          </div>
        </section>

        <section className="grid gap-2" aria-label="Params of the dashboard">
          <div className="flex flex-wrap items-center gap-3">
            <h3 className="text-sm font-medium">Params</h3>
            <Button type="button" variant="outline" size="sm" disabled={!draft.items.length} onClick={fromViews} title="The params its views declare, merged">
              From its views
            </Button>
          </div>
          <CodeEditor aria-label="Dashboard params" language="json" minHeight="5rem" value={draft.paramsJson} onChange={(paramsJson) => set({ paramsJson })} />
          <p className="text-xs text-muted-foreground">One JSON Schema of an object for the whole page: each view takes the values of its own params' names, the defaults here included, and its own defaults for the rest.</p>
        </section>

        <div className="flex flex-wrap items-center gap-2">
          <SubmitButton type="button" busy={save.busy} icon={<Save aria-hidden="true" />} onClick={() => void save.run()}>
            Save dashboard
          </SubmitButton>
          {saved && (
            <ConfirmButton label="Delete dashboard" title={`Delete the dashboard ${saved.name}?`} body="Its views stay." busy={remove.busy} onConfirm={remove.run} />
          )}
        </div>
        <Notice notice={save.notice} />
      </CardContent>
    </Card>
  );
}
