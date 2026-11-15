// A graph table's rows (TableBrowser.tsx) and a relationship's
// links, its edges (DataEdgeType.tsx), filtered by an SQL condition. The page and its count
// are SQL queries (edges.ts writes them) run through POST /db/{ws}/_query. A
// node opens in RowEditor; an edge's ends link to the Graph query, drawn there
// with their neighbors.
import { useEffect, useMemo, useState, type FormEvent, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { Plus } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useApi } from "../../../shared/api";
import { errorText } from "../../../shared/query";
import { Notice, Segmented } from "../../../shared/components";
import { columnsOf, nextSort, type FieldInfo, type Row, type Sort, type TableInfo, vectorField } from "../data";
import {
  edgeColumns,
  edgeOf,
  edgesFrom,
  edgesQuery,
  holdingSql,
  rowOf,
  rowsFrom,
  rowsQuery,
  type SqlPage,
} from "./edges";
import { useRefCells } from "../DataRefs";
import { RowEditor } from "../DataRowEditor";
import { EdgeEnd, RowsGrid } from "../DataRows";
import { PAGE_SIZES, Pager } from "../../layout";

/** A page of what an SQL query selects and how many it selects, keyed under ``key`` (the table's or edge type's loads). */
function useSqlPage(workspace: string, key: string[], queries: { rows: string; count: string }, limit: number) {
  const client = useApi();
  return useQuery({
    queryKey: [...key, "sql", queries],
    queryFn: ({ signal }) => {
      const run = (query: string, rows: number) =>
        client.post("/db/{workspace}/_query", { params: { path: { workspace } }, body: { query, limit: rows }, signal });
      return Promise.all([run(queries.rows, limit), run(queries.count, 1)]).then(([page, count]) => ({
        rows: page.rows,
        count: Number(count.rows[0]?.count ?? 0),
      }));
    },
  });
}

/**
 * Where the view is: the condition applied (``start`` at first), the sort and the page. A new condition, sort or size
 * starts at the first page.
 */
function usePaging(start = "") {
  const [where, setWhere] = useState(start);
  const [sort, setSort] = useState<Sort | null>(null);
  const [limit, setLimit] = useState(25);
  const [skip, setSkip] = useState(0);
  const page: SqlPage = useMemo(() => ({ where, sort, limit, skip }), [where, sort, limit, skip]);
  return {
    page,
    filter(condition: string) {
      setWhere(condition);
      setSkip(0);
    },
    sortBy(column: string) {
      setSort((s) => nextSort(s, column));
      setSkip(0);
    },
    resize(size: number) {
      setLimit(size);
      setSkip(0);
    },
    previous: () => setSkip((s) => Math.max(0, s - limit)),
    next: () => setSkip((s) => s + limit),
  };
}

type Paging = ReturnType<typeof usePaging>;

