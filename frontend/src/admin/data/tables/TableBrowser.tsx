// A table of the Data section: a page of its rows, filtered and sorted by the
// server, or its schema (DataSchema.tsx). A table with full-text fields has a
// search box, whose results are a page of the list (GET ?_search=) for a graph
// table too; one with a vector field has a search for its nearest rows
// (DataNearest.tsx), shown in place of its rows. A graph table's rows (every table's,
// unless made plain) filter by an SQL condition (DataGraphRows.tsx); the
// relationships joining them are their own pages (DataEdgeType.tsx) and its
// drawing the Graph query's. A row opens in
// RowEditor (DataRowEditor.tsx); the grid is DataRows.tsx's. A bucket shows its
// files (DataBucket.tsx) instead, with no row editor, schema or search.
import { useEffect, useMemo, useState, type FormEvent } from "react";
import { useIsFetching, useQuery, useQueryClient } from "@tanstack/react-query";
import { ListFilter, LoaderCircle, Network, Plus, RotateCw, Search, SquareTerminal, X } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button, buttonVariants } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { useAction } from "../../../shared/action";
import { useApi, queryString } from "../../../shared/api";
import { errorText } from "../../../shared/query";
import { ConfirmButton, Notice, Segmented } from "../../../shared/components";
import { fmtInt, plural } from "../../../shared/format";
import {
  columnsOf,
  countQuery,
  dataHash,
  keeperOf,
  listQuery,
  nextSort,
  type FieldInfo,
  type ListQuery,
  type Workspace,
  type Row,
  type Sort,
  type TableInfo,
  type TableView,
  vectorField,
} from "../data";
import { type NearestQuery } from "./vectors";
import { BucketFiles } from "../buckets/DataBucket";
import { GraphNodes } from "../graph/DataGraphRows";
import { NearestRows, NearestSearch } from "./DataNearest";
import { PinToggle, type Pins } from "../DataPins";
import { useRefCells } from "../DataRefs";
import { RowEditor } from "../DataRowEditor";
import { RowsGrid } from "../DataRows";
import { BucketBadge, KeptBadge, NoRelationships } from "../DataWorkspace";
import { SchemaEditor } from "./DataSchema";
import { PAGE_SIZES, Pager, useCursorPages } from "../../layout";

