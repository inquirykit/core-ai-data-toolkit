// A namespace of the Data section, when no table of it is chosen: one form
// that makes a table (empty, or a sample's schema and rows) or a relationship; its tables and relationships (with the
// tables each joins); and how a client writes a row. "Relationship" is the
// dashboard's word for the API's edge type. A table is a graph table, which
// relationships can join, unless it was made plain: those are marked.
import { useState, type FormEvent, type ReactNode } from "react";
import { Network, Plus, Spline, SquareTerminal } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button, buttonVariants } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";
import { useAction } from "../../shared/action";
import type { ApiClient } from "../../shared/api";
import { CodeBlock, ConfirmButton, CopyButton, Notice, Segmented, SubmitButton } from "../../shared/components";
import { fmtBytes, fmtInt, plural } from "../../shared/format";
import { DataGrid, type GridColumn } from "../../shared/grid";
import { dataHash, joinLabel, type Namespace, type TableInfo } from "./data";
import { useEdgeTypes } from "./DataEdgeType";
import { PinToggle, type Pins } from "./DataPins";
import { SAMPLES, sampleTransaction, type SampleKey } from "./samples";

export function NamespaceCard({ namespace, client, pins, onChange }: { namespace: Namespace; client: ApiClient; pins: Pins; onChange(): void }) {
  const rows = namespace.tables.reduce((sum, t) => sum + t.count, 0);
  const edges = namespace.edges.reduce((sum, e) => sum + e.count, 0);
  const graphTables = namespace.tables.filter((t) => t.graph).length;
  const example = `curl -X PUT ${location.origin}/db/${namespace.name}/todos/1 \\
  -H 'X-API-Key: <API_KEY>' -H 'Content-Type: application/json' \\
  -d '{"title": "Hello", "tags": ["first"]}'`;

  const drop = useAction(
    async () => {
      await client.delete("/db/{namespace}", { params: { path: { namespace: namespace.name } } });
      location.hash = dataHash({});
      onChange();
    },
    { success: `Deleted ${namespace.name}.`, failed: `Could not delete ${namespace.name}` },
  );

  return (
    <Card className="gap-4">
      <CardHeader>
        <CardTitle className="font-mono text-lg">{namespace.name}</CardTitle>
        <CardDescription>
          {namespace.tables.length} tables · {fmtInt(rows)} rows
          {namespace.edges.length > 0 && ` · ${plural(edges, "relationship")}`} · {fmtBytes(namespace.size_bytes)} on disk
        </CardDescription>
        <CardAction className="flex flex-wrap justify-end gap-2">
          <QueryButton
            href={dataHash({ namespace: namespace.name, query: true })}
            disabled={!namespace.tables.length}
            title={namespace.tables.length ? "Run a read-only SQL query over the namespace's tables" : "Create a table first: SQL reads the namespace's tables"}
            icon={<SquareTerminal className="text-query-sql" aria-hidden="true" />}
            label="SQL query"
          />
          <QueryButton
            href={dataHash({ namespace: namespace.name, graph: true })}
            disabled={!graphTables}
            title={
              graphTables
                ? "Run a read-only Cypher query over the tables and relationships, and draw what it returns"
                : "Create a table first: Graph query reads tables that relationships can join"
            }
            icon={<Network className="text-query-graph" aria-hidden="true" />}
            label="Graph query"
          />
          {!namespace.managed && (
            <ConfirmButton
              label="Delete namespace"
              title={`Delete ${namespace.name}?`}
              body={`Its ${namespace.tables.length} tables and ${fmtInt(rows)} rows are deleted with it. This cannot be undone.`}
              busy={drop.busy}
              onConfirm={drop.run}
            />
          )}
        </CardAction>
      </CardHeader>
      <CardContent className="grid gap-5">
        {namespace.managed ? (
          <Notice notice={{ text: `Read-only here: kept by the service (${namespace.managed}).` }} />
        ) : (
          <NewForm namespace={namespace.name} graphTables={graphTables} client={client} onCreated={onChange} />
        )}
        {namespace.tables.length > 0 && (
          <Group title={namespace.edges.length ? "Tables" : undefined}>
            <TableList namespace={namespace.name} tables={namespace.tables} markPlain={!namespace.managed} pins={pins} />
          </Group>
        )}
        {namespace.edges.length > 0 && (
          <Group title="Relationships">
            <EdgeTypes namespace={namespace} client={client} onChange={onChange} />
          </Group>
        )}
        <div className="grid gap-2">
          <p className="text-sm text-muted-foreground">
            {namespace.tables.length ? "Clients write rows like this (a missing table is made on the first write):" : "No tables yet. Create one above (a sample comes with rows to try), or let a client's first write make it:"}
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

/** A part of the namespace's page under its heading: its tables or its relationships; no heading when it has only tables. */
function Group({ title, children }: { title?: string; children: ReactNode }) {
  return (
    <section className="grid gap-2" aria-label={title}>
      {title && <h3 className="text-sm font-medium">{title}</h3>}
      {children}
    </section>
  );
}

/** The tables: each opens its page, and can be pinned. With ``markPlain``, a plain one says relationships cannot join it. */
function TableList({ namespace, tables, markPlain, pins }: { namespace: string; tables: TableInfo[]; markPlain: boolean; pins: Pins }) {
  const columns: GridColumn<TableInfo>[] = [
    {
      key: "table",
      label: "Table",
      headClassName: "pl-4",
      className: "pl-4",
      render: (t) => (
        <>
          <a className="font-mono text-[13px] font-medium hover:underline" href={dataHash({ namespace, table: t.name })}>
            {t.name}
          </a>
          {markPlain && !t.graph && <NoRelationships className="ml-2" />}
        </>
      ),
    },
    {
      key: "schema",
      label: "Schema",
      className: "font-mono text-xs text-muted-foreground",
      render: (t) => (t.fields.length ? `${t.fields.length} fields${t.strict ? ", strict" : ""}` : "none"),
    },
    { key: "rows", label: "Rows", align: "right", render: (t) => fmtInt(t.count) },
    {
      key: "size",
      label: "Size",
      align: "right",
      headTitle: "On disk: the table's rows, its indexes and its rows' lists of relationships, in files that grow a page at a time and never shrink",
      className: "text-muted-foreground",
      render: (t) => fmtBytes(t.size_bytes),
    },
    {
      key: "pin",
      label: <span className="sr-only">Pinned</span>,
      headClassName: "w-0 pr-2",
      className: "pr-2 text-right",
      render: (t) => <PinToggle pins={pins} namespace={namespace} table={t.name} />,
    },
  ];
  return (
    <div className="overflow-hidden rounded-lg border">
      <DataGrid columns={columns} rows={tables} rowKey={(t) => t.name} headerClassName="bg-muted/50" rowHref={(t) => dataHash({ namespace, table: t.name })} empty="No tables" />
    </div>
  );
}

/** A link to a query page, or a disabled button when the namespace has no table it can read (its title says so). */
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
 * The namespace's relationships (edge types): each joins rows of its tables (PUT /db/{ns}/_edges)
 * and opens its page. ``joins`` (GET /db/{ns}/_edges, a read of every edge) comes after the list. An admin
 * can drop one unless the service keeps the namespace.
 */
function EdgeTypes({ namespace, client, onChange }: { namespace: Namespace; client: ApiClient; onChange(): void }) {
  const details = useEdgeTypes(client, namespace.name).data;
  const drop = useAction(
    async (name: string) => {
      await client.delete("/db/{namespace}/_edges/{edge_type}", { params: { path: { namespace: namespace.name, edge_type: name } } });
      onChange();
    },
    { success: (_, name) => `Dropped the relationship ${name}.`, failed: (name) => `Could not drop ${name}` },
  );

  const columns: GridColumn<Namespace["edges"][number]>[] = [
    {
      key: "name",
      label: "Relationship",
      headClassName: "pl-4",
      className: "pl-4 font-mono text-[13px] font-medium",
      render: (e) => (
        <a className="inline-flex items-center gap-1.5 hover:underline" href={dataHash({ namespace: namespace.name, edge: e.name })}>
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
        !namespace.managed && (
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
      <DataGrid columns={columns} rows={namespace.edges} rowKey={(e) => e.name} headerClassName="bg-muted/50" empty="No relationships" />
    </div>
  );
}

/** What the form makes: a table (a graph table, as the API makes them) or a relationship (an empty edge type). */
type Making = "table" | "edge";

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
};
const MAKING_OPTIONS = Object.entries(MAKING).map(([value, m]) => [value, m.label]) as [Making, string][];

/** What a new table starts with: no rows, or a sample's schema and rows (samples.ts). */
type Start = "empty" | SampleKey;
const START_OPTIONS = [["empty", "Empty"], ...Object.entries(SAMPLES).map(([key, s]) => [key, s.label])] as [Start, string][];

/**
 * One form for both: what to make, its name, and a line saying what it is; a table can start from a sample.
 * It opens what it made: an empty table at its schema, a sample's at its rows.
 */
function NewForm(props: { namespace: string; graphTables: number; client: ApiClient; onCreated(): void }) {
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
      const path = { namespace: props.namespace };
      if (making === "edge") {
        const made = await props.client.post("/db/{namespace}/_edges", { params: { path }, body: { name: name.trim() } });
        setName("");
        props.onCreated();
        location.hash = dataHash({ namespace: props.namespace, edge: made.name });
        return;
      }
      const made = await props.client.post("/db/{namespace}", { params: { path }, body: { name: name.trim(), fields: sample?.fields } });
      if (sample) {
        try {
          await props.client.post("/db/{namespace}/_transaction", { params: { path }, body: sampleTransaction(sample, made.name) });
        } catch (error) {
          // The rows and relationships go in together or not at all; then the table goes too.
          await props.client.delete("/db/{namespace}/{table}", { params: { path: { ...path, table: made.name } } }).catch(() => undefined);
          throw error;
        }
      }
      setName("");
      setStart("empty");
      props.onCreated();
      location.hash = dataHash({ namespace: props.namespace, table: made.name, view: sample ? undefined : "schema" });
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
