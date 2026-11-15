// A row of a Data section table as JSON in a dialog, opened from its rows
// (TableBrowser.tsx): saved whole (PUT), added (POST) or deleted, or only shown
// when the service keeps the data (evals, jobs...). Its table refs are listed
// under the JSON, each set by picking rows (DataRefs.tsx). A graph table's row lists its
// relationships, its edges (GET .../{id}/_edges), each linked to its type and its other end.
// A row opens as it is now, read alone (GET), since lists leave out a vector
// field, which saving the row as listed would drop: a vector the rows carry
// is shown and kept, one embedded with a model left out (saving embeds the
// text again, and the server refuses a row that sends it).
import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ArrowLeft, ArrowRight, Waypoints } from "lucide-react";
import { Button, buttonVariants } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { useAction } from "../../shared/action";
import { CodeEditor } from "../../shared/code-editor";
import { useApi } from "../../shared/api";
import { ConfirmButton, CopyButton, Notice, SubmitButton } from "../../shared/components";
import { errorText } from "../../shared/query";
import { dataHash, parseRow, preview, without, type FieldInfo, type Row, type TableInfo } from "./data";
import { RowRefs } from "./DataRefs";
import { EdgeEnd } from "./DataRows";

export function RowEditor(props: {
  workspace: string;
  table: string;
  /** The workspace's tables, which the table's refs name: without them, the editor lists none */
  tables?: TableInfo[];
  row: Row | "new" | null;
  /** The table is a graph table: the row lists its relationships and can be drawn in the Graph query */
  graph?: boolean;
  /** Show the row, without saving or deleting */
  readOnly?: boolean;
  /** The table's vector field, if it has one */
  vector?: FieldInfo;
  columns: string[];
  onClose(): void;
  onSaved(): void;
}) {
  const client = useApi();
  const { row, vector } = props;
  const isNew = row === "new";
  const [text, setText] = useState("");
  // Embedded: the server makes it from the text, and refuses a row that sends it.
  const embedded = vector?.model ? vector.name : undefined;
  const whole = client.useQuery(
    "get",
    "/db/{workspace}/{table}/{row_id}",
    { params: { path: { workspace: props.workspace, table: props.table, row_id: row && row !== "new" ? row.id : "" } } },
    { enabled: Boolean(row && row !== "new"), gcTime: 0 },
  );
  // This row as read alone (the cache may still hold the row opened before while this one loads).
  const read = row && row !== "new" && whole.data?.id === row.id ? whole.data : undefined;
  // Until it is read whole, the row cannot be saved: what a list gave lacks its vector.
  const waiting = Boolean(row && row !== "new" && !read && !whole.error);

  const save = useAction(
    async () => {
      if (row === null) return;
      remove.clear();
      const parsed = parseRow(text);
      if ("error" in parsed) throw new Error(parsed.error);
      const path = { workspace: props.workspace, table: props.table };
      if (row === "new") {
        const made = await client.post("/db/{workspace}/{table}", { params: { path }, body: parsed.row });
        props.onSaved();
        return `Added ${made.id}.`;
      }
      if (parsed.row.id !== row.id) throw new Error("The id cannot change here: add a new row with the new id instead.");
      await client.put("/db/{workspace}/{table}/{row_id}", { params: { path: { ...path, row_id: row.id } }, body: parsed.row });
      props.onSaved();
      return `Saved ${row.id}.`;
    },
    { notice: true, success: (said) => said },
  );

  const remove = useAction(
    async () => {
      if (isNew || !row) return;
      save.clear();
      await client.delete("/db/{workspace}/{table}/{row_id}", { params: { path: { workspace: props.workspace, table: props.table, row_id: row.id } } });
      props.onSaved();
      return row.id;
    },
    { notice: true, success: (id) => id && `Deleted ${id}.` },
  );
  const notice = save.notice.text ? save.notice : remove.notice.text ? remove.notice : { text: errorText(whole.error), kind: "error" as const };

  useEffect(() => {
    if (row === null) return;
    save.clear();
    remove.clear();
    if (row === "new") {
      // A template with the table's fields, so a new row looks like the others.
      const template: Record<string, unknown> = { id: "" };
      for (const c of props.columns) if (c !== "id" && c !== embedded) template[c] = null;
      setText(JSON.stringify(template, null, 2));
    } else {
      setText(JSON.stringify(without(row, embedded), null, 2));
    }
    // Only when a row opens: the columns changing must not reset what is being typed.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [row]);

  // The row whole, once read.
  useEffect(() => {
    if (read) setText(JSON.stringify(without(read, embedded), null, 2));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [read]);

  return (
    <Dialog open={row !== null} onOpenChange={(open) => !open && props.onClose()}>
      <DialogContent className="sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle className="font-mono">{isNew ? `New row in ${props.table}` : row?.id}</DialogTitle>
          <DialogDescription>
            {isNew
              ? "JSON. Leave id empty to have one made."
              : `${props.workspace}/${props.table} · saving replaces the whole row (PUT).`}
            {embedded && ` ${embedded} is embedded from ${vector?.from} when it is saved.`}
          </DialogDescription>
        </DialogHeader>
        <CodeEditor
          aria-label="Row as JSON"
          language="json"
          lineNumbers
          minHeight="18rem"
          maxHeight="55vh"
          value={text}
          readOnly={props.readOnly}
          onChange={setText}
          onRun={props.readOnly || waiting ? undefined : () => void save.run()}
        />
        {row && props.tables && (
          <RowRefs
            workspace={props.workspace}
            table={props.table}
            tables={props.tables}
            id={isNew ? undefined : row.id}
            text={text}
            readOnly={props.readOnly || waiting}
            onText={setText}
          />
        )}
        {!isNew && row && props.graph && <RowEdges workspace={props.workspace} table={props.table} id={row.id} onFollow={props.onClose} />}
        <Notice notice={notice} />
        <DialogFooter className="sm:justify-between">
          <div className="flex gap-2">
            {!isNew && row && !props.readOnly && (
              <ConfirmButton label="Delete row" title={`Delete ${row.id}?`} body="This cannot be undone." onConfirm={remove.run} />
            )}
            {!isNew && row && props.graph && (
              <a
                className={buttonVariants({ variant: "outline" })}
                href={dataHash({ workspace: props.workspace, graph: true, graphNode: `${props.table}/${row.id}` })}
                title="Show the row and its neighbors in the Graph query"
                onClick={props.onClose}
              >
                <Waypoints aria-hidden="true" />
                Graph
              </a>
            )}
            <CopyButton value={text} />
          </div>
          <div className="flex gap-2">
            <Button type="button" variant="outline" onClick={props.onClose}>
              {props.readOnly ? "Close" : "Cancel"}
            </Button>
            {!props.readOnly && (
              <SubmitButton type="button" onClick={save.run} busy={save.busy || waiting}>
                {isNew ? "Add row" : "Save"}
              </SubmitButton>
            )}
          </div>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/** How many of a row's relationships its editor lists; the relationship pages have the rest. */
const ROW_EDGES = 100;

/**
 * The row's relationships (edges), those going from it first: the type (a link to its page),
 * the row at the other end (a link to the Graph query) and the edge's own
 * properties. ``onFollow`` closes the dialog when a link is followed.
 */
function RowEdges(props: { workspace: string; table: string; id: string; onFollow(): void }) {
  const client = useApi();
  const { workspace, table, id } = props;
  // Keyed under the table's loads, so its page's Refresh reloads it too.
  const edges = useQuery({
    queryKey: ["db", "rows", workspace, table, "edges-of", id],
    queryFn: ({ signal }) =>
      client.get("/db/{workspace}/{table}/{row_id}/_edges", { params: { path: { workspace, table, row_id: id }, query: { _limit: ROW_EDGES } }, signal }),
  });
  const me = `${table}/${id}`;
  const list = (edges.data?.edges ?? [])
    .map((e) => ({ edge: e, out: e.from === me }))
    .sort((a, b) => Number(b.out) - Number(a.out) || a.edge.type.localeCompare(b.edge.type));
  const error = errorText(edges.error);
  return (
    <div className="grid gap-1.5">
      <div className="text-sm font-medium">
        Relationships
        {edges.data && (
          <span className="ml-1.5 font-normal text-muted-foreground tabular-nums">
            {list.length}
            {edges.data.next ? `, the first ${ROW_EDGES}` : ""}
          </span>
        )}
      </div>
      {error ? (
        <Notice notice={{ text: error, kind: "error" }} />
      ) : !edges.data ? (
        <p className="text-xs text-muted-foreground">Loading…</p>
      ) : !list.length ? (
        <p className="text-xs text-muted-foreground">None yet. Clients add them with PUT /db/{workspace}/_edges.</p>
      ) : (
        <ul className="max-h-40 overflow-auto rounded-md border text-sm" onClick={(e) => (e.target as HTMLElement).closest("a") && props.onFollow()}>
          {list.map(({ edge, out }) => {
            const { type, from, to, ...rest } = edge;
            const shown = Object.keys(rest).length ? preview(rest, 80) : "";
            return (
              <li key={`${type}|${from}|${to}`} className="flex items-center gap-2 border-b px-3 py-1.5 last:border-b-0">
                {out ? (
                  <ArrowRight className="size-3.5 shrink-0 text-muted-foreground" aria-label="goes from this row" />
                ) : (
                  <ArrowLeft className="size-3.5 shrink-0 text-muted-foreground" aria-label="comes to this row" />
                )}
                <a className="shrink-0 font-mono text-xs hover:underline" href={dataHash({ workspace, edge: type })} title={`Every ${type} relationship`}>
                  {type}
                </a>
                <EdgeEnd workspace={workspace} end={out ? to : from} />
                {shown && (
                  <span className="ml-auto min-w-0 truncate font-mono text-xs text-muted-foreground" title={JSON.stringify(rest, null, 2)}>
                    {shown}
                  </span>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
