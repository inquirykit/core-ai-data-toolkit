// A workspace of the Data section, when no table of it is chosen: its label, which
// can be changed; one form that makes a table (empty, or a sample's schema and rows),
// a relationship or a bucket of files; its tables, in their groups, and relationships (with the
// tables each joins); its views and dashboards; and how a client writes a row. "Relationship" is the
// dashboard's word for the API's edge type. A table is a graph table, which
// relationships can join, unless it was made plain: those are marked, and so
// are kept tables, which a part of the service keeps (read-only here).
import { useState, type FormEvent, type ReactNode } from "react";
import { Files, LayoutDashboard, Lock, Network, Pencil, Plus, Shapes, Spline, SquareTerminal } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button, buttonVariants } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";
import { useAction } from "../../shared/action";
import { useApi } from "../../shared/api";
import { CodeBlock, ConfirmButton, CopyButton, Notice, Segmented, SubmitButton } from "../../shared/components";
import { fmtAgo, fmtBytes, fmtInt, plural } from "../../shared/format";
import { DataGrid, type GridColumn } from "../../shared/grid";
import { DEFAULT_WORKSPACE, dataHash, groupsOf, isOwn, NEW_ENTRY, vectorField, type TableInfo, type Workspace } from "./data";
import { joinLabel } from "./graph/edges";
import { useEdgeTypes } from "./graph/DataEdgeType";
import { PinToggle, type Pins } from "./DataPins";
import { useWorkspaceViews } from "./views/DataViews";
import { ViewIcon } from "./views/ViewDisplay";
import { describeView, type DashboardInfo, type ViewInfo } from "./views/views";
import { SAMPLES, sampleTransaction, type SampleKey } from "./samples";

