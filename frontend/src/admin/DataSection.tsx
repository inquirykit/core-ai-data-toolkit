import { useState, type FormEvent, type ReactNode } from "react";
import { Database, Link2Off, Network, Pin, Plus, Search, Spline, SquareTerminal, Table2, X } from "lucide-react";
import { motion } from "motion/react";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";
import { useAction } from "../shared/action";
import type { ApiClient } from "../shared/api";
import { useHash } from "../shared/browser";
import { errorText } from "../shared/query";
import { EmptyCard, Notice, SubmitButton } from "../shared/components";
import { fmtBytes, fmtInt } from "../shared/format";
import { dataHash, locationFromHash, pinnedTables, searchTree, type DataLocation, type EdgeTypeInfo, type Namespace, type TableInfo } from "./data";
import { EdgeTypeBrowser } from "./DataEdgeType";
import { NamespaceCard } from "./DataNamespace";
import { PinToggle, usePins, type Pins } from "./DataPins";
import { QueryPanel } from "./DataQuery";
import { TableBrowser } from "./TableBrowser";
import { DetailSkeleton, ListCard, MasterDetail, NavRow, NoRows } from "./layout";

/**
 * /admin#/data: namespaces with their tables and relationships (edge types) on
 * the left, the chosen namespace, table or relationship on the right. Rows go through the same
 * /db routes clients use; the admin key is accepted there too.
 */
export function DataSection({ client }: { client: ApiClient }) {
  const loc = useHash(locationFromHash);
  const query = client.useQuery("get", "/db", undefined, { select: (d) => d.namespaces });
  const namespaces = query.data ?? null;
  const error = errorText(query.error);
  const reload = query.refetch;
  const pins = usePins();

  const current = namespaces?.find((n) => n.name === loc.namespace);
  return (
    <MasterDetail list={<NamespacesCard namespaces={namespaces} loc={loc} client={client} pins={pins} error={error} reload={reload} />}>
      {!namespaces ? (
        <DetailSkeleton />
      ) : loc.namespace && !current ? (
        <EmptyCard icon={Database} title={`No namespace named ${loc.namespace}`} body="It may have been deleted. Pick one on the left." />
      ) : current && loc.query && !current.tables.length ? (
        <EmptyCard icon={SquareTerminal} title={`No tables in ${current.name} yet`} body="SQL query reads the namespace's tables. Create one on the left, or let a client's first write make it." />
      ) : current && loc.graph && !current.tables.some((t) => t.graph) ? (
        <EmptyCard icon={Network} title={`No tables for relationships in ${current.name}`} body="Graph query reads tables that relationships can join. Create one on the namespace's page (New, Table), then link its rows with relationships." />
      ) : current && loc.query ? (
        <QueryPanel key={`${current.name}/sql`} client={client} namespace={current} language="sql" startTable={loc.queryTable} />
      ) : current && loc.graph ? (
        <QueryPanel
          key={`${current.name}/cypher`}
          client={client}
          namespace={current}
          language="cypher"
          startTable={loc.graphTable}
          startEdge={loc.graphEdge}
          startNode={loc.graphNode}
        />
      ) : current && loc.edge && !current.edges.some((e) => e.name === loc.edge) ? (
        <EmptyCard icon={Spline} title={`No relationship named ${loc.edge} in ${current.name}`} body="It may have been dropped. Create one on the namespace's page (New, Relationship)." />
      ) : current && loc.edge ? (
        <EdgeTypeBrowser key={`${current.name}/_edges/${loc.edge}`} client={client} namespace={current} type={loc.edge} onChange={reload} />
      ) : current && loc.table ? (
        <TableBrowser
          key={`${current.name}/${loc.table}`}
          client={client}
          namespace={current}
          table={loc.table}
          view={loc.view ?? "rows"}
          pins={pins}
          onChange={reload}
        />
      ) : current ? (
        <NamespaceCard namespace={current} client={client} pins={pins} onChange={reload} />
      ) : (
        <EmptyCard
          icon={Database}
          title={namespaces.length ? "Pick a namespace" : "No namespaces yet"}
          body={
            namespaces.length
              ? "Its tables and their rows show here."
              : "Create one on the left, then its tables. Clients write rows to /db/<namespace>/<table>; a table also appears on its first write."
          }
        />
      )}
    </MasterDetail>
  );
}

