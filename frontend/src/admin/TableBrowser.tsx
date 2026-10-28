// A table of the Data section: a page of its rows, filtered and sorted by the
// server, or its schema (DataSchema.tsx). A graph table's rows (every table's,
// unless made plain) filter by a Cypher condition (DataGraphRows.tsx); the
// relationships joining them are their own pages (DataEdgeType.tsx) and its
// drawing the Graph query's. A row opens in
// RowEditor (DataRowEditor.tsx); the grid is DataRows.tsx's.
import { useMemo, useState, type FormEvent } from "react";
import { useIsFetching, useQuery, useQueryClient } from "@tanstack/react-query";
import { ListFilter, LoaderCircle, Network, Plus, RotateCw, SquareTerminal, X } from "lucide-react";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Button, buttonVariants } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { queryString, type ApiClient } from "../shared/api";
import { failed } from "../shared/errors";
import { errorText } from "../shared/query";
import { ConfirmButton, Notice, Segmented } from "../shared/components";
import { fmtInt } from "../shared/format";
import {
  columnsOf,
  dataHash,
  filterQuery,
  listQuery,
  nextSort,
  type FieldInfo,
  type ListQuery,
  type Namespace,
  type Row,
  type Sort,
  type TableView,
} from "./data";
import { GraphNodes } from "./DataGraphRows";
import { PinToggle, type Pins } from "./DataPins";
import { RowEditor } from "./DataRowEditor";
import { PAGE_SIZES, Pager, RowsGrid } from "./DataRows";
import { NoRelationships } from "./DataNamespace";
import { SchemaEditor } from "./DataSchema";

export function TableBrowser(props: {
  client: ApiClient;
  /** The namespace the table is in, as listed */
  namespace: Namespace;
  table: string;
  view: TableView;
  pins: Pins;
  onChange(): void;
}) {
  const { client, table, view } = props;
  const namespace = props.namespace.name;
  // None until a row is written to it.
  const info = props.namespace.tables.find((t) => t.name === table);
  const graph = info?.graph ?? false;
  const fields = info?.fields ?? [];
  // What keeps the namespace, when the service does: then nothing here can be changed.
  const managed = props.namespace.managed ?? null;
  const readOnly = Boolean(managed);
  const queries = useQueryClient();
  // Every load of the table is keyed under this: its rows and a row's edges.
  const tableKey = ["db", "rows", namespace, table];
  const loading = useIsFetching({ queryKey: tableKey }) > 0;

  // A write can change any of them, and the namespace's counts.
  const refresh = () => {
    void queries.invalidateQueries({ queryKey: tableKey });
    props.onChange();
  };

  const drop = () =>
    client.delete("/db/{namespace}/{table}", { params: { path: { namespace, table } } }).then(() => {
      toast.success(`Dropped ${namespace}/${table}.`);
      location.hash = dataHash({ namespace });
      props.onChange();
    }, failed(`Could not drop ${table}`));

  const tabs: [TableView, string][] = [
    ["rows", "Rows"],
    ["schema", fields.length ? `Schema · ${fields.length}` : "Schema"],
  ];

  return (
    <Card className="min-w-0 gap-4">
      <CardHeader>
        <CardTitle className="flex items-center gap-1.5 font-mono text-lg">
          <a className="text-muted-foreground hover:text-foreground" href={dataHash({ namespace })}>
            {namespace}
          </a>
          <span className="text-muted-foreground">/</span>
          {table}
        </CardTitle>
        <CardDescription className="flex items-center gap-2">
          {fmtInt(info?.count ?? 0)} rows
          {loading && <LoaderCircle className="size-3.5 animate-spin" aria-label="Loading" />}
          {!info && <Badge variant="outline">no rows written yet</Badge>}
          {info && !graph && !managed && <NoRelationships />}
        </CardDescription>
        <CardAction className="flex flex-wrap justify-end gap-2">
          {/* Pinned, it is listed first in the sidebar; a table nothing is written to yet is not listed. */}
          {info && <PinToggle pins={props.pins} namespace={namespace} table={table} text />}
          {graph ? (
            <a
              className={buttonVariants({ variant: "outline", size: "sm" })}
              href={dataHash({ namespace, graph: true, graphTable: table })}
              title="Open the Graph query with this table's rows and their relationships"
            >
              <Network className="text-query-graph" aria-hidden="true" />
              Graph query
            </a>
          ) : (
            <a
              className={buttonVariants({ variant: "outline", size: "sm" })}
              href={dataHash({ namespace, query: true, queryTable: table })}
              title="Open the SQL query with this table's rows"
            >
              <SquareTerminal className="text-query-sql" aria-hidden="true" />
              SQL query
            </a>
          )}
          <Button type="button" variant="outline" size="sm" onClick={refresh}>
            <RotateCw aria-hidden="true" />
            Refresh
          </Button>
          {info && !readOnly && (
            <ConfirmButton
              label="Drop table"
              title={`Drop ${namespace}/${table}?`}
              body={`Every row in it is deleted${graph ? ", with its relationships" : ""}. A client writing to it later makes it anew, empty${graph ? "" : ", as a table relationships can join"}.`}
              onConfirm={drop}
            />
          )}
        </CardAction>
      </CardHeader>
      <CardContent className="grid gap-3">
        {managed && <Notice notice={{ text: `Read-only here: kept by the service (${managed}).` }} />}
        <Segmented<TableView> label="View" size="sm" options={tabs} value={view} onChange={(v) => (location.hash = dataHash({ namespace, table, view: v }))} />
        {view === "schema" ? (
          <SchemaEditor client={client} namespace={namespace} table={table} readOnly={readOnly} onSaved={refresh} />
        ) : graph ? (
          <GraphNodes client={client} namespace={namespace} table={table} fields={fields} readOnly={readOnly} onChange={refresh} />
        ) : (
          <TableRows client={client} namespace={namespace} table={table} fields={fields} readOnly={readOnly} onChange={refresh} />
        )}
      </CardContent>
    </Card>
  );
}

