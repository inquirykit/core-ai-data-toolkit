import { Fragment, useState, type FormEvent, type ReactNode } from "react";
import { ChevronRight, Database, Files, LayoutDashboard, Link2Off, Lock, Network, Pin, Plus, Search, Shapes, Spline, SquareTerminal, Table2, X } from "lucide-react";
import { motion } from "motion/react";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";
import { useAction } from "../../shared/action";
import { useApi } from "../../shared/api";
import { useHash } from "../../shared/browser";
import { errorText } from "../../shared/query";
import { EmptyCard, Notice, SubmitButton } from "../../shared/components";
import { fmtBytes, fmtInt, plural } from "../../shared/format";
import {
  dataHash,
  groupsOf,
  isOwn,
  locationFromHash,
  pinnedTables,
  searchTree,
  type DataLocation,
  type EdgeTypeEntry,
  type TableEntry,
  type TableInfo,
  type TreeEntry,
  type Workspace,
  type WorkspaceEntry,
} from "./data";
import { DashboardPage } from "./views/DataDashboard";
import { EdgeTypeBrowser } from "./graph/DataEdgeType";
import { WorkspaceCard } from "./DataWorkspace";
import { PinToggle, usePins, type Pins } from "./DataPins";
import { QueryPanel } from "./query/DataQuery";
import { useWorkspaceViews, ViewPage } from "./views/DataViews";
import { ViewIcon } from "./views/ViewDisplay";
import { TableBrowser } from "./tables/TableBrowser";
import { WorkspaceDiagram } from "./diagram/DataDiagram";
import { DetailSkeleton, ListCard, MasterDetail, NavRow, NoRows } from "../layout";

/**
 * /admin#/data: workspaces with their tables, graph, vectors, buckets and views on the left, and after them, folded
 * away, the service's own (evals, jobs...), read-only here; the chosen workspace, table, relationship, view or
 * dashboard on the right. Rows go through the same /db routes clients use; the admin key is accepted there too.
 */
export function DataSection() {
  const client = useApi();
  const loc = useHash(locationFromHash);
  // The page holds the admin key, which ?all=true needs: the service's own come too. The list names every
  // workspace's tables and edge types; the open workspace is read whole, with their counts, sizes and schemas.
  const query = client.useQuery("get", "/db", { params: { query: { all: true } } });
  const all = query.data?.workspaces ?? null;
  // A workspace past the list's first page is opened by its name all the same.
  const known = Boolean(loc.workspace && (all?.some((w) => w.name === loc.workspace) || query.data?.next));
  const opened = client.useQuery("get", "/db/{workspace}", { params: { path: { workspace: loc.workspace ?? "" } } }, { enabled: known });
  // The workspace the location names, never the one before it, which the query shows until the new one arrives.
  const current = known && opened.data && opened.data.name === loc.workspace ? opened.data : null;
  const error = errorText(query.error);
  const reload = async () => {
    await Promise.all([query.refetch(), known && opened.refetch()]);
  };
  const pins = usePins();

  const workspaces = all?.filter((w) => !isOwn(w)).length ?? 0;
  return (
    <MasterDetail
      list={<WorkspacesCard all={all} more={Boolean(query.data?.next)} current={current} loc={loc} pins={pins} error={error} reload={reload} />}
    >
      {!all ? (
        <DetailSkeleton />
      ) : loc.workspace && !known ? (
        <EmptyCard icon={Database} title={`No workspace named ${loc.workspace}`} body="It may have been deleted. Pick one on the left." />
      ) : loc.workspace && !current ? (
        opened.error ? (
          <EmptyCard icon={Database} title={`${loc.workspace} could not be read`} body={errorText(opened.error)} />
        ) : (
          <DetailSkeleton />
        )
      ) : current && loc.query && !current.tables.length ? (
        <EmptyCard icon={SquareTerminal} title={`No tables in ${current.name} yet`} body="SQL query reads the workspace's tables. Create one on the left, or let a client's first write make it." />
      ) : current && loc.graph && !current.tables.some((t) => t.graph) ? (
        <EmptyCard icon={Network} title={`No tables for relationships in ${current.name}`} body="Graph query reads tables that relationships can join. Create one on the workspace's page (New, Table), then link its rows with relationships." />
      ) : current && loc.diagram && !current.tables.length ? (
        <EmptyCard icon={Shapes} title={`No tables in ${current.name} yet`} body="The diagram draws the workspace's tables and what joins them. Create one on the left, or let a client's first write make it." />
      ) : current && loc.diagram ? (
        <WorkspaceDiagram key={`${current.name}/diagram`} workspace={current} />
      ) : current && loc.query ? (
        <QueryPanel key={`${current.name}/sql`} workspace={current} kind="sql" startTable={loc.queryTable} />
      ) : current && loc.graph ? (
        <QueryPanel
          key={`${current.name}/graph`}
          workspace={current}
          kind="graph"
          startTable={loc.graphTable}
          startEdge={loc.graphEdge}
          startNode={loc.graphNode}
        />
      ) : current && loc.viewName ? (
        <ViewPage key={`${current.name}/_views/${loc.viewName}`} workspace={current} name={loc.viewName} values={loc.values ?? {}} />
      ) : current && loc.dashboard ? (
        <DashboardPage key={`${current.name}/_dashboards/${loc.dashboard}`} workspace={current} name={loc.dashboard} values={loc.values ?? {}} />
      ) : current && loc.edge && !current.edges.some((e) => e.name === loc.edge) ? (
        <EmptyCard icon={Spline} title={`No relationship named ${loc.edge} in ${current.name}`} body="It may have been dropped. Create one on the workspace's page (New, Relationship)." />
      ) : current && loc.edge ? (
        <EdgeTypeBrowser key={`${current.name}/_edges/${loc.edge}`} workspace={current} type={loc.edge} onChange={reload} />
      ) : current && loc.table ? (
        <TableBrowser
          key={`${current.name}/${loc.table}?${loc.filter?.join("=") ?? ""}`}
          workspace={current}
          table={loc.table}
          view={loc.view ?? "rows"}
          row={loc.row}
          filter={loc.filter}
          pins={pins}
          onChange={reload}
        />
      ) : current ? (
        <WorkspaceCard key={current.name} workspace={current} pins={pins} onChange={reload} />
      ) : (
        <EmptyCard
          icon={Database}
          title={workspaces ? "Pick a workspace" : "No workspaces yet"}
          body={
            workspaces
              ? "Its tables and their rows show here."
              : "Create one on the left, then its tables. Clients write rows to /db/<workspace>/<table>; a table also appears on its first write."
          }
        />
      )}
    </MasterDetail>
  );
}