export function WorkspaceCard({ workspace, pins, onChange }: { workspace: Workspace; pins: Pins; onChange(): void }) {
  const client = useApi();
  const rows = workspace.tables.reduce((sum, t) => sum + t.count, 0);
  const edges = workspace.edges.reduce((sum, e) => sum + e.count, 0);
  const graphTables = workspace.tables.filter((t) => t.graph).length;
  const own = isOwn(workspace);
  // Tables a part of the service keeps here: while it holds one, the workspace cannot be deleted.
  const kept = workspace.tables.filter((t) => t.kept).length;
  // A bucket's size is its rows': its files' bytes are kept apart, in DATA_DIR/files.
  const buckets = workspace.tables.filter((t) => t.bucket).length;
  const groups = groupsOf(workspace);
  // As in the sidebar: a workspace with only tables lists them without a heading.
  const headed = groups.length > 1 || groups[0]?.key !== "tables";
  const example = `curl -X PUT ${location.origin}/db/${workspace.name}/todos/1 \\
  -H 'X-API-Key: <API_KEY>' -H 'Content-Type: application/json' \\
  -d '{"title": "Hello", "tags": ["first"]}'`;

  const drop = useAction(
    async () => {
      await client.delete("/db/{workspace}", { params: { path: { workspace: workspace.name } } });
      location.hash = dataHash({});
      onChange();
    },
    { success: `Deleted ${workspace.name}.`, failed: `Could not delete ${workspace.name}` },
  );

  return (
    <Card className="gap-4">
      <CardHeader>
        <CardTitle className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-2">
          <span className="max-w-full truncate font-mono text-lg" title={workspace.name}>
            {workspace.name}
          </span>
          <LabelEditor workspace={workspace} onChange={onChange} />
        </CardTitle>
        <CardDescription>
          {workspace.tables.length} tables · {fmtInt(rows)} rows
          {workspace.edges.length > 0 && ` · ${plural(edges, "relationship")}`} · {fmtBytes(workspace.size_bytes)} on disk
          {buckets > 0 && ", besides the buckets' files"}
          {workspace.name === DEFAULT_WORKSPACE && " · always there"}
          {kept > 0 && ` · ${plural(kept, "table")} kept by the service`}
        </CardDescription>
        <CardAction className="flex flex-wrap justify-end gap-2">
          <QueryButton
            href={dataHash({ workspace: workspace.name, diagram: true })}
            disabled={!workspace.tables.length}
            title={
              workspace.tables.length
                ? "Draw the workspace's tables, their table refs and its relationships"
                : "Create a table first: the diagram draws the workspace's tables"
            }
            icon={<Shapes aria-hidden="true" />}
            label="Diagram"
          />
          <QueryButton
            href={dataHash({ workspace: workspace.name, query: true })}
            disabled={!workspace.tables.length}
            title={workspace.tables.length ? "Run a read-only SQL query over the workspace's tables" : "Create a table first: SQL reads the workspace's tables"}
            icon={<SquareTerminal className="text-query-sql" aria-hidden="true" />}
            label="SQL query"
          />
          <QueryButton
            href={dataHash({ workspace: workspace.name, graph: true })}
            disabled={!graphTables}
            title={
              graphTables
                ? "Run a read-only SQL query over the relationships, and draw the ones it returns"
                : "Create a table first: Graph query reads tables that relationships can join"
            }
            icon={<Network className="text-query-graph" aria-hidden="true" />}
            label="Graph query"
          />
          {!own && !kept && workspace.name !== DEFAULT_WORKSPACE && (
            <ConfirmButton
              label="Delete workspace"
              title={`Delete ${workspace.name}?`}
              body={`Its ${workspace.tables.length} tables and ${fmtInt(rows)} rows are deleted with it. This cannot be undone.`}
              busy={drop.busy}
              onConfirm={drop.run}
            />
          )}
        </CardAction>
      </CardHeader>
      <CardContent className="grid gap-5">
        {own ? (
          <Notice notice={{ text: `Read-only here: kept by the service (${workspace.managed}).` }} />
        ) : (
          <NewForm workspace={workspace.name} graphTables={graphTables} onCreated={onChange} />
        )}
        {groups.map((group) =>
          group.key === "graph" ? (
            <Group key={group.key} title="Relationships">
              <EdgeTypes workspace={workspace} onChange={onChange} />
            </Group>
          ) : (
            <Group key={group.key} title={headed ? group.label : undefined}>
              <TableList workspace={workspace.name} tables={group.tables} markPlain={!own} pins={pins} />
            </Group>
          ),
        )}
        <Group title="Views">
          <ViewsList workspace={workspace.name} />
        </Group>
        <div className="grid gap-2">
          <p className="text-sm text-muted-foreground">
            {workspace.tables.length ? "Clients write rows like this (a missing table is made on the first write):" : "No tables yet. Create one above (a sample comes with rows to try), or let a client's first write make it:"}
          </p>
          <CodeBlock>{example}</CodeBlock>
          <div>
            <CopyButton value={example} />
          </div>
        </div>
      </CardContent>
    </Card>
  );
}

/**
 * The workspace's label beside its name, and, unless the service keeps it, a button that edits it in place: saved
 * on Enter, cleared when saved empty (PATCH /db/{workspace}).
 */
function LabelEditor({ workspace, onChange }: { workspace: Workspace; onChange(): void }) {
  const client = useApi();
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState("");
  const save = useAction(
    async (event: FormEvent) => {
      event.preventDefault();
      await client.patch("/db/{workspace}", { params: { path: { workspace: workspace.name } }, body: { label: text.trim() || null } });
      setEditing(false);
      onChange();
    },
    { notice: true },
  );
  if (editing) {
    return (
      <form className="flex flex-wrap items-center gap-2 font-sans text-sm font-normal" onSubmit={save.run}>
        <Input
          aria-label="Label"
          placeholder="What it holds, in words"
          maxLength={200}
          autoFocus
          autoComplete="off"
          className="h-8 w-72"
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => e.key === "Escape" && setEditing(false)}
        />
        <SubmitButton size="sm" busy={save.busy}>
          Save
        </SubmitButton>
        <Button type="button" variant="ghost" size="sm" onClick={() => setEditing(false)}>
          Cancel
        </Button>
        <Notice notice={save.notice} />
      </form>
    );
  }
  if (isOwn(workspace)) return workspace.label ? <span className="font-sans text-sm font-normal text-muted-foreground">{workspace.label}</span> : null;
  return (
    <span className="flex items-center gap-1 font-sans text-sm font-normal text-muted-foreground">
      {workspace.label ?? <span className="italic">No label</span>}
      <Button
        type="button"
        variant="ghost"
        size="icon"
        className="size-7"
        aria-label="Edit the label"
        title="Edit the label: what the workspace holds, in words"
        onClick={() => {
          setText(workspace.label ?? "");
          save.clear();
          setEditing(true);
        }}
      >
        <Pencil aria-hidden="true" />
      </Button>
    </span>
  );
}

