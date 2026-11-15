// A workspace's views in the Data section (#/data/<ws>/_views/<name>?<params>):
// the view's params form (SchemaForm, from its params' JSON Schema), whose
// values the hash keeps so a link opens the same view with the same values; its
// answer drawn by its display (ViewDisplay.tsx), with when it ran and whether it
// came from the cache; and its editor: the source (SQL in the query panel's
// editor, or a rows or search source as JSON), the params' JSON Schema, the
// display's fields, a preview of what is typed (POST .../_views/_try, never
// kept), save and delete. Runs go through POST /db/{ws}/_views/{name}, as a
// client's do; the editor's writes need the admin key the page holds.
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useQueryClient, type QueryClient } from "@tanstack/react-query";
import { Eye, LayoutDashboard, Pencil, Play, RotateCw, Save, X } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { useAction } from "../../../shared/action";
import { useApi } from "../../../shared/api";
import { CodeEditor } from "../../../shared/code-editor";
import { ConfirmButton, Field, Notice, Segmented, SelectField, SubmitButton } from "../../../shared/components";
import { valueKey } from "../../../shared/form-schema";
import { fmtAgo, fmtInt, fmtTime, plural } from "../../../shared/format";
import { errorText } from "../../../shared/query";
import { SchemaForm, type SchemaFormHandle } from "../../../shared/schema-form";
import { DetailSkeleton } from "../../layout";
import { dataHash, NEW_ENTRY, type Workspace } from "../data";
import { ResultView } from "./ViewDisplay";
import {
  describeView,
  DISPLAYS,
  draftOf,
  guessDisplay,
  newDraft,
  paramNames,
  paramsForm,
  SOURCES,
  sourceExample,
  specOf,
  valuesFromHash,
  valuesToHash,
  type DisplayDraft,
  type DisplayType,
  type ParamsSchema,
  type ParamValues,
  type SourceKind,
  type ViewDraft,
  type ViewInfo,
  type ViewResult,
  type ViewSpec,
} from "./views";

/** The pages' queries of a workspace's views and dashboards, loaded again after a write. */
export function invalidateViews(queries: QueryClient): void {
  for (const path of ["/db/{workspace}/_views", "/db/{workspace}/_views/{name}", "/db/{workspace}/_dashboards", "/db/{workspace}/_dashboards/{name}"]) {
    void queries.invalidateQueries({ queryKey: ["get", path] });
  }
  void queries.invalidateQueries({ queryKey: ["post", "/db/{workspace}/_views/{name}"] });
}

/** A workspace's views and dashboards (the first 500 of each, by name): for the sidebar, its page and the editors. */
export function useWorkspaceViews(workspace: string, enabled = true) {
  const client = useApi();
  const init = { params: { path: { workspace }, query: { _limit: 500 } } };
  const views = client.useQuery("get", "/db/{workspace}/_views", init, { enabled });
  const dashboards = client.useQuery("get", "/db/{workspace}/_dashboards", init, { enabled });
  return {
    views: views.data?.views ?? null,
    dashboards: dashboards.data?.dashboards ?? null,
    more: Boolean(views.data?.next || dashboards.data?.next),
    error: errorText(views.error ?? dashboards.error),
  };
}

/** When an answer was asked of the database, and whether it was kept from a run before. */
export function RanAt({ result, fetching }: { result: ViewResult; fetching?: boolean }) {
  return (
    <span className="inline-flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
      <span title={new Date(result.ran_at * 1000).toLocaleString()}>
        Ran at {fmtTime(result.ran_at)} ({fmtAgo(result.ran_at)})
      </span>
      {result.cached && (
        <Badge variant="outline" className="font-normal" title="Kept from a run with the same params, younger than the view's refresh interval">
          from the cache
        </Badge>
      )}
      {result.truncated && (
        <Badge variant="outline" className="font-normal" title="The view's row cap cut the rows: only the first are here">
          cut at {fmtInt(result.rows.length)} rows
        </Badge>
      )}
      {fetching && <RotateCw className="size-3 animate-spin" aria-label="Running" />}
    </span>
  );
}

/**
 * The params of a view or a dashboard as a form made from their JSON Schema, starting from ``values`` (the hash's)
 * and again whenever they change; ``onRun`` gets what is typed once it validates.
 */