// ------------------------------------------------------------ workspaces
function WorkspacesCard(props: {
  /** The workspaces and the service's own, as GET /db?all=true lists them */
  all: WorkspaceEntry[] | null;
  /** Whether GET /db has more than the page listed */
  more: boolean;
  /** The open workspace, read whole (GET /db/{workspace}): its tables' counts and sizes */
  current: Workspace | null;
  loc: DataLocation;
  pins: Pins;
  error: string;
  reload(): Promise<unknown>;
}) {
  const client = useApi();
  const [name, setName] = useState("");
  const [label, setLabel] = useState("");
  const [search, setSearch] = useState("");
  // Whether the service's own are unfolded, once the admin has said; until then, while one of them is shown.
  const [ownOpen, setOwnOpen] = useState<boolean | null>(null);
  const searching = Boolean(search.trim());
  const workspaces = props.all?.filter((w) => !isOwn(w)) ?? [];
  const own = props.all?.filter(isOwn) ?? [];
  const tree = searchTree(workspaces, search);
  const ownTree = searchTree(own, search);
  const pinned = pinnedTables([...tree, ...ownTree], props.pins.keys);
  // A search unfolds them when it finds something there.
  const showOwn = (searching && ownTree.length > 0) || (ownOpen ?? own.some((w) => w.name === props.loc.workspace));

  const create = useAction(
    async (event: FormEvent) => {
      event.preventDefault();
      const made = await client.post("/db", { body: { name: name.trim(), label: label.trim() || undefined } });
      setName("");
      setLabel("");
      await props.reload();
      location.hash = dataHash({ workspace: made.name });
    },
    { notice: true },
  );

  const detailsOf = (workspace: string) => (props.current?.name === workspace ? props.current : undefined);
  const entry = (e: TreeEntry) => (
    <WorkspaceTree key={e.workspace.name} entry={e} details={detailsOf(e.workspace.name)} loc={props.loc} pins={props.pins} searching={searching} />
  );
  return (
    <ListCard
      title="Workspaces"
      description={props.all ? `${plural(workspaces.length, "workspace")} in the database${props.more ? ", the first by name" : ""}` : "Loading…"}
      label="Workspaces and tables"
      onRefresh={props.reload}
      loaded={Boolean(props.all)}
      error={props.error}
      top={props.all?.length ? <TreeSearch value={search} onChange={setSearch} /> : undefined}
      footer={
        <>
          <form className="grid gap-2" onSubmit={create.run}>
            <div className="flex gap-2">
              <Input
                aria-label="New workspace"
                placeholder="New workspace"
                pattern="[A-Za-z][A-Za-z0-9]*(_[A-Za-z0-9]+)*"
                title="Letters, digits and single underscores, starting with a letter"
                maxLength={48}
                required
                autoComplete="off"
                className="h-8"
                value={name}
                onChange={(e) => setName(e.target.value)}
              />
              <SubmitButton size="sm" busy={create.busy} icon={<Plus aria-hidden="true" />}>
                Create
              </SubmitButton>
            </div>
            <Input
              aria-label="Label of the new workspace"
              placeholder="Label (optional)"
              title="What it holds, in words: shown beside its name"
              maxLength={200}
              autoComplete="off"
              className="h-8"
              value={label}
              onChange={(e) => setLabel(e.target.value)}
            />
          </form>
          <Notice notice={create.notice} />
        </>
      }
    >
      {pinned.length > 0 && (
        <div role="group" aria-label="Pinned tables" className="mb-1 grid min-w-0 gap-0.5 border-b pb-1">
          <div className="flex items-center gap-1.5 px-2 pb-0.5 text-xs font-medium text-muted-foreground" aria-hidden="true">
            <Pin className="size-3" />
            Pinned
          </div>
          {pinned.map(({ workspace, table }) => (
            <TableLink
              key={`${workspace.name}/${table.name}`}
              workspace={workspace}
              table={table}
              info={detailsOf(workspace.name)?.tables.find((t) => t.name === table.name)}
              loc={props.loc}
              pins={props.pins}
              label={
                <>
                  <span className="text-muted-foreground">{workspace.name}/</span>
                  {table.name}
                </>
              }
              name={`${workspace.name}/${table.name}`}
            />
          ))}
        </div>
      )}
      {tree.map(entry)}
      {searching && !tree.length && !ownTree.length && <NoRows>No workspace or table matches “{search.trim()}”</NoRows>}
      {(searching ? ownTree.length > 0 : own.length > 0) && (
        <Collapsible open={showOwn} onOpenChange={setOwnOpen} className="mt-1 border-t pt-1">
          <CollapsibleTrigger
            className="group flex h-8 w-full items-center gap-2 rounded-md px-2 text-left text-xs font-medium text-muted-foreground outline-none hover:bg-muted hover:text-foreground focus-visible:ring-[3px] focus-visible:ring-ring/50"
            title="Where the service keeps its own data (evals, jobs, enrichers, the playground): browsable, read-only here"
          >
            <ChevronRight className="size-3.5 transition-transform duration-200 group-data-[state=open]:rotate-90" aria-hidden="true" />
            The service's own
            <span className="ml-auto tabular-nums">{own.length}</span>
          </CollapsibleTrigger>
          <CollapsibleContent className="grid gap-0.5">{ownTree.map(entry)}</CollapsibleContent>
        </Collapsible>
      )}
    </ListCard>
  );
}

