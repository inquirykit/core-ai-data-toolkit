// The nearest rows of a table with a vector field (TableBrowser.tsx): a text
// its field's model embeds, or a vector as JSON when rows carry their own, how
// many rows, and words for its full-text fields, sent to POST .../_search;
// with full-text fields, hybrid ranks by the text's words too, and a rerank
// model may read the rows found again. The rows found are drawn in the shared
// DataGrid with their distance (and the score of a hybrid or reranked
// search), best first, and open in RowEditor (DataRowEditor.tsx).
import { useMemo, useState, type FormEvent } from "react";
import { useQuery } from "@tanstack/react-query";
import { Radar, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { useApi, type Schemas } from "../../../shared/api";
import { Notice, SelectInput } from "../../../shared/components";
import { DataGrid, type GridColumn } from "../../../shared/grid";
import { errorText } from "../../../shared/query";
import { fmtInt } from "../../../shared/format";
import { columnsOf, describeField, type FieldInfo, type Row, type TableInfo } from "../data";
import { distanceMeaning, fmtDistance, nearestQuery, type NearestForm, type NearestQuery } from "./vectors";
import { RowEditor } from "../DataRowEditor";
import { Cell } from "../DataRows";

const K_CHOICES = ["5", "10", "25", "50", "100"];

/** The search box: what it asks applies when submitted (``onSearch``), and clears with X (``onSearch(null)``). */
export function NearestSearch(props: { field: FieldInfo; words: string[]; active: boolean; onSearch(query: NearestQuery | null): void }) {
  const client = useApi();
  const { field } = props;
  const [form, setForm] = useState<NearestForm>({ text: "", k: 10, words: "" });
  // The rerank models the service lists, offered for the reranker (any other ID can be typed).
  const rerankers = client.useQuery("get", "/models/catalog", undefined, {
    select: (d) => d.tasks.find((t) => t.task === "rerank")?.models.map((m) => m.id) ?? [],
    enabled: Boolean(field.model),
  });
  const [error, setError] = useState("");
  const submit = (event: FormEvent) => {
    event.preventDefault();
    const read = nearestQuery(field, form);
    setError("error" in read ? read.error : "");
    if ("query" in read) props.onSearch(read.query);
  };
  const clear = () => {
    setForm((f) => ({ ...f, text: "", words: "" }));
    setError("");
    props.onSearch(null);
  };
  return (
    <form role="search" aria-label="Nearest rows" className="grid gap-2 rounded-lg border bg-muted/30 p-2.5" onSubmit={submit}>
      <div className="flex flex-wrap items-center gap-2">
        <div className="relative min-w-56 flex-[2]">
          <Radar className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-muted-foreground" aria-hidden="true" />
          <Input
            aria-label={field.model ? "Text to find the nearest rows to" : "Vector to find the nearest rows to"}
            placeholder={field.model ? `Nearest rows to a text, by meaning (${field.model})` : `Nearest rows to a vector of ${field.dimensions} numbers: [0.1, …]`}
            title={describeField(field)}
            autoComplete="off"
            spellCheck={Boolean(field.model)}
            className={field.model ? "h-8 pl-8" : "h-8 pl-8 font-mono text-xs"}
            value={form.text}
            onChange={(e) => setForm((f) => ({ ...f, text: e.target.value }))}
          />
        </div>
        {props.words.length > 0 && (
          <Input
            aria-label="Words the rows hold"
            placeholder={`and words in ${props.words.join(", ")}`}
            title='Only rows whose full-text fields hold these words, as the search of the rows takes them: "a phrase", -word, word*'
            maxLength={500}
            autoComplete="off"
            className="h-8 min-w-40 flex-1"
            value={form.words}
            onChange={(e) => setForm((f) => ({ ...f, words: e.target.value }))}
          />
        )}
        {field.model && props.words.length > 0 && (
          <Label className="flex items-center gap-2 text-xs font-normal" title="Rank by the text's words in the full-text fields as well as by its meaning">
            <Switch aria-label="Hybrid" checked={Boolean(form.hybrid)} onCheckedChange={(hybrid) => setForm((f) => ({ ...f, hybrid }))} />
            Hybrid
          </Label>
        )}
        {field.model && (
          <>
            <Input
              aria-label="Rerank with"
              list="rerankers"
              placeholder="rerank with: none"
              title="A rerank model that reads the text against four times as many rows found, and keeps the best"
              autoComplete="off"
              className="h-8 w-56 font-mono text-[13px]"
              value={form.rerank ?? ""}
              onChange={(e) => setForm((f) => ({ ...f, rerank: e.target.value }))}
            />
            <datalist id="rerankers">
              {(rerankers.data ?? []).map((m) => (
                <option key={m} value={m} />
              ))}
            </datalist>
          </>
        )}
        <SelectInput
          size="sm"
          className="w-28"
          aria-label="How many rows"
          value={String(form.k)}
          display={`${form.k} rows`}
          onChange={(v) => setForm((f) => ({ ...f, k: Number(v) }))}
          options={K_CHOICES.map((k) => ({ value: k, label: `${k} rows` }))}
        />
        <Button type="submit" variant="secondary" size="sm">
          Find nearest
        </Button>
        {props.active && (
          <Button type="button" variant="ghost" size="sm" onClick={clear}>
            <X aria-hidden="true" />
            Clear
          </Button>
        )}
      </div>
      {error && <Notice notice={{ text: error, kind: "error" }} />}
    </form>
  );
}

type Hit = Schemas["VectorHit"] & { row: Row };

/** The rows the search found, best first, with their distance (and a hybrid search's score); a row opens in the editor. */
export function NearestRows(props: {
  workspace: string;
  table: string;
  field: FieldInfo;
  query: NearestQuery;
  fields: FieldInfo[];
  /** The workspace's tables, which the table's refs name */
  tables: TableInfo[];
  /** A graph table: a row opens with its relationships */
  graph: boolean;
  readOnly: boolean;
  onChange(): void;
}) {
  const client = useApi();
  const { workspace, table, query } = props;
  const found = useQuery({
    queryKey: ["db", "rows", workspace, table, "nearest", query],
    queryFn: ({ signal }) => client.post("/db/{workspace}/{table}/_search", { params: { path: { workspace, table } }, body: query, signal }),
  });
  const hits: Hit[] = useMemo(() => found.data?.results ?? [], [found.data]);
  const rows = useMemo(() => hits.map((h) => h.row), [hits]);
  const [editing, setEditing] = useState<Row | null>(null);
  const scored = hits.some((h) => h.score != null);
  const columns: GridColumn<Hit>[] = useMemo(
    () => [
      ...(scored
        ? [{
            key: "_score",
            label: "score",
            align: "right" as const,
            headTitle: "Higher better: the reranker's (0 to 1), or a hybrid search's, by the row's rank among the nearest rows and among those holding the words",
            className: "font-mono text-[13px]",
            render: (h: Hit) => (h.score == null ? "" : h.score.toFixed(4)),
          }]
        : []),
      {
        key: "_distance",
        label: "distance",
        align: "right",
        headTitle: distanceMeaning(props.field.similarity),
        className: "font-mono text-[13px]",
        render: (h) => (h.distance == null ? <span className="text-muted-foreground" title="Found by its words only">—</span> : fmtDistance(h.distance)),
      },
      ...columnsOf(rows).map((c, i) => ({
        key: c,
        label: <span className="font-mono text-xs">{c}</span>,
        className: i === 0 ? "max-w-[22rem] font-mono text-[13px] font-medium" : "max-w-[22rem]",
        render: (h: Hit) => <Cell value={h.row[c]} />,
      })),
    ],
    [rows, scored, props.field.similarity],
  );
  const error = errorText(found.error);
  const tokens = found.data?.tokens;
  return (
    <>
      {error && <Notice notice={{ text: error, kind: "error" }} />}
      <div className="max-h-[65vh] overflow-auto rounded-lg border">
        <DataGrid
          aria-label="Nearest rows"
          columns={columns}
          rows={hits}
          rowKey={(h) => h.row.id}
          loading={found.isFetching}
          empty={error ? "The search failed" : "No rows with a vector match"}
          onRow={(h) => setEditing(h.row)}
          headerClassName="sticky top-0 z-10 bg-muted/90 backdrop-blur"
        />
      </div>
      <p className="text-xs text-muted-foreground">
        {found.data ? `${fmtInt(hits.length)} ${scored ? "best" : "nearest"} first` : "Searching…"}
        {tokens != null && ` · the text took ${fmtInt(tokens)} tokens to embed`}
      </p>
      <RowEditor
        workspace={workspace}
        table={table}
        tables={props.tables}
        row={editing}
        graph={props.graph}
        readOnly={props.readOnly}
        vector={props.field}
        columns={props.fields.map((f) => f.name)}
        onClose={() => setEditing(null)}
        onSaved={() => {
          setEditing(null);
          props.onChange();
        }}
      />
    </>
  );
}
