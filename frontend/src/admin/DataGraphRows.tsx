// A graph table's rows (TableBrowser.tsx) and a relationship's
// links, its edges (DataEdgeType.tsx), filtered by a Cypher condition. The page and its count
// are Cypher queries (data.ts writes them) run through POST /db/{ns}/_query. A
// node opens in RowEditor; an edge's ends link to the Graph query, drawn there
// with their neighbors.
import { useMemo, useState, type FormEvent, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { Plus } from "lucide-react";
import { Button } from "@/components/ui/button";
import type { ApiClient } from "../shared/api";
import { errorText } from "../shared/query";
import { Notice, Segmented } from "../shared/components";
import {
  columnsOf,
  edgeColumns,
  edgesCypher,
  edgesMatch,
  nextSort,
  nodesCypher,
  nodesMatch,
  type CypherPage,
  type FieldInfo,
  type QueryRow,
  type Row,
  type Sort,
} from "./data";
import { RowEditor } from "./DataRowEditor";
import { EdgeEnd, PAGE_SIZES, Pager, RowsGrid } from "./DataRows";

/** A page of what a Cypher query matches and how many it matches, keyed under ``key`` (the table's or edge type's loads). */
function useCypherPage(client: ApiClient, namespace: string, key: string[], queries: { rows: string; count: string }, limit: number) {
  return useQuery({
    queryKey: [...key, "cypher", queries],
    queryFn: ({ signal }) => {
      const run = (query: string, rows: number) =>
        client.post("/db/{namespace}/_query", { params: { path: { namespace } }, body: { language: "cypher", query, limit: rows }, signal });
      return Promise.all([run(queries.rows, limit), run(queries.count, 1)]).then(([page, count]) => ({
        rows: page.rows,
        count: Number(count.rows[0]?.count ?? 0),
      }));
    },
  });
}

/** Where the view is: the condition applied, the sort and the page. A new condition, sort or size starts at the first page. */
function usePaging() {
  const [where, setWhere] = useState("");
  const [sort, setSort] = useState<Sort | null>(null);
  const [limit, setLimit] = useState(25);
  const [skip, setSkip] = useState(0);
  const page: CypherPage = useMemo(() => ({ where, sort, limit, skip }), [where, sort, limit, skip]);
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
  client: ApiClient;
  namespace: string;
  table: string;
  fields: FieldInfo[];
  readOnly: boolean;
  /** A node was saved or deleted */
  onChange(): void;
}) {
  const { client, namespace, table } = props;
  const paging = usePaging();
  const { page } = paging;
  const queries = useMemo(() => nodesCypher(table, page), [table, page]);
  const result = useCypherPage(client, namespace, ["db", "rows", namespace, table], queries, page.limit);
  const rows = useMemo(() => result.data?.rows.map((r) => r.row as Row) ?? null, [result.data]);
  const columns = useMemo(() => columnsOf(rows ?? []), [rows]);
  const declared = useMemo(() => new Map(props.fields.map((f) => [f.name, f])), [props.fields]);
  const [editing, setEditing] = useState<Row | "new" | null>(null);

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
      <CypherFilter
        match={nodesMatch(table)}
        applied={page.where}
        onApply={paging.filter}
        placeholder="n.age > 30 AND n.name STARTS WITH 'A'"
        hint={
          <>
            <Code>n</Code> is the row: <Code>n.name = 'Ann'</Code>, <Code>n.age &gt;= 30</Code>, <Code>'vip' IN n.tags</Code>,{" "}
            <Code>n.email IS NULL</Code>, <Code>(n)--()</Code> for rows with relationships, joined by AND, OR, NOT. Text goes in quotes.
          </>
        }
      />
      <QueryError error={result.error} />
      <RowsGrid
        columns={columns}
        rows={rows}
        rowKey={(row) => row.id}
        sort={page.sort}
        onSort={paging.sortBy}
        declared={declared}
        onRow={setEditing}
        loading={result.isFetching}
        empty={page.where ? "No rows match this condition" : "No rows yet"}
      />
      <PagerOf paging={paging} shown={rows?.length ?? 0} result={result} />
      <RowEditor
        client={client}
        namespace={namespace}
        table={table}
        row={editing}
        graph
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

export function EdgeRows(props: { client: ApiClient; namespace: string; type: string }) {
  const { client, namespace, type } = props;
  const paging = usePaging();
  const { page } = paging;
  const queries = useMemo(() => edgesCypher(type, page), [type, page]);
  const result = useCypherPage(client, namespace, ["db", "edges", namespace, type], queries, page.limit);
  const rows = useMemo(() => result.data?.rows.map((r) => r.e as QueryRow) ?? null, [result.data]);
  const columns = useMemo(() => edgeColumns(rows ?? []), [rows]);

  return (
    <>
      <Toolbar paging={paging} />
      <CypherFilter
        match={edgesMatch(type)}
        applied={page.where}
        onApply={paging.filter}
        placeholder="e.since >= 2020 AND b.name = 'Acme'"
        hint={
          <>
            <Code>e</Code> is the relationship, <Code>a</Code> the row it goes from and <Code>b</Code> the row it goes to: <Code>e.weight &gt; 0.5</Code>,{" "}
            <Code>a.name STARTS WITH 'A'</Code>, <Code>b:companies</Code>, <Code>a.id = 'ann'</Code>.
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
          (column === "from" || column === "to") && typeof edge[column] === "string" ? <EdgeEnd namespace={namespace} end={edge[column] as string} /> : undefined
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

/** "MATCH (n:people) WHERE [ … ]": the statement so far, and the condition, applied on Enter or Filter. */
function CypherFilter(props: { match: string; applied: string; onApply(condition: string): void; placeholder: string; hint: ReactNode }) {
  const [draft, setDraft] = useState(props.applied);
  const submit = (event: FormEvent) => {
    event.preventDefault();
    props.onApply(draft.trim());
  };
  return (
    <div className="grid gap-1.5">
      <form className="flex flex-wrap items-center gap-2" onSubmit={submit}>
        <label className="flex h-8 min-w-0 flex-1 items-center overflow-hidden rounded-md border border-input bg-muted/40 font-mono text-xs shadow-xs transition-[color,box-shadow] focus-within:border-ring focus-within:ring-[3px] focus-within:ring-ring/50">
          <span className="shrink-0 truncate pl-3 text-muted-foreground" title={props.match}>
            {props.match}
          </span>
          <span className="shrink-0 pl-2 font-semibold">WHERE</span>
          <input
            aria-label="Cypher condition"
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

/** The query's error: a condition Cypher cannot read comes back as the reason ArcadeDB gives. */
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