/** A part of the workspace's page under its heading: a group of its tables or its relationships; no heading when it has only tables. */
function Group({ title, children }: { title?: string; children: ReactNode }) {
  return (
    <section className="grid gap-2" aria-label={title}>
      {title && <h3 className="text-sm font-medium">{title}</h3>}
      {children}
    </section>
  );
}

/**
 * The tables: each opens its page, and can be pinned. A kept one names what keeps it; with ``markPlain``, a plain
 * one says relationships cannot join it.
 */
function TableList({ workspace, tables, markPlain, pins }: { workspace: string; tables: TableInfo[]; markPlain: boolean; pins: Pins }) {
  const columns: GridColumn<TableInfo>[] = [
    {
      key: "table",
      label: "Table",
      headClassName: "pl-4",
      className: "pl-4",
      render: (t) => (
        <>
          <a className="font-mono text-[13px] font-medium hover:underline" href={dataHash({ workspace, table: t.name })}>
            {t.name}
          </a>
          {t.kept && <KeptBadge keeper={t.kept} className="ml-2" />}
          {markPlain && !t.graph && !t.kept && !t.bucket && <NoRelationships className="ml-2" />}
        </>
      ),
    },
    {
      key: "schema",
      label: "Schema",
      className: "font-mono text-xs text-muted-foreground",
      render: (t) => {
        if (t.bucket) return "files: name, size, sha256, type, time";
        const vector = vectorField(t.fields);
        return t.fields.length ? `${plural(t.fields.length, "field")}${t.strict ? ", strict" : ""}${vector ? `, vector of ${vector.dimensions}` : ""}` : "none";
      },
    },
    { key: "rows", label: "Rows", align: "right", headTitle: "Rows, or a bucket's files", render: (t) => fmtInt(t.count) },
    {
      key: "size",
      label: "Size",
      align: "right",
      headTitle:
        "On disk in the database: the table's rows, its indexes and its rows' lists of relationships, in files that grow a page at a time and never shrink. A bucket's files are kept apart and not counted",
      className: "text-muted-foreground",
      title: (t) => (t.bucket ? "Its rows only: the files' bytes are kept apart, in DATA_DIR/files" : undefined),
      render: (t) => fmtBytes(t.size_bytes),
    },
    {
      key: "pin",
      label: <span className="sr-only">Pinned</span>,
      headClassName: "w-0 pr-2",
      className: "pr-2 text-right",
      render: (t) => <PinToggle pins={pins} workspace={workspace} table={t.name} />,
    },
  ];
  return (
    <div className="overflow-hidden rounded-lg border">
      <DataGrid columns={columns} rows={tables} rowKey={(t) => t.name} headerClassName="bg-muted/50" rowHref={(t) => dataHash({ workspace, table: t.name })} empty="No tables" />
    </div>
  );
}

/** A view or a dashboard, as the workspace's list of them shows it. */
type ViewEntry = { kind: "view"; view: ViewInfo } | { kind: "dashboard"; dashboard: DashboardInfo };

/**
 * The workspace's dashboards and views (saved reads with params and a display, which a client key runs): each opens
 * its page; New view and New dashboard open an editor.
 */