/**
 * A workspace's row of the tree, its label beside its name, and under it, while it is open or a search finds
 * something in it, its contents in their groups (groupsOf): SQL query first, set apart. ``details``, the open
 * workspace read whole, gives the counts and sizes.
 */
function WorkspaceTree(props: { entry: TreeEntry; details?: Workspace; loc: DataLocation; pins: Pins; searching: boolean; }) {
  const { entry, details, loc, pins, searching } = props;
  const { workspace: ws, tables, edges } = entry;
  const open = ws.name === loc.workspace;
  const rows = details?.tables.reduce((sum, t) => sum + t.count, 0) ?? 0;
  const buckets = ws.tables.some((t) => t.bucket) ? ", besides the buckets' files" : "";
  return (
    <div className="min-w-0">
      <TreeLink
        href={dataHash({ workspace: ws.name })}
        active={open && !loc.table && !loc.edge && !loc.viewName && !loc.dashboard}
        icon={<Database aria-hidden="true" />}
        label={
          <>
            {ws.name}
            {ws.label && <span className="ml-1.5 font-sans text-muted-foreground">{ws.label}</span>}
          </>
        }
        name={ws.label ? `${ws.name}: ${ws.label}` : ws.name}
        meta={!ws.tables.length ? "empty" : details ? `${ws.tables.length} · ${fmtBytes(details.size_bytes)}` : String(ws.tables.length)}
        title={`${ws.label ? `${ws.label}. ` : ""}${plural(ws.tables.length, "table")}${details ? `, ${fmtInt(rows)} rows, ${fmtBytes(details.size_bytes)} on disk${buckets}` : ""}`}
      />
      {/* A search opens every workspace it finds tables or relationships in. */}
      {(open || (searching && (tables.length > 0 || edges.length > 0))) && (
        <motion.div
          className="ml-4 grid min-w-0 gap-0.5 border-l py-0.5 pl-2"
          initial={{ opacity: 0, height: 0 }}
          animate={{ opacity: 1, height: "auto" }}
          transition={{ duration: 0.2 }}
        >
          {open && <SqlLink workspace={ws} loc={loc} />}
          <TreeGroups workspace={ws} details={details} tables={tables} edges={edges} loc={loc} pins={pins} graphQuery={open} />
          {!ws.tables.length && !ws.edges.length && <NoRows>No tables yet</NoRows>}
          {open && <ViewsGroup workspace={ws.name} loc={loc} apart={ws.tables.length > 0 || ws.edges.length > 0} />}
        </motion.div>
      )}
    </div>
  );
}

