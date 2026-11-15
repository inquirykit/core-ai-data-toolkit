// The Data section's two query pages, both DuckDB SQL: SQL query
// (#/data/<ws>/_query) over every table, and Graph query (#/data/<ws>/_graph),
// whose result, when its rows are edges as the workspace's _edges table holds
// them, is drawn (GraphView.tsx) as well as listed: the one place a graph is
// drawn. Each keeps its draft and recent queries in the browser, offers
// examples written with the workspace's own names, and downloads its result as
// JSON or CSV. Values are drawn by DataRows.tsx's Cell.
import { lazy, Suspense, useEffect, useMemo, useRef, useState } from "react";
import { Download, LoaderCircle, Play, Spline, Table2, Waypoints, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";
import { useApi, AuthError } from "../../../shared/api";
import { download } from "../../../shared/browser";
import { CodeEditor, type CodeEditorHandle } from "../../../shared/code-editor";
import { message } from "../../../shared/errors";
import { Field, Notice, Segmented, SelectField } from "../../../shared/components";
import { fmtInt } from "../../../shared/format";
import { DataGrid, type GridColumn } from "../../../shared/grid";
import { readLocal, writeLocal } from "../../../shared/storage";
import { dataHash, type Workspace, type QueryRow } from "../data";
import {
  edgeTypeQuery,
  parseParams,
  queryColumns,
  querySnippets,
  tableEdgesQuery,
  tableQuery,
  toCsv,
  QUERY_LIMITS,
  type QueryPage,
  type QuerySnippet,
} from "./queries";
import { Cell } from "../DataRows";
import { graphFromRows } from "../graph/graph";

// sigma.js and graphology load only when a graph query's page opens.
const GraphView = lazy(() => import("../graph/GraphView"));

interface QueryDraft {
  text: string;
  params: string;
}

interface QueryMemory extends Partial<QueryDraft> {
  /** The last queries run, newest first */
  history?: (QueryDraft & { at: number })[];
}

const HISTORY = 12;
const queryKey = (kind: QueryPage, workspace: string) => `ai-toolkit.data.${kind}.${workspace}`;

/** What tells the two pages apart. */
const PAGES = {
  sql: {
    title: "SQL query",
    placeholder: "SELECT * FROM table WHERE id = $id",
  },
  graph: {
    title: "Graph query",
    placeholder: "SELECT * FROM _edges WHERE type = 'knows'",
  },
} as const;

type ResultView = "graph" | "table";
const RESULT_VIEWS: [ResultView, string][] = [
  ["graph", "Graph"],
  ["table", "Table"],
];

/**
 * A read-only query over the workspace, through POST /db/{ws}/_query: one
 * DuckDB SELECT, table names written as they appear here (the workspace's
 * relationships are rows of its _edges table), values as $name parameters.
 * ``startTable`` (a table page's link) starts with a query of that table, run
 * at once, and ``startEdge`` (an edge type page's) with one of its edges.
 */
export function QueryPanel({ workspace, kind, startTable, startEdge, startNode }: {
  workspace: Workspace;
  /** The SQL query, or the Graph query, which draws the edges its rows are */
  kind: QueryPage;
  startTable?: string;
  /** Graph query: the edge type it starts with a query of */
  startEdge?: string;
  /** Graph query: the row the drawing of the sample starts at (``table/id``) */
  startNode?: string;
}) {
  const client = useApi();
  const page = PAGES[kind];
  const stored = useMemo(() => readLocal<QueryMemory>(queryKey(kind, workspace.name)), [kind, workspace.name]);
  const [text, setText] = useState(stored.text ?? "");
  const [params, setParams] = useState(stored.params ?? "");
  const [limit, setLimit] = useState<(typeof QUERY_LIMITS)[number]>("100");
  const [history, setHistory] = useState(stored.history ?? []);
  const [rows, setRows] = useState<QueryRow[] | null>(null);
  const [elapsed, setElapsed] = useState(0);
  const [error, setError] = useState("");
  const [running, setRunning] = useState(false);
  const [resultView, setResultView] = useState<ResultView>("graph");
  const editor = useRef<CodeEditorHandle>(null);

  const graphTables = useMemo(() => workspace.tables.filter((t) => t.graph), [workspace]);
  const tables = workspace.tables;

  useEffect(() => {
    writeLocal(queryKey(kind, workspace.name), { text, params, history } satisfies QueryMemory);
  }, [kind, workspace.name, text, params, history]);

  const snippets = useMemo(() => querySnippets(kind, workspace.tables, workspace.edges), [kind, workspace]);
  // SQL completes the workspace's tables with their columns (declared fields, _rest, _seq), and _edges.
  const schema = useMemo(
    () =>
      Object.fromEntries([
        ...workspace.tables.map((t) => [t.name, ["id", ...t.fields.map((f) => f.name), "_rest", "_seq"]]),
        ["_edges", ["type", "from_table", "from_id", "to_table", "to_id", "props"]],
      ]),
    [workspace],
  );
  const columns = useMemo(() => queryColumns(rows ?? []), [rows]);
  // What the graph query's result draws: the edges its rows are.
  const drawn = useMemo(
    () => (kind === "graph" && rows ? graphFromRows(rows, graphTables.map((t) => t.name)) : null),
    [kind, rows, graphTables],
  );
  const drawable = Boolean(drawn?.nodes.length);
  // The first column stays put while the result scrolls sideways.
  const gridColumns: GridColumn<QueryRow>[] = columns.map((c, i) => ({
    key: c,
    label: c,
    headClassName: cn("h-9 px-3 font-mono text-xs font-medium", i === 0 && "sticky left-0 z-10 bg-muted"),
    className: cn("max-w-80 px-3 py-1.5", i === 0 && "sticky left-0 bg-card"),
    render: (row) => <Cell value={row[c]} />,
  }));

  const load = (draft: QueryDraft) => {
    setText(draft.text);
    setParams(draft.params);
    editor.current?.focus();
  };
  const useSnippet = (snippet: QuerySnippet) => load({ text: snippet.text, params: snippet.params ? JSON.stringify(snippet.params) : "" });
  const insert = (name: string) => editor.current?.insert(name);

  const run = async (draft: QueryDraft = { text, params }) => {
    if (!draft.text.trim() || running) return;
    const parsed = parseParams(draft.params);
    if ("error" in parsed) {
      setError(parsed.error);
      return;
    }
    setRunning(true);
    setError("");
    const started = performance.now();
    try {
      const result = await client.post("/db/{workspace}/_query", {
        params: { path: { workspace: workspace.name } },
        body: { query: draft.text, params: parsed.params, limit: Number(limit) },
      });
      setRows(result.rows);
      setElapsed(performance.now() - started);
      const entry = { ...draft, at: Date.now() };
      setHistory((h) => [entry, ...h.filter((e) => e.text !== draft.text || e.params !== draft.params)].slice(0, HISTORY));
    } catch (e) {
      if (!(e instanceof AuthError)) setError(message(e));
    } finally {
      setRunning(false);
    }
  };

  // A table's or edge type's page links here with it: its query is written and run.
  useEffect(() => {
    const text = startTable ? (kind === "graph" ? tableEdgesQuery(startTable) : tableQuery(startTable)) : startEdge ? edgeTypeQuery(startEdge) : "";
    if (!text) return;
    const start = { text, params: "" };
    load(start);
    void run(start);
  }, [startTable, startEdge]);

  const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-");
  return (
    <Card className="min-w-0 gap-4">
      <CardHeader>
        <CardTitle className="flex items-center gap-1.5 font-mono text-lg">
          <a className="text-muted-foreground hover:text-foreground" href={dataHash({ workspace: workspace.name })}>
            {workspace.name}
          </a>
          <span className="text-muted-foreground">/</span>
          <span className="font-sans">{page.title}</span>
        </CardTitle>
        <CardDescription>
          {kind === "graph" ? (
            <>
              Read-only SQL over the workspace's {workspace.edges.length} relationships, which are rows of <code className="font-mono">_edges</code>{" "}
              (type, from_table, from_id, to_table, to_id, props), and its tables. The relationships it returns are drawn.
            </>
          ) : (
            <>
              Read-only SQL (DuckDB's) over the workspace's {workspace.tables.length} tables: declared fields are columns, the others are in{" "}
              <code className="font-mono">_rest</code> (<code className="font-mono">_rest-&gt;&gt;'kind'</code>).
            </>
          )}{" "}
          Names are written as they appear here; values go in as parameters (<code className="font-mono">$name</code>).
        </CardDescription>
      </CardHeader>
      <CardContent className="grid gap-3">
        <CodeEditor
          ref={editor}
          aria-label={page.title}
          language="sql"
          schema={schema}
          lineNumbers
          minHeight="9rem"
          placeholder={page.placeholder}
          value={text}
          onChange={setText}
          onRun={() => void run()}
        />
        <div className="flex flex-wrap items-end gap-2">
          <Field label="Parameters" hint="A JSON object" className="min-w-56 flex-1">
            <Input
              placeholder='{"id": "ann"}'
              spellCheck={false}
              autoComplete="off"
              className="h-8 font-mono text-xs"
              value={params}
              onChange={(e) => setParams(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && void run()}
            />
          </Field>
          <SelectField
            label="Rows at most"
            size="sm"
            triggerClassName="w-24"
            aria-label="Rows at most"
            value={limit}
            onChange={(v) => setLimit(v as (typeof QUERY_LIMITS)[number])}
            options={QUERY_LIMITS.map((n) => ({ value: n, label: n }))}
          />
          {/* Two menus rather than fields: picking fills the editor, so neither keeps a value. */}
          <SelectField
            label="Examples"
            size="sm"
            triggerClassName="w-56"
            aria-label="Insert an example query"
            placeholder="Insert an example…"
            value=""
            onChange={(v) => useSnippet(snippets[Number(v)])}
            options={snippets.map((snippet, i) => ({ value: String(i), label: snippet.title }))}
          />
          {history.length > 0 && (
            <SelectField
              label="Recent"
              size="sm"
              triggerClassName="w-56"
              aria-label="A recent query"
              placeholder={`${history.length} recent…`}
              value=""
              onChange={(v) => load(history[Number(v)])}
              options={history.map((entry, i) => ({
                value: String(i),
                className: "font-mono text-xs",
                label: <span className="max-w-72 truncate">{entry.text.replace(/\s+/g, " ")}</span>,
              }))}
            />
          )}
          <Button type="button" size="sm" className="ml-auto" disabled={running || !text.trim()} onClick={() => void run()} title="Cmd/Ctrl + Enter">
            {running ? <LoaderCircle className="animate-spin" aria-hidden="true" /> : <Play aria-hidden="true" />}
            Run
          </Button>
        </div>

        {(tables.length > 0 || workspace.edges.length > 0) && (
          <div className="flex flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
            <span className="mr-1">Insert:</span>
            {tables.map((t) => (
              <button
                key={t.name}
                type="button"
                className="inline-flex h-6 items-center gap-1 rounded-md border bg-background px-2 font-mono text-foreground hover:bg-muted"
                title={t.graph ? "A table" : "A plain table: relationships cannot join it"}
                onClick={() => insert(t.name)}
              >
                {t.graph ? <Waypoints className="size-3 text-muted-foreground" aria-hidden="true" /> : <Table2 className="size-3 text-muted-foreground" aria-hidden="true" />}
                {t.name}
              </button>
            ))}
            {workspace.edges.map((e) => (
              <button
                key={e.name}
                type="button"
                className="inline-flex h-6 items-center gap-1 rounded-md border border-dashed bg-background px-2 font-mono text-foreground hover:bg-muted"
                title="A relationship: its type, as _edges holds it"
                onClick={() => insert(`'${e.name}'`)}
              >
                <Spline className="size-3 text-muted-foreground" aria-hidden="true" />
                {e.name}
              </button>
            ))}
          </div>
        )}

        {error && <Notice notice={{ text: error, kind: "error" }} />}

        {rows && (
          <>
            <div className="flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
              <span>
                {fmtInt(rows.length)} {rows.length === 1 ? "row" : "rows"}
                {rows.length >= Number(limit) && " (the limit)"} · {elapsed < 1000 ? `${Math.round(elapsed)} ms` : `${(elapsed / 1000).toFixed(1)} s`}
              </span>
              {drawable && <Segmented<ResultView> label="Show the result as" size="sm" options={RESULT_VIEWS} value={resultView} onChange={setResultView} />}
              <div className="ml-auto flex gap-2">
                <Button type="button" variant="outline" size="sm" disabled={!rows.length} onClick={() => download(`${workspace.name}-query-${stamp}.json`, JSON.stringify(rows, null, 2), "application/json")}>
                  <Download aria-hidden="true" />
                  JSON
                </Button>
                <Button type="button" variant="outline" size="sm" disabled={!rows.length} onClick={() => download(`${workspace.name}-query-${stamp}.csv`, toCsv(rows, columns), "text/csv")}>
                  <Download aria-hidden="true" />
                  CSV
                </Button>
                {kind === "graph" && (
                  <Button type="button" variant="ghost" size="sm" onClick={() => setRows(null)} title="Put the result away and draw a sample of the tables again">
                    <X aria-hidden="true" />
                    Clear
                  </Button>
                )}
              </div>
            </div>
            {drawable && resultView === "graph" ? (
              <Suspense fallback={<Skeleton className="h-[62vh] rounded-lg" />}>
                <GraphView workspace={workspace} data={drawn!} />
              </Suspense>
            ) : (
              <div className="max-h-[60vh] overflow-auto rounded-lg border">
                <DataGrid columns={gridColumns} rows={rows} rowKey={(_, i) => i} headerClassName="sticky top-0 z-10 bg-muted/90 backdrop-blur" empty="No rows" />
              </div>
            )}
          </>
        )}
        {!rows && kind === "graph" && graphTables.length > 0 && (
          <>
            <p className="text-xs text-muted-foreground">A sample of the tables, until a query runs: what it returns is drawn here instead.</p>
            <Suspense fallback={<Skeleton className="h-[62vh] rounded-lg" />}>
              <GraphView workspace={workspace} startNode={startNode} />
            </Suspense>
          </>
        )}
        {!rows && !error && kind === "sql" && (
          <p className="text-xs text-muted-foreground">
            Try an example, or click a name above to insert it. Queries that would change data are refused; writes go through the rows and edges routes.
          </p>
        )}
      </CardContent>
    </Card>
  );
}