function ViewsList({ workspace }: { workspace: string }) {
  const { views, dashboards, more, error } = useWorkspaceViews(workspace);
  const entries: ViewEntry[] = [
    ...(dashboards ?? []).map((dashboard) => ({ kind: "dashboard" as const, dashboard })),
    ...(views ?? []).map((view) => ({ kind: "view" as const, view })),
  ];
  const hrefOf = (e: ViewEntry) => (e.kind === "view" ? dataHash({ workspace, viewName: e.view.name }) : dataHash({ workspace, dashboard: e.dashboard.name }));
  const columns: GridColumn<ViewEntry>[] = [
    {
      key: "name",
      label: "View or dashboard",
      headClassName: "pl-4",
      className: "pl-4",
      render: (e) => (
        <a className="inline-flex items-center gap-1.5 font-mono text-[13px] font-medium hover:underline" href={hrefOf(e)}>
          {e.kind === "view" ? <ViewIcon type={e.view.display.type} className="size-3.5 text-muted-foreground" /> : <LayoutDashboard className="size-3.5 text-muted-foreground" aria-hidden="true" />}
          {e.kind === "view" ? e.view.name : e.dashboard.name}
        </a>
      ),
    },
    {
      key: "what",
      label: "What it shows",
      className: "text-muted-foreground",
      render: (e) =>
        e.kind === "view"
          ? `${e.view.label ? `${e.view.label}: ` : ""}${describeView(e.view).toLowerCase()}`
          : `${e.dashboard.title}: ${plural(e.dashboard.items.length, "view")}`,
    },
    { key: "updated", label: "Changed", align: "right", headClassName: "pr-4", className: "pr-4 text-muted-foreground", render: (e) => fmtAgo(e.kind === "view" ? e.view.updated : e.dashboard.updated) },
  ];
  return (
    <div className="grid gap-2">
      <div className="flex flex-wrap items-center gap-2">
        <a className={buttonVariants({ variant: "outline", size: "sm" })} href={dataHash({ workspace, viewName: NEW_ENTRY })}>
          <Plus aria-hidden="true" />
          New view
        </a>
        <a className={buttonVariants({ variant: "outline", size: "sm" })} href={dataHash({ workspace, dashboard: NEW_ENTRY })}>
          <LayoutDashboard aria-hidden="true" />
          New dashboard
        </a>
        <span className="text-xs text-muted-foreground">Saved reads over the workspace, with params and a display: the admin writes them, a client key runs them.</span>
      </div>
      {error && <p className="text-sm text-critical-text">{error}</p>}
      {entries.length > 0 && (
        <div className="overflow-hidden rounded-lg border">
          <DataGrid
            columns={columns}
            rows={entries}
            rowKey={(e) => `${e.kind}/${e.kind === "view" ? e.view.name : e.dashboard.name}`}
            headerClassName="bg-muted/50"
            rowHref={hrefOf}
            empty="No views"
          />
        </div>
      )}
      {more && <p className="text-xs text-muted-foreground">The first 500 of each.</p>}
    </div>
  );
}

/** A link to a query page, or a disabled button when the workspace has no table it can read (its title says so). */
function QueryButton(props: { href: string; disabled: boolean; title: string; icon: ReactNode; label: string }) {
  if (props.disabled) {
    return (
      // A disabled button takes no pointer events: the span around it shows the title.
      <span title={props.title}>
        <Button type="button" variant="outline" size="sm" disabled>
          {props.icon}
          {props.label}
        </Button>
      </span>
    );
  }
  return (
    <a className={buttonVariants({ variant: "outline", size: "sm" })} href={props.href} title={props.title}>
      {props.icon}
      {props.label}
    </a>
  );
}

/**
 * The workspace's relationships (edge types): each joins rows of its tables (PUT /db/{ws}/_edges)
 * and opens its page. ``joins`` (GET /db/{ws}/_edges, a read of every edge) comes after the list. An admin
 * can drop one unless the service keeps the workspace.
 */