/** The search over the tree: it narrows the workspaces and tables listed as it is typed; Escape clears it. */
function TreeSearch({ value, onChange }: { value: string; onChange(value: string): void }) {
  return (
    <div className="relative px-2">
      <Search className="pointer-events-none absolute top-1/2 left-4.5 size-3.5 -translate-y-1/2 text-muted-foreground" aria-hidden="true" />
      <Input
        type="text"
        aria-label="Find a table or workspace"
        placeholder="Find a table (or app/table)"
        title="Matches table names, and workspace names and labels; app/peo looks for tables like peo in workspaces like app"
        autoComplete="off"
        spellCheck={false}
        className="h-8 pr-7 pl-7 text-sm"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={(e) => e.key === "Escape" && onChange("")}
      />
      {value && (
        <button
          type="button"
          aria-label="Clear the search"
          className="absolute top-1/2 right-4 -translate-y-1/2 text-muted-foreground hover:text-foreground"
          onClick={() => onChange("")}
        >
          <X className="size-3.5" />
        </button>
      )}
    </div>
  );
}

/**
 * A table's row of the tree, or of the pinned ones (``label`` names its workspace there); ``marked`` shows a
 * pin after the name of a pinned one. In a workspace (not the service's own), a kept table shows a lock naming
 * what keeps it, and a plain table says that relationships cannot join it; a bucket has an icon of its own.
 * ``info`` (its workspace read whole, when open) gives its count and size.
 */
function TableLink(props: { workspace: WorkspaceEntry; table: TableEntry; info?: TableInfo; loc: DataLocation; pins: Pins; label?: ReactNode; name?: string; marked?: boolean }) {
  const t = props.table;
  const info = props.info;
  const ws = props.workspace.name;
  const plain = !t.graph && !t.kept && !t.bucket && !isOwn(props.workspace);
  const kept = t.kept ? `Read-only here: kept by the service (${t.kept}). ` : "";
  return (
    <TreeLink
      href={dataHash({ workspace: ws, table: t.name })}
      active={ws === props.loc.workspace && t.name === props.loc.table}
      icon={t.bucket ? <Files aria-label="bucket" /> : <Table2 aria-hidden="true" />}
      label={props.label ?? t.name}
      name={props.name ?? t.name}
      note={
        t.kept ? (
          <Lock className="size-3 shrink-0 text-muted-foreground" aria-label={`kept by the service (${t.kept})`} />
        ) : plain ? (
          <Link2Off className="size-3 shrink-0 text-muted-foreground" aria-label="no relationships" />
        ) : undefined
      }
      pinned={props.marked && props.pins.has(ws, t.name)}
      pin={<PinToggle pins={props.pins} workspace={ws} table={t.name} />}
      meta={info ? `${fmtInt(info.count)} · ${fmtBytes(info.size_bytes)}` : ""}
      title={`${kept}${plain ? "Made as a plain table: relationships cannot join its rows. " : ""}${
        !info ? (t.bucket ? "A bucket" : "A table") : t.bucket ? `A bucket: ${fmtInt(info.count)} files; their rows take ${fmtBytes(info.size_bytes)} on disk` : `${fmtInt(info.count)} rows, ${fmtBytes(info.size_bytes)} on disk`
      }`}
    />
  );
}