export function TableBrowser(props: {
  /** The workspace the table is in, as listed */
  workspace: Workspace;
  table: string;
  view: TableView;
  /** The row to open in its editor (``?_row=``), as a link to it opens it */
  row?: string;
  /** The field and value the rows start filtered by (``?customer=c1``), as a link to the rows naming a row filters them */
  filter?: [string, string];
  pins: Pins;
  onChange(): void;
}) {
  const client = useApi();
  const { table, view } = props;
  const workspace = props.workspace.name;
  // None until a row is written to it.
  const info = props.workspace.tables.find((t) => t.name === table);
  const graph = info?.graph ?? false;
  // A table of files, which its file routes alone write: its page lists them.
  const bucket = info?.bucket ?? false;
  const fields = info?.fields ?? [];
  // What keeps the table (its workspace, or the table alone), when the service does: then nothing here can be changed.
  const keeper = keeperOf(props.workspace, info);
  const readOnly = Boolean(keeper);
  const queries = useQueryClient();
  // Every load of the table is keyed under this: its rows and a row's edges.
  const tableKey = ["db", "rows", workspace, table];
  const loading = useIsFetching({ queryKey: tableKey }) > 0;

  // A write can change any of them, and the workspace's counts.
  const refresh = () => {
    void queries.invalidateQueries({ queryKey: tableKey });
    props.onChange();
  };

  const drop = useAction(
    async () => {
      await client.delete("/db/{workspace}/{table}", { params: { path: { workspace, table } } });
      location.hash = dataHash({ workspace });
      props.onChange();
    },
    { success: `Dropped ${workspace}/${table}.`, failed: `Could not drop ${table}` },
  );

  const tabs: [TableView, string][] = [
    ["rows", "Rows"],
    ["schema", fields.length ? `Schema · ${fields.length}` : "Schema"],
  ];
  const wordy = fields.filter((f) => f.fulltext).map((f) => f.name);
  const [search, setSearch] = useState("");
  // A search only stands while the table has a field to search.
  const searching = wordy.length > 0 && search.trim() !== "";
  const vector = vectorField(fields);
  // The nearest rows asked for, shown in place of the rows while the table has its vector field.
  const [asked, setAsked] = useState<NearestQuery | null>(null);
  const nearest = vector ? asked : null;
  const tables = props.workspace.tables;
  // What the grid takes from the location: the row it opens (the location forgets it once the row is closed), and the filter.
  const located = {
    row: props.row,
    filter: props.filter,
    onRowClosed: () => {
      if (props.row) location.hash = dataHash({ workspace, table, filter: props.filter });
    },
  };

  return (
    <Card className="min-w-0 gap-4">
      <CardHeader>
        <CardTitle className="flex min-w-0 items-center gap-1.5 font-mono text-lg">
          <a className="min-w-0 truncate text-muted-foreground hover:text-foreground" href={dataHash({ workspace })} title={workspace}>
            {workspace}
          </a>
          <span className="text-muted-foreground">/</span>
          {table}
        </CardTitle>
        <CardDescription className="flex items-center gap-2">
          {bucket ? plural(info?.count ?? 0, "file") : `${fmtInt(info?.count ?? 0)} rows`}
          {loading && <LoaderCircle className="size-3.5 animate-spin" aria-label="Loading" />}
          {!info && <Badge variant="outline">no rows written yet</Badge>}
          {info?.kept && <KeptBadge keeper={info.kept} />}
          {bucket && <BucketBadge />}
          {info && !graph && !keeper && !bucket && <NoRelationships />}
        </CardDescription>
        <CardAction className="flex flex-wrap justify-end gap-2">
          {/* Pinned, it is listed first in the sidebar; a table nothing is written to yet is not listed. */}
          {info && <PinToggle pins={props.pins} workspace={workspace} table={table} text />}
          {graph ? (
            <a
              className={buttonVariants({ variant: "outline", size: "sm" })}
              href={dataHash({ workspace, graph: true, graphTable: table })}
              title="Open the Graph query with this table's rows and their relationships"
            >
              <Network className="text-query-graph" aria-hidden="true" />
              Graph query
            </a>
          ) : (
            <a
              className={buttonVariants({ variant: "outline", size: "sm" })}
              href={dataHash({ workspace, query: true, queryTable: table })}
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
              title={`Drop ${workspace}/${table}?`}
              body={
                bucket
                  ? "Every file in it is deleted, from the disk too. This cannot be undone."
                  : `Every row in it is deleted${graph ? ", with its relationships" : ""}. A client writing to it later makes it anew, empty${graph ? "" : ", as a table relationships can join"}.`
              }
              busy={drop.busy}
              onConfirm={drop.run}
            />
          )}
        </CardAction>
      </CardHeader>
      <CardContent className="grid gap-3">
        {keeper && <Notice notice={{ text: `Read-only here: kept by the service (${keeper}).` }} />}
        {!bucket && (
          <Segmented<TableView> label="View" size="sm" options={tabs} value={view} onChange={(v) => (location.hash = dataHash({ workspace, table, view: v }))} />
        )}
        {!bucket && view === "rows" && vector && <NearestSearch field={vector} words={wordy} active={nearest !== null} onSearch={setAsked} />}
        {!bucket && view === "rows" && wordy.length > 0 && !nearest && <SearchForm fields={wordy} search={search} onSearch={setSearch} />}
        {bucket ? (
          <BucketFiles workspace={workspace} bucket={table} readOnly={readOnly} onChange={refresh} />
        ) : view === "schema" ? (
          <SchemaEditor workspace={workspace} table={table} readOnly={readOnly} onSaved={refresh} />
        ) : nearest && vector ? (
          <NearestRows
            workspace={workspace}
            table={table}
            field={vector}
            query={nearest}
            fields={fields}
            tables={tables}
            graph={graph}
            readOnly={readOnly}
            onChange={refresh}
          />
        ) : searching ? (
          <TableRows workspace={workspace} table={table} fields={fields} tables={tables} search={search} readOnly={readOnly} onChange={refresh} {...located} />
        ) : graph ? (
          <GraphNodes workspace={workspace} table={table} fields={fields} tables={tables} readOnly={readOnly} onChange={refresh} {...located} />
        ) : (
          <TableRows workspace={workspace} table={table} fields={fields} tables={tables} readOnly={readOnly} onChange={refresh} {...located} />
        )}
      </CardContent>
    </Card>
  );
}

/**
 * A plain table's rows, or any table's that a search finds (the best match first unless sorted), filtered by
 * field = value and sorted by the server (GET /db/{ws}/{table}).
 */