/** A plain table's rows, filtered by field = value and sorted by the server (GET /db/{ns}/{table}). */
function TableRows(props: { client: ApiClient; namespace: string; table: string; fields: FieldInfo[]; readOnly: boolean; onChange(): void }) {
  const { client, namespace, table } = props;
  const [filters, setFilters] = useState<[string, string][]>([]);
  const [sort, setSort] = useState<Sort | null>(null);
  const [limit, setLimit] = useState(25);
  // The cursors of the pages before this one, and this one's (undefined: the first page).
  const [cursors, setCursors] = useState<(string | undefined)[]>([undefined]);
  const [editing, setEditing] = useState<Row | "new" | null>(null);

  const query: ListQuery = useMemo(
    () => ({ filters, sort, limit, cursor: cursors[cursors.length - 1] }),
    [filters, sort, limit, cursors],
  );
  const rows = useQuery({
    queryKey: ["db", "rows", namespace, table, "page", query],
    queryFn: ({ signal }) => {
      const path = { namespace, table };
      return Promise.all([
        client.get("/db/{namespace}/{table}", { params: { path, query: listQuery(query) }, signal }),
        // The count takes the list's field=value filters, which its schema cannot name: they go in as the query string.
        client.get("/db/{namespace}/{table}/_count", { params: { path }, querySerializer: () => queryString(filterQuery(query.filters)), signal }),
      ]);
    },
  });
  const { data } = rows;
  const error = errorText(rows.error);
  const loading = rows.isFetching;
  const [page, count] = data ? [data[0], data[1].count] : [null, null];

  const firstPage = () => setCursors([undefined]);
  const columns = useMemo(() => columnsOf(page?.rows ?? []), [page]);
  const declared = useMemo(() => new Map(props.fields.map((f) => [f.name, f])), [props.fields]);

  return (
    <>
      <div className="flex flex-wrap items-center gap-2">
        <FilterForm
          columns={columns}
          onAdd={(field, value) => {
            setFilters((f) => [...f, [field, value]]);
            firstPage();
          }}
        />
        <Segmented
          label="Rows per page"
          size="sm"
          className="ml-auto"
          options={PAGE_SIZES}
          value={String(limit)}
          onChange={(v) => {
            setLimit(Number(v));
            firstPage();
          }}
        />
        {!props.readOnly && (
          <Button type="button" size="sm" onClick={() => setEditing("new")}>
            <Plus aria-hidden="true" />
            New row
          </Button>
        )}
      </div>
      {filters.length > 0 && (
        <div className="flex flex-wrap items-center gap-1.5">
          {filters.map(([field, value], i) => (
            <Badge key={i} variant="secondary" className="gap-1 pr-1 font-mono">
              {field} = {value}
              <button
                type="button"
                aria-label={`Remove filter ${field}`}
                className="grid size-4 place-items-center rounded-sm hover:bg-background"
                onClick={() => {
                  setFilters((f) => f.filter((_, j) => j !== i));
                  firstPage();
                }}
              >
                <X className="size-3" />
              </button>
            </Badge>
          ))}
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="h-6 px-2 text-xs"
            onClick={() => {
              setFilters([]);
              firstPage();
            }}
          >
            Clear
          </Button>
        </div>
      )}
      {error && <Notice notice={{ text: error, kind: "error" }} />}

      <RowsGrid
        columns={columns}
        rows={page?.rows ?? null}
        rowKey={(row) => row.id}
        sort={sort}
        onSort={(c) => {
          setSort((s) => nextSort(s, c));
          firstPage();
        }}
        declared={declared}
        onRow={setEditing}
        loading={loading}
        empty={filters.length ? "No rows match these filters" : "No rows yet"}
      />
      <Pager
        start={(cursors.length - 1) * limit}
        shown={page?.rows.length ?? 0}
        count={count}
        hasPrevious={cursors.length > 1}
        hasNext={Boolean(page?.next)}
        loading={loading}
        onPrevious={() => setCursors((c) => c.slice(0, -1))}
        onNext={() => setCursors((c) => [...c, page?.next ?? undefined])}
      />

      <RowEditor
        client={client}
        namespace={namespace}
        table={table}
        row={editing}
        readOnly={props.readOnly}
        columns={[...new Set([...columns, ...props.fields.map((f) => f.name)])]}
        onClose={() => setEditing(null)}
        onSaved={() => {
          setEditing(null);
          props.onChange();
        }}
      />
    </>
  );
}