/**
 * A workspace's contents in their groups (groupsOf): Tables, Graph (the Graph query, with ``graphQuery``, and
 * the relationships), Vectors and Buckets, each under its heading. A workspace with only tables lists them
 * without one.
 */
function TreeGroups(props: { workspace: WorkspaceEntry; details?: Workspace; tables: TableEntry[]; edges: EdgeTypeEntry[]; loc: DataLocation; pins: Pins; graphQuery: boolean }) {
  const { loc, pins, details } = props;
  const workspace = props.workspace.name;
  const groups = groupsOf(props, props.graphQuery);
  const headed = groups.length > 1 || groups[0]?.key !== "tables";
  return (
    <>
      {groups.map((group, i) => {
        const rows = (
          <>
            {group.key === "graph" && props.graphQuery && <GraphQueryLink workspace={props.workspace} details={details} loc={loc} />}
            {group.tables.map((t) => (
              <TableLink key={t.name} workspace={props.workspace} table={t} info={details?.tables.find((i) => i.name === t.name)} loc={loc} pins={pins} marked />
            ))}
            {group.edges.map((e) => {
              const info = details?.edges.find((i) => i.name === e.name);
              return (
                <TreeLink
                  key={e.name}
                  href={dataHash({ workspace, edge: e.name })}
                  active={workspace === loc.workspace && e.name === loc.edge}
                  icon={<Spline aria-hidden="true" />}
                  label={e.name}
                  name={e.name}
                  meta={info ? `${fmtInt(info.count)} · ${fmtBytes(info.size_bytes)}` : ""}
                  title={`Relationship: links rows of tables.${info ? ` ${fmtInt(info.count)} of them, ${fmtBytes(info.size_bytes)} on disk` : ""}`}
                />
              );
            })}
          </>
        );
        return headed ? (
          <TreeGroup key={group.key} label={group.label} apart={i > 0}>
            {rows}
          </TreeGroup>
        ) : (
          <Fragment key={group.key}>{rows}</Fragment>
        );
      })}
    </>
  );
}

/**
 * The open workspace's fifth group, Views: its dashboards, then its views, each opening its page; none while it has
 * neither (its page makes them).
 */
function ViewsGroup({ workspace, loc, apart }: { workspace: string; loc: DataLocation; apart: boolean }) {
  const { views, dashboards, more } = useWorkspaceViews(workspace);
  if (!views?.length && !dashboards?.length) return null;
  const here = workspace === loc.workspace;
  return (
    <TreeGroup label="Views" apart={apart}>
      {(dashboards ?? []).map((d) => (
        <TreeLink
          key={`d/${d.name}`}
          href={dataHash({ workspace, dashboard: d.name })}
          active={here && loc.dashboard === d.name}
          icon={<LayoutDashboard aria-hidden="true" />}
          label={d.name}
          name={`${d.title} (${d.name})`}
          meta={`${d.items.length} ${d.items.length === 1 ? "view" : "views"}`}
          title={`Dashboard: ${d.title}`}
        />
      ))}
      {(views ?? []).map((v) => (
        <TreeLink
          key={`v/${v.name}`}
          href={dataHash({ workspace, viewName: v.name })}
          active={here && loc.viewName === v.name}
          icon={<ViewIcon type={v.display.type} />}
          label={v.name}
          name={v.label ? `${v.label} (${v.name})` : v.name}
          meta={v.display.type}
          title={`View${v.label ? `: ${v.label}` : ""}, a ${v.display.type} of ${v.source.kind === "sql" ? "SQL" : v.source.table}`}
        />
      ))}
      {more && <NoRows>The first 500 of each</NoRows>}
    </TreeGroup>
  );
}

/** Rows of the tree under a small heading; ``apart`` draws a line above, after the group before it. */
function TreeGroup({ label, apart, children }: { label: string; apart?: boolean; children: ReactNode }) {
  return (
    <div role="group" aria-label={label} className={cn("grid min-w-0 gap-0.5", apart && "mt-1 border-t pt-1")}>
      <div className="px-2 pb-0.5 text-xs font-medium text-muted-foreground" aria-hidden="true">
        {label}
      </div>
      {children}
    </div>
  );
}