// ------------------------------------------------------------ namespaces
function NamespacesCard(props: {
  namespaces: Namespace[] | null;
  loc: DataLocation;
  client: ApiClient;
  pins: Pins;
  error: string;
  reload(): Promise<unknown>;
}) {
  const [name, setName] = useState("");
  const [search, setSearch] = useState("");
  const searching = Boolean(search.trim());
  const tree = searchTree(props.namespaces ?? [], search);
  const pinned = pinnedTables(tree, props.pins.keys);

  const create = useAction(
    async (event: FormEvent) => {
      event.preventDefault();
      const made = await props.client.post("/db", { body: { name: name.trim() } });
      setName("");
      await props.reload();
      location.hash = dataHash({ namespace: made.name });
    },
    { notice: true },
  );

  return (
    <ListCard
      title="Namespaces"
      description={props.namespaces ? `${props.namespaces.length} in ArcadeDB` : "Loading…"}
      label="Namespaces and tables"
      onRefresh={props.reload}
      loaded={Boolean(props.namespaces)}
      error={props.error}
      top={props.namespaces?.length ? <TreeSearch value={search} onChange={setSearch} /> : undefined}
      footer={
        <>
          <form className="flex gap-2" onSubmit={create.run}>
            <Input
              aria-label="New namespace"
              placeholder="New namespace"
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
          </form>
          <Notice notice={create.notice} />
        </>
      }
    >
      {pinned.length > 0 && (
        <div role="group" aria-label="Pinned tables" className="mb-1 grid gap-0.5 border-b pb-1">
          <div className="flex items-center gap-1.5 px-2 pb-0.5 text-xs font-medium text-muted-foreground" aria-hidden="true">
            <Pin className="size-3" />
            Pinned
          </div>
          {pinned.map(({ namespace, table }) => (
            <TableLink
              key={`${namespace.name}/${table.name}`}
              namespace={namespace}
              table={table}
              loc={props.loc}
              pins={props.pins}
              label={
                <>
                  <span className="text-muted-foreground">{namespace.name}/</span>
                  {table.name}
                </>
              }
            />
          ))}
        </div>
      )}
      {tree.map(({ namespace: ns, tables, edges }) => {
        const open = ns.name === props.loc.namespace;
        const rows = ns.tables.reduce((sum, t) => sum + t.count, 0);
        return (
          <div key={ns.name}>
            <TreeLink
              href={dataHash({ namespace: ns.name })}
              active={open && !props.loc.table && !props.loc.edge}
              icon={<Database aria-hidden="true" />}
              label={ns.name}
              meta={ns.tables.length ? `${ns.tables.length} · ${fmtBytes(ns.size_bytes)}` : "empty"}
              title={`${ns.tables.length} tables, ${fmtInt(rows)} rows, ${fmtBytes(ns.size_bytes)} on disk`}
            />
            {/* A search opens every namespace it finds tables or relationships in. */}
            {(open || (searching && (tables.length > 0 || edges.length > 0))) && (
              <motion.div
                className="ml-4 grid gap-0.5 border-l py-0.5 pl-2"
                initial={{ opacity: 0, height: 0 }}
                animate={{ opacity: 1, height: "auto" }}
                transition={{ duration: 0.2 }}
              >
                {open && <QueryLinks namespace={ns} loc={props.loc} />}
                <TreeGroups namespace={ns} tables={tables} edges={edges} loc={props.loc} pins={props.pins} />
                {!ns.tables.length && !ns.edges.length && <NoRows>No tables yet</NoRows>}
              </motion.div>
            )}
          </div>
        );
      })}
      {searching && !tree.length && <NoRows>No namespace or table matches “{search.trim()}”</NoRows>}
    </ListCard>
  );
}