export function GraphNodes(props: {
  workspace: string;
  table: string;
  fields: FieldInfo[];
  /** The workspace's tables, which the table's refs name */
  tables: TableInfo[];
  readOnly: boolean;
  /** The row to open in its editor, as a link opens it */
  row?: string;
  /** The field and value the rows start filtered by, as a link to the rows naming a row filters them */
  filter?: [string, string];
  /** The editor closed */
  onRowClosed(): void;
  /** A node was saved or deleted */
  onChange(): void;
}) {
  const { workspace, table } = props;
  const paging = usePaging(props.filter ? holdingSql(props.fields, ...props.filter) : "");
  const { page } = paging;
  const vector = vectorField(props.fields);
  // A row's vector is hundreds of numbers: the page leaves it out, though the editor reads the row whole.
  const declaredNames = useMemo(() => props.fields.map((f) => f.name), [props.fields]);
  const queries = useMemo(() => rowsQuery(table, page, declaredNames, vector?.name), [table, page, declaredNames, vector?.name]);
  const result = useSqlPage(workspace, ["db", "rows", workspace, table], queries, page.limit);
  const rows = useMemo(() => result.data?.rows.map((r) => rowOf(r) as Row) ?? null, [result.data]);
  // Nor is it a column, should a page loaded before the field was known hold it.
  const columns = useMemo(() => columnsOf(rows ?? []).filter((c) => c !== vector?.name), [rows, vector?.name]);
  const declared = useMemo(() => new Map(props.fields.map((f) => [f.name, f])), [props.fields]);
  const refs = useRefCells(workspace, table, props.tables, rows);
  const [editing, setEditing] = useState<Row | "new" | null>(null);
  useEffect(() => {
    if (props.row) setEditing({ id: props.row });
  }, [props.row]);
  const close = () => {
    setEditing(null);
    props.onRowClosed();
  };

  return (
    <>
      <Toolbar paging={paging}>
        {!props.readOnly && (
          <Button type="button" size="sm" onClick={() => setEditing("new")}>
            <Plus aria-hidden="true" />
            New row
          </Button>
        )}
      </Toolbar>
      <SqlFilter
        start={rowsFrom(table)}
        applied={page.where}
        onApply={paging.filter}
        placeholder="age > 30 AND name LIKE 'A%'"
        hint={
          <>
            Declared fields are columns: <Code>name = 'Ann'</Code>, <Code>age &gt;= 30</Code>, <Code>list_contains(tags, 'vip')</Code>,{" "}
            <Code>email IS NULL</Code>; the others are in <Code>_rest</Code>: <Code>_rest-&gt;&gt;'kind' = 'lead'</Code>. Joined by AND, OR, NOT; text
            goes in single quotes.
          </>
        }
      />
      <QueryError error={result.error} />
      <RowsGrid
        columns={[...columns, ...refs.columns]}
        heads={refs.heads}
        rows={rows}
        rowKey={(row) => row.id}
        sort={page.sort}
        onSort={paging.sortBy}
        declared={declared}
        onRow={setEditing}
        cell={refs.cell}
        loading={result.isFetching}
        empty={page.where ? "No rows match this condition" : "No rows yet"}
      />
      <PagerOf paging={paging} shown={rows?.length ?? 0} result={result} />
      <RowEditor
        workspace={workspace}
        table={table}
        tables={props.tables}
        row={editing}
        graph
        readOnly={props.readOnly}
        vector={vector}
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

export function EdgeRows(props: { workspace: string; type: string }) {
  const { workspace, type } = props;
  const paging = usePaging();
  const { page } = paging;
  const queries = useMemo(() => edgesQuery(type, page), [type, page]);
  const result = useSqlPage(workspace, ["db", "edges", workspace, type], queries, page.limit);
  const rows = useMemo(() => result.data?.rows.map(edgeOf) ?? null, [result.data]);
  const columns = useMemo(() => edgeColumns(rows ?? []), [rows]);

  return (
    <>
      <Toolbar paging={paging} />
      <SqlFilter
        start={edgesFrom(type)}
        applied={page.where}
        onApply={paging.filter}
        placeholder="props->>'since' >= '2020' AND to_table = 'companies'"
        hint={
          <>
            A relationship goes from <Code>from_table</Code>/<Code>from_id</Code> to <Code>to_table</Code>/<Code>to_id</Code>; its properties are{" "}
            <Code>props</Code>: <Code>props-&gt;&gt;'role' = 'CTO'</Code>, <Code>from_id = 'ann'</Code>, <Code>to_table = 'companies'</Code>.
          </>
        }
      />
      <QueryError error={result.error} />
      <RowsGrid
        columns={columns}
        rows={rows}
        rowKey={(edge) => `${edge.from}|${edge.to}`}
        sort={page.sort}
        onSort={paging.sortBy}
        cell={(column, edge) =>
          (column === "from" || column === "to") && typeof edge[column] === "string" ? <EdgeEnd workspace={workspace} end={edge[column] as string} /> : undefined
        }
        loading={result.isFetching}
        empty={page.where ? "None match this condition" : "None yet"}
      />
      <PagerOf paging={paging} shown={rows?.length ?? 0} result={result} />
    </>
  );
}

/** What the page offers (a graph table's New row), then the page size on the right. */
function Toolbar({ paging, children }: { paging: Paging; children?: ReactNode }) {
  return (
    <div className="flex flex-wrap items-center gap-2">
      {children}
      <Segmented
        label="Rows per page"
        size="sm"
        className="ml-auto"
        options={PAGE_SIZES}
        value={String(paging.page.limit)}
        onChange={(v) => paging.resize(Number(v))}
      />
    </div>
  );
}

/** "SELECT * FROM "people" WHERE [ … ]": the statement so far, and the condition, applied on Enter or Filter. */
function SqlFilter(props: { start: string; applied: string; onApply(condition: string): void; placeholder: string; hint: ReactNode }) {
  const [draft, setDraft] = useState(props.applied);
  const submit = (event: FormEvent) => {
    event.preventDefault();
    props.onApply(draft.trim());
  };
  return (
    <div className="grid gap-1.5">
      <form className="flex flex-wrap items-center gap-2" onSubmit={submit}>
        <label className="flex h-8 min-w-0 flex-1 items-center overflow-hidden rounded-md border border-input bg-muted/40 font-mono text-xs shadow-xs transition-[color,box-shadow] focus-within:border-ring focus-within:ring-[3px] focus-within:ring-ring/50">
          <span className="shrink-0 truncate pl-3 text-muted-foreground" title={props.start}>
            {props.start.replace(/ WHERE$| AND$/, "")}
          </span>
          <span className="shrink-0 pl-2 font-semibold">{props.start.endsWith(" AND") ? "AND" : "WHERE"}</span>
          <input
            aria-label="SQL condition"
            className="h-full min-w-40 flex-1 bg-transparent px-2 outline-none placeholder:text-muted-foreground/70"
            placeholder={props.placeholder}
            spellCheck={false}
            autoComplete="off"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
          />
        </label>
        <Button type="submit" variant={draft.trim() !== props.applied ? "default" : "secondary"} size="sm">
          Filter
        </Button>
        {props.applied && (
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={() => {
              setDraft("");
              props.onApply("");
            }}
          >
            Clear
          </Button>
        )}
      </form>
      <p className="text-xs text-muted-foreground">{props.hint}</p>
    </div>
  );
}

function Code({ children }: { children: ReactNode }) {
  return <code className="font-mono text-[11px] whitespace-nowrap text-foreground">{children}</code>;
}

/** The query's error: a condition the database cannot read comes back with the reason it gives. */
function QueryError({ error }: { error: unknown }) {
  const text = errorText(error);
  return text ? <Notice notice={{ text, kind: "error" }} /> : null;
}

function PagerOf({ paging, shown, result }: { paging: Paging; shown: number; result: { data?: { count: number }; isFetching: boolean } }) {
  const { skip } = paging.page;
  const count = result.data?.count ?? null;
  return (
    <Pager
      start={skip}
      shown={shown}
      count={count}
      hasPrevious={skip > 0}
      hasNext={count != null && skip + shown < count}
      loading={result.isFetching}
      onPrevious={paging.previous}
      onNext={paging.next}
    />
  );
}