export function ParamsForm(props: { schema: ParamsSchema | undefined; values: ParamValues; busy?: boolean; label?: string; onRun(values: ParamValues): void }) {
  const form = useRef<SchemaFormHandle>(null);
  const [draft, setDraft] = useState<ParamValues>(props.values);
  const key = valueKey(props.values);
  useEffect(() => setDraft(props.values), [key]);
  const schema = useMemo(() => paramsForm(props.schema), [props.schema]);
  if (!paramNames(props.schema).length) {
    return (
      <div>
        <SubmitButton type="button" size="sm" busy={props.busy} icon={<Play aria-hidden="true" />} onClick={() => props.onRun({})}>
          {props.label ?? "Run"}
        </SubmitButton>
      </div>
    );
  }
  return (
    <form
      noValidate
      className="grid gap-3 rounded-lg border bg-muted/30 p-3"
      aria-label="Params"
      onSubmit={(event) => {
        event.preventDefault();
        // A form still loading has said nothing yet: the server checks the values, and fills in the defaults.
        if (form.current && !form.current.validate()) return;
        props.onRun(draft);
      }}
    >
      <SchemaForm ref={form} schema={schema} value={draft} onChange={(v) => setDraft((v ?? {}) as ParamValues)} />
      <div>
        <SubmitButton size="sm" busy={props.busy} icon={<Play aria-hidden="true" />}>
          {props.label ?? "Run"}
        </SubmitButton>
      </div>
    </form>
  );
}

/** A view's page: its params, its answer, and its editor; ``name`` NEW_ENTRY for a new one, which opens in the editor. */
export function ViewPage({ workspace, name, values }: { workspace: Workspace; name: string; values: Record<string, string> }) {
  const client = useApi();
  const ws = workspace.name;
  const isNew = name === NEW_ENTRY;
  const saved = client.useQuery("get", "/db/{workspace}/_views/{name}", { params: { path: { workspace: ws, name } } }, { enabled: !isNew });
  const spec = saved.data;
  const [draft, setDraft] = useState<ViewDraft | null>(() => (isNew ? newDraft(workspace.tables.map((t) => t.name)) : null));
  const [preview, setPreview] = useState<{ spec: ViewSpec; result: ViewResult } | null>(null);
  const editing = draft !== null;
  // While editing, the params form is the draft's, as far as its schema reads.
  const draftSpec = useMemo(() => (draft ? specOf({ ...draft, name: draft.name || "draft" }) : null), [draft]);
  const paramsSchema = draft ? (draftSpec && "spec" in draftSpec ? draftSpec.spec.params : undefined) : spec?.params;
  const params = useMemo(() => valuesFromHash(paramsSchema, values), [paramsSchema, values]);
  // The saved view runs with the values its own params take, whatever the draft declares meanwhile.
  const savedParams = useMemo(() => valuesFromHash(spec?.params, values), [spec?.params, values]);
  const run = client.useQuery(
    "post",
    "/db/{workspace}/_views/{name}",
    { params: { path: { workspace: ws, name } }, body: { params: savedParams } },
    { enabled: Boolean(spec) && !isNew },
  );
  const queries = useQueryClient();

  const tryDraft = useAction(
    async (with_: ParamValues) => {
      if (!draft) return;
      const made = specOf(draft);
      if ("error" in made) throw new Error(made.error);
      const result = await client.post("/db/{workspace}/_views/_try", { params: { path: { workspace: ws } }, body: { spec: made.spec, params: with_ } });
      setPreview({ spec: made.spec, result });
    },
    { notice: true },
  );

  const go = (next: ParamValues) => {
    const hash = dataHash({ workspace: ws, viewName: name, values: valuesToHash(next) });
    if (editing) void tryDraft.run(next);
    if (hash !== location.hash) location.hash = hash;
    else if (!editing) void run.refetch();
  };

  if (!isNew && saved.isPending) return <DetailSkeleton />;
  if (!isNew && !spec) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>No view named {name}</CardTitle>
          <CardDescription>{errorText(saved.error) || "It may have been deleted."}</CardDescription>
        </CardHeader>
      </Card>
    );
  }
  const shown = preview ?? (spec && run.data ? { spec, result: run.data } : null);
  const title = isNew ? "New view" : (spec?.label ?? name);
  return (
    <div className="grid min-w-0 gap-5">
      <Card className="min-w-0 gap-4">
        <CardHeader>
          <CardTitle className="flex min-w-0 flex-wrap items-center gap-1.5 text-lg">
            <a className="font-mono text-muted-foreground hover:text-foreground" href={dataHash({ workspace: ws })}>
              {ws}
            </a>
            <span className="text-muted-foreground">/</span>
            <span className="truncate">{title}</span>
            {spec?.label && <span className="font-mono text-sm font-normal text-muted-foreground">{name}</span>}
          </CardTitle>
          <CardDescription>
            {spec?.description ??
              (isNew
                ? "A saved read over the workspace's data: SQL, a table's rows or its nearest rows, with params and a display. A client key runs it; only the admin writes it."
                : spec && `${describeView(spec)}. A client key runs it: POST /db/${ws}/_views/${name}`)}
          </CardDescription>
          <CardAction className="flex gap-2">
            {!isNew && !editing && (
              <Button type="button" variant="outline" size="sm" onClick={() => spec && setDraft(draftOf(spec))}>
                <Pencil aria-hidden="true" />
                Edit
              </Button>
            )}
            {!isNew && editing && (
              <Button
                type="button"
                variant="ghost"
                size="sm"
                onClick={() => {
                  setDraft(null);
                  setPreview(null);
                }}
              >
                <X aria-hidden="true" />
                Close the editor
              </Button>
            )}
          </CardAction>
        </CardHeader>
        <CardContent className="grid min-w-0 gap-4">
          <ParamsForm
            schema={paramsSchema}
            values={params}
            busy={editing ? tryDraft.busy : run.isFetching}
            label={editing ? "Preview" : "Run"}
            onRun={go}
          />
          {editing && <Notice notice={tryDraft.notice} />}
          {!editing && run.error && <Notice notice={{ kind: "error", text: errorText(run.error) }} />}
          {shown && (
            <div className="grid min-w-0 gap-2">
              <div className="flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
                {preview && (
                  <Badge variant="secondary" className="font-normal">
                    <Eye aria-hidden="true" />
                    Preview of the editor's view, not saved
                  </Badge>
                )}
                <span>{plural(shown.result.rows.length, "row")}</span>
                <RanAt result={shown.result} fetching={!preview && run.isFetching} />
              </div>
              <ResultView workspace={workspace} spec={shown.spec} result={shown.result} />
            </div>
          )}
          {!shown && !editing && run.isFetching && <DetailSkeleton />}
          {!shown && editing && <p className="text-sm text-muted-foreground">Preview to see what the view answers before saving it.</p>}
        </CardContent>
      </Card>
      {draft && (
        <ViewEditor
          workspace={workspace}
          saved={spec ?? null}
          draft={draft}
          onChange={setDraft}
          result={shown?.result}
          onSaved={(made) => {
            invalidateViews(queries);
            setPreview(null);
            setDraft(null);
            location.hash = dataHash({ workspace: ws, viewName: made.name, values });
          }}
        />
      )}
    </div>
  );
}