/** The search over the tree: it narrows the namespaces and tables listed as it is typed; Escape clears it. */
function TreeSearch({ value, onChange }: { value: string; onChange(value: string): void }) {
  return (
    <div className="relative px-2">
      <Search className="pointer-events-none absolute top-1/2 left-4.5 size-3.5 -translate-y-1/2 text-muted-foreground" aria-hidden="true" />
      <Input
        type="text"
        aria-label="Find a table or namespace"
        placeholder="Find a table (or app/table)"
        title="Matches table and namespace names; app/peo looks for tables like peo in namespaces like app"
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
 * A table's row of the tree, or of the pinned ones (``label`` names its namespace there); ``marked`` shows a
 * pin after the name of a pinned one. In a namespace the service does not keep, a plain table says that
 * relationships cannot join it.
 */
function TableLink(props: { namespace: Namespace; table: TableInfo; loc: DataLocation; pins: Pins; label?: ReactNode; marked?: boolean }) {
  const t = props.table;
  const ns = props.namespace.name;
  const plain = !t.graph && !props.namespace.managed;
  return (
    <TreeLink
      href={dataHash({ namespace: ns, table: t.name })}
      active={ns === props.loc.namespace && t.name === props.loc.table}
      icon={<Table2 aria-hidden="true" />}
      label={props.label ?? t.name}
      note={plain ? <Link2Off className="size-3 shrink-0 text-muted-foreground" aria-label="no relationships" /> : undefined}
      pinned={props.marked && props.pins.has(ns, t.name)}
      pin={<PinToggle pins={props.pins} namespace={ns} table={t.name} />}
      meta={`${fmtInt(t.count)} · ${fmtBytes(t.size_bytes)}`}
      title={`${plain ? "Made as a plain table: relationships cannot join its rows. " : ""}${fmtInt(t.count)} rows, ${fmtBytes(t.size_bytes)} on disk`}
    />
  );
}

/**
 * A namespace's tables and relationships, each under its heading as the New form names them. A namespace
 * with no relationships lists its tables without a heading.
 */
function TreeGroups(props: { namespace: Namespace; tables: TableInfo[]; edges: EdgeTypeInfo[]; loc: DataLocation; pins: Pins }) {
  const { loc, pins } = props;
  const namespace = props.namespace.name;
  const links = props.tables.map((t) => <TableLink key={t.name} namespace={props.namespace} table={t} loc={loc} pins={pins} marked />);
  if (!props.edges.length) return <>{links}</>;
  return (
    <>
      {links.length > 0 && <TreeGroup label="Tables">{links}</TreeGroup>}
      {props.edges.length > 0 && (
        <TreeGroup label="Relationships" apart={links.length > 0}>
          {props.edges.map((e) => (
            <TreeLink
              key={e.name}
              href={dataHash({ namespace, edge: e.name })}
              active={namespace === loc.namespace && e.name === loc.edge}
              icon={<Spline aria-hidden="true" />}
              label={e.name}
              meta={`${fmtInt(e.count)} · ${fmtBytes(e.size_bytes)}`}
              title={`Relationship: links rows of tables. ${fmtInt(e.count)} of them, ${fmtBytes(e.size_bytes)} on disk`}
            />
          ))}
        </TreeGroup>
      )}
    </>
  );
}

/** Rows of the tree under a small heading; ``apart`` draws a line above, after the group before it. */
function TreeGroup({ label, apart, children }: { label: string; apart?: boolean; children: ReactNode }) {
  return (
    <div role="group" aria-label={label} className={cn("grid gap-0.5", apart && "mt-1 border-t pt-1")}>
      <div className="px-2 pb-0.5 text-xs font-medium text-muted-foreground" aria-hidden="true">
        {label}
      </div>
      {children}
    </div>
  );
}

/**
 * The namespace's two query pages, above its tables and set apart from them, each named in its own hue: SQL
 * over any table, Cypher (the Graph query) over the graph tables. Each is grayed out until there is a
 * table it can read.
 */
function QueryLinks({ namespace, loc }: { namespace: Namespace; loc: DataLocation }) {
  const tables = namespace.tables.length;
  const graphTables = namespace.tables.filter((t) => t.graph).length;
  const edges = namespace.edges.reduce((sum, e) => sum + e.count, 0);
  return (
    <div className="mb-1 grid gap-0.5 border-b pb-1">
      <QueryLink
        tone="sql"
        href={dataHash({ namespace: namespace.name, query: true })}
        active={Boolean(loc.query)}
        disabled={!tables}
        icon={<SquareTerminal aria-hidden="true" />}
        label="SQL query"
        meta={tables ? `${tables} ${tables === 1 ? "table" : "tables"}` : "no tables"}
        title={tables ? "Run a read-only SQL query over the namespace's tables" : "Create a table first: SQL reads the namespace's tables"}
      />
      <QueryLink
        tone="graph"
        href={dataHash({ namespace: namespace.name, graph: true })}
        active={Boolean(loc.graph)}
        disabled={!graphTables}
        icon={<Network aria-hidden="true" />}
        label="Graph query"
        meta={graphTables ? `${fmtInt(edges)} ${edges === 1 ? "relationship" : "relationships"}` : "no tables"}
        title={
          graphTables
            ? "Run a read-only Cypher query over the tables and relationships, and draw what it returns"
            : "Create a table first: Graph query reads tables that relationships can join"
        }
      />
    </div>
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
 * keyboard focus: a button cannot go inside the row's link.
 */
function TreeLink(props: {
  href: string;
  active: boolean;
  icon: ReactNode;
  label: ReactNode;
  note?: ReactNode;
  pinned?: boolean;
  pin?: ReactNode;
  meta: string;
  title: string;
}) {
  const link = (
    <NavRow href={props.href} title={props.title} active={props.active}>
      {props.icon}
      <span className="truncate font-mono text-[13px]">{props.label}</span>
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
    <div className="group/row relative">
      {link}
      <span className="absolute top-1 right-1 opacity-0 transition-opacity group-hover/row:opacity-100 group-has-[:focus-visible]/row:opacity-100">
        {props.pin}
      </span>
    </div>
  );
}