/** The workspace's SQL query page, above its groups and set apart from them; grayed out until it has a table. */
function SqlLink({ workspace, loc }: { workspace: WorkspaceEntry; loc: DataLocation }) {
  const tables = workspace.tables.length;
  return (
    <div className="mb-1 grid min-w-0 gap-0.5 border-b pb-1">
      <QueryLink
        tone="sql"
        href={dataHash({ workspace: workspace.name, query: true })}
        active={Boolean(loc.query)}
        disabled={!tables}
        icon={<SquareTerminal aria-hidden="true" />}
        label="SQL query"
        meta={tables ? `${tables} ${tables === 1 ? "table" : "tables"}` : "no tables"}
        title={tables ? "Run a read-only SQL query over the workspace's tables" : "Create a table first: SQL reads the workspace's tables"}
      />
    </div>
  );
}

/** The workspace's Graph query page, first in its Graph group: SQL over the relationships, drawn, grayed out until it has a table. */
function GraphQueryLink({ workspace, details, loc }: { workspace: WorkspaceEntry; details?: Workspace; loc: DataLocation }) {
  const graphTables = workspace.tables.filter((t) => t.graph).length;
  const edges = details?.edges.reduce((sum, e) => sum + e.count, 0);
  return (
    <QueryLink
      tone="graph"
      href={dataHash({ workspace: workspace.name, graph: true })}
      active={Boolean(loc.graph)}
      disabled={!graphTables}
      icon={<Network aria-hidden="true" />}
      label="Graph query"
      meta={!graphTables ? "no tables" : edges === undefined ? "" : `${fmtInt(edges)} ${edges === 1 ? "relationship" : "relationships"}`}
      title={
        graphTables
          ? "Run a read-only SQL query over the relationships, and draw the ones it returns"
          : "Create a table first: Graph query reads tables that relationships can join"
      }
    />
  );
}

/** Each query page's hue (globals.css), on its name and icon. */
const QUERY_TONES = {
  sql: "text-query-sql [&_svg]:text-query-sql",
  graph: "text-query-graph [&_svg]:text-query-graph",
};

/** A query page's row: its name and icon in its hue and the sans font (a table's name is monospace), an italic meta; the name is never cut. */
function QueryLink(props: { tone: keyof typeof QUERY_TONES; href: string; active: boolean; disabled: boolean; icon: ReactNode; label: string; meta: string; title: string }) {
  return (
    <NavRow
      href={props.href}
      title={props.title}
      active={props.active && !props.disabled}
      disabled={props.disabled}
      className={QUERY_TONES[props.tone]}
    >
      {props.icon}
      <span className="shrink-0 text-[13px]">{props.label}</span>
      <span className="ml-auto min-w-0 truncate text-xs text-muted-foreground italic">{props.meta}</span>
    </NavRow>
  );
}

/**
 * One row of the sidebar tree: a ``note`` (a small icon) after the name, and a pin when ``pinned``. ``pin`` (a PinToggle) takes the place of the meta while the row is hovered or has
 * keyboard focus: a button cannot go inside the row's link. A long name is cut to the row's width, ``name`` in full
 * as its tooltip, so the counts stay in view.
 */
function TreeLink(props: {
  href: string;
  active: boolean;
  icon: ReactNode;
  label: ReactNode;
  /** The name in full, for the label's tooltip */
  name: string;
  note?: ReactNode;
  pinned?: boolean;
  pin?: ReactNode;
  meta: string;
  title: string;
}) {
  const link = (
    <NavRow href={props.href} title={props.title} active={props.active}>
      {props.icon}
      <span className="min-w-0 truncate font-mono text-[13px]" title={props.name}>
        {props.label}
      </span>
      {props.note}
      {props.pinned && <Pin className="fill-current" aria-label="Pinned" />}
      <span
        className={cn(
          "ml-auto shrink-0 text-xs text-muted-foreground tabular-nums",
          props.pin && "transition-opacity group-hover/row:opacity-0 group-has-[:focus-visible]/row:opacity-0",
        )}
      >
        {props.meta}
      </span>
    </NavRow>
  );
  if (!props.pin) return link;
  return (
    <div className="group/row relative min-w-0">
      {link}
      <span className="absolute top-1 right-1 opacity-0 transition-opacity group-hover/row:opacity-100 group-has-[:focus-visible]/row:opacity-100">
        {props.pin}
      </span>
    </div>
  );
}