function EdgeTypes({ workspace, onChange }: { workspace: Workspace; onChange(): void }) {
  const client = useApi();
  const details = useEdgeTypes(workspace.name).data;
  const drop = useAction(
    async (name: string) => {
      await client.delete("/db/{workspace}/_edges/{edge_type}", { params: { path: { workspace: workspace.name, edge_type: name } } });
      onChange();
    },
    { success: (_, name) => `Dropped the relationship ${name}.`, failed: (name) => `Could not drop ${name}` },
  );

  const columns: GridColumn<Workspace["edges"][number]>[] = [
    {
      key: "name",
      label: "Relationship",
      headClassName: "pl-4",
      className: "pl-4 font-mono text-[13px] font-medium",
      render: (e) => (
        <a className="inline-flex items-center gap-1.5 hover:underline" href={dataHash({ workspace: workspace.name, edge: e.name })}>
          <Spline className="size-3.5 text-muted-foreground" aria-hidden="true" />
          {e.name}
        </a>
      ),
    },
    {
      key: "joins",
      label: "Joins",
      headTitle: "The tables it goes from and to",
      className: "font-mono text-xs text-muted-foreground",
      render: (e) => details?.find((d) => d.name === e.name)?.joins.map((j) => joinLabel(j)).join(", ") ?? "…",
    },
    { key: "count", label: "Count", align: "right", render: (e) => fmtInt(e.count) },
    {
      key: "size",
      label: "Size",
      align: "right",
      headClassName: "pr-4",
      headTitle: "On disk: its bucket files, which grow a page at a time and never shrink",
      className: "pr-4 text-muted-foreground",
      render: (e) => fmtBytes(e.size_bytes),
    },
    {
      key: "drop",
      headClassName: "w-0 pr-2",
      className: "pr-2 text-right",
      render: (e) =>
        !workspace.managed && (
          <ConfirmButton
            label="Drop"
            title={`Drop the relationship ${e.name}?`}
            body={`All ${fmtInt(e.count)} ${e.name} relationships are deleted; the rows they join stay. This cannot be undone.`}
            busy={drop.running?.[0] === e.name}
            onConfirm={() => drop.run(e.name)}
          />
        ),
    },
  ];
  return (
    <div className="overflow-hidden rounded-lg border">
      <DataGrid columns={columns} rows={workspace.edges} rowKey={(e) => e.name} headerClassName="bg-muted/50" empty="No relationships" />
    </div>
  );
}

/** What the form makes: a table (a graph table, as the API makes them), a relationship (an empty edge type) or a bucket. */
type Making = "table" | "edge" | "bucket";

const MAKING: Record<Making, { label: string; placeholder: string; button: string; hint: string }> = {
  table: {
    label: "Table",
    placeholder: "products",
    button: "Create table",
    hint: "Rows of data, each with an id, such as people or orders. Relationships can link its rows to others.",
  },
  edge: {
    label: "Relationship",
    placeholder: "works_at",
    button: "Create relationship",
    hint: "A kind of link between rows, such as works_at from people to companies. Each link can carry details of its own (a role, a date).",
  },
  bucket: {
    label: "Bucket",
    placeholder: "scans",
    button: "Create bucket",
    hint: "A table of files: upload them on its page, or a client posts each to /db/<workspace>/<bucket>/_files. Each file is a row (its name, size, SHA-256, type and time), its bytes on disk.",
  },
};
const MAKING_OPTIONS = Object.entries(MAKING).map(([value, m]) => [value, m.label]) as [Making, string][];

/** What a new table starts with: no rows, or a sample's schema and rows (samples.ts). */
type Start = "empty" | SampleKey;
const START_OPTIONS = [["empty", "Empty"], ...Object.entries(SAMPLES).map(([key, s]) => [key, s.label])] as [Start, string][];

/**
 * One form for both: what to make, its name, and a line saying what it is; a table can start from a sample.
 * It opens what it made: an empty table at its schema, a sample's at its rows.
 */