const DISPLAY_HINTS: Record<DisplayType, string> = {
  table: "The rows in a table: the columns named, in their order, or every one.",
  number: "One figure: a field of the first row, large, with its label and unit.",
  line: "A line per y field over x; with a series field, a line per value of it (one y then).",
  bar: "Bars per x: one per y field, or with a series field one per value of it; side by side, or stacked.",
  pie: "A slice per label, as large as the sum of its value; past seven, the rest are one slice.",
  graph: "The relationships the SQL returns (rows of _edges: from_table, from_id, to_table, to_id), drawn as the Graph query draws them.",
  files: "A bucket's files (rows with id, name, size, content_type): each with its download, images as thumbnails.",
};

/** The editor of a view: what it reads, its params, how it is drawn, and its caps; Save and Delete. */
function ViewEditor(props: {
  workspace: Workspace;
  saved: ViewInfo | null;
  draft: ViewDraft;
  onChange(draft: ViewDraft): void;
  /** The last answer shown, whose columns the display's fields are offered from */
  result?: ViewResult;
  onSaved(spec: ViewSpec): void;
}) {
  const client = useApi();
  const { draft, saved } = props;
  const ws = props.workspace.name;
  const set = (part: Partial<ViewDraft>) => props.onChange({ ...draft, ...part });
  const setDisplay = (part: Partial<DisplayDraft>) => set({ display: { ...draft.display, ...part } });
  const queries = useQueryClient();
  const tables = props.workspace.tables;
  const schema = useMemo(
    () => Object.fromEntries([...tables.map((t) => [t.name, t.fields.map((f) => f.name)]), ...props.workspace.edges.map((e) => [e.name, []])]),
    [tables, props.workspace.edges],
  );
  const columns = props.result?.columns ?? [];
  const listId = `view-columns-${ws}`;

  const save = useAction(
    async () => {
      const made = specOf(draft);
      if ("error" in made) throw new Error(made.error);
      const done = await client.put("/db/{workspace}/_views/{name}", { params: { path: { workspace: ws, name: made.spec.name } }, body: made.spec });
      props.onSaved(made.spec);
      return done;
    },
    { notice: true, success: (done) => `Saved ${done.name}.` },
  );
  const remove = useAction(
    async () => {
      if (!saved) return;
      await client.delete("/db/{workspace}/_views/{name}", { params: { path: { workspace: ws, name: saved.name } } });
      invalidateViews(queries);
      location.hash = dataHash({ workspace: ws });
    },
    { success: () => `Deleted ${saved?.name}.`, failed: () => `Could not delete ${saved?.name}` },
  );

  // Another kind of source starts from its example over the first table (SQL keeps what was typed).
  const chooseKind = (kind: SourceKind) =>
    set({ kind, ...(kind !== "sql" && kind !== draft.kind ? { sourceJson: JSON.stringify(sourceExample(kind, tables[0]?.name ?? "table"), null, 2) } : {}) });
  const fieldInput = (label: string, key: keyof DisplayDraft, placeholder: string, hint?: ReactNode) => (
    <Field label={label} hint={hint} className="min-w-40 flex-1">
      <Input
        aria-label={`Display ${label.toLowerCase()}`}
        list={listId}
        placeholder={placeholder}
        autoComplete="off"
        spellCheck={false}
        className="h-8 font-mono text-xs"
        value={String(draft.display[key] ?? "")}
        onChange={(e) => setDisplay({ [key]: e.target.value } as Partial<DisplayDraft>)}
      />
    </Field>
  );

  return (
    <Card className="min-w-0 gap-4">
      <CardHeader>
        <CardTitle className="text-base">{saved ? `Edit ${saved.name}` : "The new view"}</CardTitle>
        <CardDescription>Preview runs what is typed here with the params above, and keeps nothing; Save keeps it, and a client key can run it then.</CardDescription>
      </CardHeader>
      <CardContent className="grid min-w-0 gap-5">
        <div className="flex flex-wrap gap-3">
          <Field label="Name" hint={saved ? "Fixed once saved" : "Letters, digits and single underscores"} className="w-56">
            <Input
              aria-label="View name"
              className="h-8 font-mono text-sm"
              placeholder="sales_by_region"
              pattern="[A-Za-z][A-Za-z0-9]*(_[A-Za-z0-9]+)*"
              maxLength={48}
              readOnly={Boolean(saved)}
              value={draft.name}
              onChange={(e) => set({ name: e.target.value })}
            />
          </Field>
          <Field label="Label" className="min-w-56 flex-1">
            <Input aria-label="View label" className="h-8" placeholder="Sales by region" maxLength={200} value={draft.label} onChange={(e) => set({ label: e.target.value })} />
          </Field>
        </div>
        <Field label="Description">
          <Input aria-label="View description" className="h-8" maxLength={2000} value={draft.description} onChange={(e) => set({ description: e.target.value })} />
        </Field>

        <section className="grid gap-3" aria-label="Source">
          <div className="flex flex-wrap items-center gap-3">
            <h3 className="text-sm font-medium">Reads</h3>
            <Segmented<SourceKind> label="What the view reads" size="sm" options={SOURCES} value={draft.kind} onChange={chooseKind} />
          </div>
          {draft.kind === "sql" ? (
            <>
              <CodeEditor
                aria-label="View SQL"
                language="sql"
                schema={schema}
                lineNumbers
                minHeight="7rem"
                placeholder="SELECT region, sum(total) AS total FROM sales WHERE year = $year GROUP BY region"
                value={draft.sql}
                onChange={(sql) => set({ sql })}
              />
              <p className="text-xs text-muted-foreground">
                One SELECT (DuckDB's SQL), named as the workspace names its tables; declared fields are columns, the others in{" "}
                <code className="font-mono">_rest</code>. Each param is <code className="font-mono">$name</code>, bound, never written into the text. It stops at
                its time limit and its row cap.
              </p>
              <Field label="Time limit (ms)" hint="At most the service's VIEW_TIME_LIMIT_MS, which it is when empty" className="w-72">
                <Input aria-label="Time limit in milliseconds" inputMode="numeric" className="h-8" value={draft.timeLimitMs} onChange={(e) => set({ timeLimitMs: e.target.value })} />
              </Field>
            </>
          ) : (
            <>
              <CodeEditor aria-label="View source" language="json" minHeight="6rem" value={draft.sourceJson} onChange={(sourceJson) => set({ sourceJson })} />
              <p className="text-xs text-muted-foreground">
                {draft.kind === "rows"
                  ? 'A table\'s rows as its list gives them: {"table", "filters": {field: value}, "sort": "-field", "search": words, "limit"}.'
                  : 'A table\'s nearest rows, as _search takes them: {"table", "vector" or "text", "k", "filters", "q"}; each row gets its _distance.'}{" "}
                A value <code className="font-mono">{'{"$param": "name"}'}</code> takes that param's value; with none, its key is left out.
              </p>
            </>
          )}
        </section>

        <section className="grid gap-2" aria-label="Params">
          <h3 className="text-sm font-medium">Params</h3>
          <CodeEditor aria-label="View params" language="json" minHeight="5rem" value={draft.paramsJson} onChange={(paramsJson) => set({ paramsJson })} />
          <p className="text-xs text-muted-foreground">
            A JSON Schema of an object: each property a param, with its type, default, enum or bounds, e.g.{" "}
            <code className="font-mono">{'{"type": "object", "properties": {"region": {"type": "string", "enum": ["north", "south"]}}}'}</code>. A run gives no param it
            does not declare.
          </p>
        </section>

        <section className="grid gap-3" aria-label="Display">
          <div className="flex flex-wrap items-end gap-3">
            <SelectField
              label="Display"
              className="w-44"
              aria-label="Display type"
              value={draft.display.type}
              onChange={(type) => set({ display: guessDisplay(draft.display, type as DisplayType, props.result) })}
              options={DISPLAYS.map(([value, label]) => ({ value, label }))}
            />
            <p className="min-w-56 flex-1 pb-2 text-xs text-muted-foreground">{DISPLAY_HINTS[draft.display.type]}</p>
          </div>
          <datalist id={listId}>
            {columns.map((c) => (
              <option key={c} value={c} />
            ))}
          </datalist>
          <div className="flex flex-wrap gap-3">
            {draft.display.type === "table" && fieldInput("Columns", "columns", "every column", "Comma-separated, in their order")}
            {draft.display.type === "number" && (
              <>
                {fieldInput("Field", "field", "total")}
                {fieldInput("Label", "label", "Total sales")}
                {fieldInput("Unit", "unit", "EUR")}
              </>
            )}
            {(draft.display.type === "line" || draft.display.type === "bar") && (
              <>
                {fieldInput("X", "x", "month")}
                {fieldInput("Y", "y", "total", "Comma-separated, a series each")}
                {fieldInput("Series", "series", "none", "A series per value of this field")}
                {draft.display.type === "bar" && (
                  <label className="flex items-center gap-2 self-center pt-5 text-sm">
                    <Switch checked={draft.display.stacked} onCheckedChange={(stacked) => setDisplay({ stacked })} aria-label="Stacked bars" />
                    Stacked
                  </label>
                )}
              </>
            )}
            {draft.display.type === "pie" && (
              <>
                {fieldInput("Label", "label", "region")}
                {fieldInput("Value", "value", "total")}
              </>
            )}
            {draft.display.type === "files" && fieldInput("Bucket", "bucket", "the source's table", "The bucket the rows are files of")}
          </div>
        </section>

        <div className="flex flex-wrap gap-3">
          <Field label="Rows at most" hint="At most the service's VIEW_MAX_ROWS, which it is when empty" className="w-64">
            <Input aria-label="Rows at most" inputMode="numeric" className="h-8" value={draft.maxRows} onChange={(e) => set({ maxRows: e.target.value })} />
          </Field>
          <Field label="Kept for (s)" hint="An answer is kept this long for runs with the same params: 30 when empty, 5 at least" className="w-72">
            <Input aria-label="Kept for seconds" inputMode="numeric" className="h-8" value={draft.refreshS} onChange={(e) => set({ refreshS: e.target.value })} />
          </Field>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <SubmitButton type="button" busy={save.busy} icon={<Save aria-hidden="true" />} onClick={() => void save.run()}>
            Save view
          </SubmitButton>
          {saved && (
            <ConfirmButton
              label="Delete view"
              title={`Delete the view ${saved.name}?`}
              body="Clients running it get 404 from now on. A view a dashboard shows is not deleted: take it off the dashboard first."
              busy={remove.busy}
              onConfirm={remove.run}
            />
          )}
          <a className="ml-auto inline-flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground" href={dataHash({ workspace: ws, dashboard: NEW_ENTRY })}>
            <LayoutDashboard className="size-4" aria-hidden="true" />
            New dashboard
          </a>
        </div>
        <Notice notice={save.notice} />
      </CardContent>
    </Card>
  );
}