function FilterForm({ columns, onAdd }: { columns: string[]; onAdd(field: string, value: string): void }) {
  const [field, setField] = useState("");
  const [value, setValue] = useState("");
  const listId = "data-columns";
  const submit = (event: FormEvent) => {
    event.preventDefault();
    onAdd(field.trim(), value);
    setField("");
    setValue("");
  };
  return (
    <form className="flex flex-wrap items-center gap-2" onSubmit={submit}>
      <ListFilter className="size-4 text-muted-foreground" aria-hidden="true" />
      <Input
        aria-label="Field"
        placeholder="field"
        list={listId}
        required
        pattern="[A-Za-z_][A-Za-z0-9_]*"
        autoComplete="off"
        className="h-8 w-36 font-mono text-xs"
        value={field}
        onChange={(e) => setField(e.target.value)}
      />
      <span className="text-sm text-muted-foreground">=</span>
      <Input
        aria-label="Value"
        placeholder='value, 30, true, null, "30"'
        title='Numbers, true, false and null match as such; quote to match text: "30". A list field matches when it holds the value.'
        autoComplete="off"
        className="h-8 w-52 font-mono text-xs"
        value={value}
        onChange={(e) => setValue(e.target.value)}
      />
      <Button type="submit" variant="secondary" size="sm">
        Filter
      </Button>
      <datalist id={listId}>
        {columns.map((c) => (
          <option key={c} value={c} />
        ))}
      </datalist>
    </form>
  );
}