function NewForm(props: { workspace: string; graphTables: number; onCreated(): void }) {
  const client = useApi();
  const [making, setMaking] = useState<Making>("table");
  const [name, setName] = useState("");
  const [start, setStart] = useState<Start>("empty");
  const text = MAKING[making];
  const sample = making === "table" && start !== "empty" ? SAMPLES[start] : undefined;

  const choose = (next: Start) => {
    // An empty name, or the one the last sample suggested, takes the new sample's.
    if (!name.trim() || (start !== "empty" && name === SAMPLES[start].table)) setName(next === "empty" ? "" : SAMPLES[next].table);
    setStart(next);
  };

  const create = useAction(
    async (event: FormEvent) => {
      event.preventDefault();
      const path = { workspace: props.workspace };
      if (making === "edge") {
        const made = await client.post("/db/{workspace}/_edges", { params: { path }, body: { name: name.trim() } });
        setName("");
        props.onCreated();
        location.hash = dataHash({ workspace: props.workspace, edge: made.name });
        return;
      }
      if (making === "bucket") {
        const made = await client.post("/db/{workspace}", { params: { path }, body: { name: name.trim(), bucket: true } });
        setName("");
        props.onCreated();
        location.hash = dataHash({ workspace: props.workspace, table: made.name });
        return;
      }
      const made = await client.post("/db/{workspace}", { params: { path }, body: { name: name.trim(), fields: sample?.fields } });
      if (sample) {
        try {
          await client.post("/db/{workspace}/_transaction", { params: { path }, body: sampleTransaction(sample, made.name) });
        } catch (error) {
          // The rows and relationships go in together or not at all; then the table goes too.
          await client.delete("/db/{workspace}/{table}", { params: { path: { ...path, table: made.name } } }).catch(() => undefined);
          throw error;
        }
      }
      setName("");
      setStart("empty");
      props.onCreated();
      location.hash = dataHash({ workspace: props.workspace, table: made.name, view: sample ? undefined : "schema" });
    },
    { notice: true },
  );
  return (
    <div className="grid gap-2">
      <div className="text-sm font-medium">New</div>
      <form className="flex flex-wrap items-center gap-2" onSubmit={create.run}>
        <Segmented<Making> label="What to create" options={MAKING_OPTIONS} value={making} onChange={setMaking} />
        <Input
          aria-label={`Name of the new ${text.label.toLowerCase()}`}
          placeholder={sample?.table ?? text.placeholder}
          pattern="[A-Za-z][A-Za-z0-9]*(_[A-Za-z0-9]+)*"
          title="Letters, digits and single underscores, starting with a letter"
          maxLength={48}
          required
          autoComplete="off"
          className="w-56"
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
        <SubmitButton busy={create.busy} icon={<Plus aria-hidden="true" />}>
          {text.button}
        </SubmitButton>
      </form>
      {making === "table" && (
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-xs text-muted-foreground">Start with</span>
          <Segmented<Start> label="What the table starts with" size="sm" options={START_OPTIONS} value={start} onChange={choose} />
        </div>
      )}
      <p className="text-xs text-muted-foreground">
        {sample ? sample.hint : text.hint}
        {making === "edge" && !props.graphTables && " There is no table here yet for it to join: create one first."}
      </p>
      <Notice notice={create.notice} />
    </div>
  );
}

/** The mark of a plain table (made with ``graph: false``, or before tables were graph tables): relationships cannot join its rows. */
export function NoRelationships({ className }: { className?: string }) {
  return (
    <Badge
      variant="outline"
      className={cn("font-normal text-muted-foreground", className)}
      title="Made as a plain table: relationships cannot join its rows. To link them, create a new table and copy the rows over."
    >
      no relationships
    </Badge>
  );
}

/** The mark of a bucket: a table of files, which only its file routes write. */
export function BucketBadge({ className }: { className?: string }) {
  return (
    <Badge
      variant="outline"
      className={cn("font-normal text-muted-foreground", className)}
      title="A bucket: each row is a file, its bytes on disk. Files are uploaded and deleted here or through /db/<workspace>/<bucket>/_files, never as rows."
    >
      <Files aria-hidden="true" />
      bucket of files
    </Badge>
  );
}

/** The mark of a kept table: a part of the service (``keeper``) keeps it, so it is read-only here. */
export function KeptBadge({ keeper, className }: { keeper: string; className?: string }) {
  return (
    <Badge variant="outline" className={cn("max-w-64 font-normal text-muted-foreground", className)} title={`Read-only here: kept by the service (${keeper}).`}>
      <Lock aria-hidden="true" />
      <span className="truncate">{keeper}</span>
    </Badge>
  );
}