function TableRows(props: {
  workspace: string;
  table: string;
  fields: FieldInfo[];
  /** The workspace's tables, which the table's refs name */
  tables: TableInfo[];
  search?: string;
  readOnly: boolean;
  /** The row to open in its editor, as a link opens it */
  row?: string;
  /** The field and value the rows start filtered by: an id, so it is quoted as text */
  filter?: [string, string];
  /** The editor closed */
  onRowClosed(): void;
  onChange(): void;
}) {
  const client = useApi();
  const { workspace, table, search } = props;
  const [filters, setFilters] = useState<[string, string][]>(() => (props.filter ? [[props.filter[0], JSON.stringify(props.filter[1])]] : []));
  const [sort, setSort] = useState<Sort | null>(null);
  const [limit, setLimit] = useState(25);
  // A new search starts at the first page.
  const pages = useCursorPages(search);
  const [editing, setEditing] = useState<Row | "new" | null>(null);
  useEffect(() => {
    if (props.row) setEditing({ id: props.row });
  }, [props.row]);
  const close = () => {
    setEditing(null);
    props.onRowClosed();
  };

  const query: ListQuery = useMemo(
    () => ({ filters, search, sort, limit, cursor: pages.cursor }),
    [filters, search, sort, limit, pages.cursor],
  );
  const rows = useQuery({
    queryKey: ["db", "rows", workspace, table, "page", query],
    queryFn: ({ signal }) => {
      const path = { workspace, table };
      return Promise.all([
        client.get("/db/{workspace}/{table}", { params: { path, query: listQuery(query) }, signal }),
        // The count takes the list's field=value filters, which its schema cannot name: they go in as the query string.
        client.get("/db/{workspace}/{table}/_count", { params: { path }, querySerializer: () => queryString(countQuery(query)), signal }),
      ]);
    },
  });
  const { data } = rows;
  const error = errorText(rows.error);
  const loading = rows.isFetching;
  const [page, count] = data ? [data[0], data[1].count] : [null, null];

  const firstPage = pages.first;
  const columns = useMemo(() => columnsOf(page?.rows ?? []), [page]);
  const declared = useMemo(() => new Map(props.fields.map((f) => [f.name, f])), [props.fields]);
  const refs = useRefCells(workspace, table, props.tables, page?.rows ?? null);

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
        columns={[...columns, ...refs.columns]}
        heads={refs.heads}
        rows={page?.rows ?? null}
        rowKey={(row) => row.id}
        sort={sort}
        onSort={(c) => {
          setSort((s) => nextSort(s, c));
          firstPage();
        }}
        declared={declared}
        onRow={setEditing}
        cell={refs.cell}
        loading={loading}
        empty={search ? "No rows hold these words" : filters.length ? "No rows match these filters" : "No rows yet"}
      />
      <Pager
        start={pages.index * limit}
        shown={page?.rows.length ?? 0}
        count={count}
        hasPrevious={pages.hasPrevious}
        hasNext={Boolean(page?.next)}
        loading={loading}
        onPrevious={pages.previous}
        onNext={() => pages.next(page?.next ?? undefined)}
      />

      <RowEditor
        workspace={workspace}
        table={table}
        tables={props.tables}
        row={editing}
        readOnly={props.readOnly}
        vector={vectorField(props.fields)}
        columns={[...new Set([...columns, ...props.fields.map((f) => f.name)])]}
        onClose={close}
        onSaved={() => {
          close();
          props.onChange();
        }}
      />
    </>
  );
}

/** The search box of a table with full-text fields: the search applies when submitted, and clears with X. */
function SearchForm(props: { fields: string[]; search: string; onSearch(search: string): void }) {
  const [text, setText] = useState(props.search);
  const submit = (event: FormEvent) => {
    event.preventDefault();
    props.onSearch(text.trim());
  };
  return (
    <form role="search" className="flex flex-wrap items-center gap-2" onSubmit={submit}>
      <div className="relative min-w-56 flex-1">
        <Search className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-muted-foreground" aria-hidden="true" />
        <Input
          type="search"
          aria-label="Search"
          placeholder={`Search ${props.fields.join(", ")}`}
          title='Every word must be there; "a phrase" as written, -word must not, word* starts so. A domain such as site.example.com is one word.'
          maxLength={500}
          autoComplete="off"
          className="h-8 pl-8"
          value={text}
          onChange={(e) => {
            setText(e.target.value);
            if (!e.target.value) props.onSearch("");
          }}
        />
      </div>
      <Button type="submit" variant="secondary" size="sm">
        Search
      </Button>
      {props.search && (
        <Button
          type="button"
          variant="ghost"
          size="sm"
          onClick={() => {
            setText("");
            props.onSearch("");
          }}
        >
          <X aria-hidden="true" />
          Clear
        </Button>
      )}
    </form>
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
