// Table refs (ref fields, as the API calls them) in the Data section: in a
// page of rows (TableBrowser.tsx, DataGraphRows.tsx) a ref's cell shows the
// name of each row it names, a link opening that row, and each table ref of
// another table naming the rows is a column of how many of its rows name each,
// a link to them; a row's editor (DataRowEditor.tsx) lists both under its
// JSON, its own refs each changed by picking rows of its table, which rewrites
// the JSON. refs.ts writes their SQL.
import { useMemo, useState, type ReactNode, type SyntheticEvent } from "react";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useApi } from "../../shared/api";
import { parseJsonObject } from "../../shared/json";
import { errorText } from "../../shared/query";
import { dataHash, describeField, type FieldInfo, type Row, type TableInfo } from "./data";
import { backColumn, backLabel, countsSql, idsOf, namedIds, namesSql, pickSql, referrers, tableRefs, withRef, type Referrer } from "./refs";

/** The most names one query reads (POST /db/{ws}/_query's limit): past it, a row is shown by its id. */
const NAMES_LIMIT = 1000;
/** The rows a picker offers at once: typing narrows them. */
const PICK_LIMIT = 20;

/** By table ref, by id: the name of the row it names (null for a row with none). */
type Names = Map<string, Map<string, string | null>>;
const NO_NAMES: Names = new Map();
/** By referrer's column, by id: how many of its rows name the row. */
type Counts = Map<string, Map<string, number>>;
const NO_COUNTS: Counts = new Map();

/** A link's click (or Enter) is not one on the grid's row, which opens that row. */
const own = (e: SyntheticEvent) => e.stopPropagation();

/** The names, by table ref and id, of the rows the rows' table refs name: a query a ref field, keyed under ``key``. */
function useRefNames(workspace: string, tables: TableInfo[], refs: FieldInfo[], rows: Row[] | null, key: unknown[]): Names {
  const client = useApi();
  const wanted = useMemo(() => refs.map((f) => ({ f, ids: rows ? namedIds(rows, f.name) : [] })).filter((w) => w.ids.length), [refs, rows]);
  const names = useQuery({
    queryKey: [...key, "names", wanted.map(({ f, ids }) => [f.name, f.to, ids])],
    enabled: wanted.length > 0,
    placeholderData: keepPreviousData,
    queryFn: ({ signal }): Promise<Names> =>
      Promise.all(
        wanted.map(async ({ f, ids }) => {
          const to = f.to!;
          const body = { ...namesSql(to, tables.find((t) => t.name === to)?.fields ?? [], ids), limit: NAMES_LIMIT };
          const found = await client.post("/db/{workspace}/_query", { params: { path: { workspace } }, body, signal });
          return [f.name, new Map(found.rows.map((r) => [String(r.id), r.name == null ? null : String(r.name)]))] as const;
        }),
      ).then((pairs) => new Map(pairs)),
  });
  return wanted.length ? (names.data ?? NO_NAMES) : NO_NAMES;
}

/** How many rows of each referrer name each of the rows, by its column: a query a referrer, keyed under ``key``. */
function useRefCounts(workspace: string, named: Referrer[], rows: Row[] | null, key: unknown[]): Counts {
  const client = useApi();
  const ids = useMemo(() => rows?.map((r) => r.id) ?? [], [rows]);
  const counts = useQuery({
    queryKey: [...key, "counts", named.map(backColumn), ids],
    enabled: named.length > 0 && ids.length > 0,
    placeholderData: keepPreviousData,
    queryFn: ({ signal }): Promise<Counts> =>
      Promise.all(
        named.map(async (r) => {
          const body = { ...countsSql(r, ids), limit: NAMES_LIMIT };
          const found = await client.post("/db/{workspace}/_query", { params: { path: { workspace } }, body, signal });
          return [backColumn(r), new Map(found.rows.map((x) => [String(x.id), Number(x.n)]))] as const;
        }),
      ).then((pairs) => new Map(pairs)),
  });
  return named.length && ids.length ? (counts.data ?? NO_COUNTS) : NO_COUNTS;
}

/**
 * How a page of a table's rows draws its table refs: ``cell`` gives a ref's cell, the rows it names as links (three
 * at most, the rest counted), or a referrer's, how many of its rows name the row, a link to them; undefined for any
 * other column, drawn as it is. ``columns`` are the referrers' columns, put after the rows' own, and ``heads`` their
 * headers.
 */
export function useRefCells(workspace: string, table: string, tables: TableInfo[], rows: Row[] | null) {
  const fields = tables.find((t) => t.name === table)?.fields;
  const refs = useMemo(() => tableRefs(fields ?? []), [fields]);
  const named = useMemo(() => referrers(tables, table), [tables, table]);
  const key = ["db", "rows", workspace, table];
  const names = useRefNames(workspace, tables, refs, rows, key);
  const counts = useRefCounts(workspace, named, rows, key);
  const heads = useMemo(
    () =>
      new Map(
        named.map((r) => [backColumn(r), { label: backLabel(r, named), title: `How many rows of ${r.table} name each row by ${r.field.name}` }]),
      ),
    [named],
  );
  return {
    columns: named.map(backColumn),
    heads,
    cell(column: string, row: Row): ReactNode | undefined {
      const back = named.find((r) => backColumn(r) === column);
      if (back) return <NamedBy workspace={workspace} by={back} id={row.id} count={counts.get(column)?.get(row.id)} />;
      const f = refs.find((r) => r.name === column);
      const ids = f ? idsOf(row[column]) : [];
      if (!f || !ids.length) return undefined;
      return (
        <span className="flex flex-nowrap items-center gap-1">
          {ids.slice(0, 3).map((id) => (
            <RefLink key={id} workspace={workspace} to={f.to!} id={id} name={names.get(f.name)?.get(id)} />
          ))}
          {ids.length > 3 && <span className="text-xs text-muted-foreground">+{ids.length - 3}</span>}
        </span>
      );
    },
  };
}

/** How many rows of a referrer's table name the row: a link to them, that table's rows filtered; nothing for none. */
function NamedBy(props: { workspace: string; by: Referrer; id: string; count?: number; label?: string }) {
  const { by, count } = props;
  if (!count) return null;
  return (
    <a
      href={dataHash({ workspace: props.workspace, table: by.table, filter: [by.field.name, props.id] })}
      title={`Open the ${count} rows of ${by.table} whose ${by.field.name} names ${props.id}`}
      onClick={own}
      onKeyDown={own}
      className="inline-flex items-center gap-1 rounded-md border px-1.5 text-[13px] leading-5 tabular-nums hover:border-ring hover:underline"
    >
      {props.label && <span className="font-mono text-xs text-muted-foreground">{props.label}</span>}
      {count}
    </a>
  );
}

/** A row a table ref names: its name (its id when it has none, or until it is read), a link opening it in its table. */
function RefLink(props: { workspace: string; to: string; id: string; name?: string | null }) {
  return (
    <a
      href={dataHash({ workspace: props.workspace, table: props.to, row: props.id })}
      title={`Open ${props.to} ${props.id}`}
      onClick={own}
      onKeyDown={own}
      className="inline-flex max-w-40 min-w-0 items-center rounded-md border bg-secondary px-1.5 text-[13px] leading-5 text-secondary-foreground hover:border-ring hover:underline"
    >
      <span className="truncate">{props.name ?? props.id}</span>
    </a>
  );
}

/**
 * A row's table refs in its editor, under its JSON: the rows each names, links opening them; unless ``readOnly``,
 * each is changed by taking a row out, or by picking rows of its table, which rewrites the JSON. Then how many rows
 * of other tables name it, links to them.
 */
export function RowRefs(props: {
  workspace: string;
  table: string;
  tables: TableInfo[];
  /** The row's id, when it is one there (not a new row) */
  id?: string;
  text: string;
  readOnly?: boolean;
  onText(text: string): void;
}) {
  const { workspace, tables } = props;
  const fields = tables.find((t) => t.name === props.table)?.fields;
  const refs = useMemo(() => tableRefs(fields ?? []), [fields]);
  const parsed = useMemo(() => parseJsonObject(props.text), [props.text]);
  const row = parsed.ok ? (parsed.value as Row) : null;
  const rows = useMemo(() => (row ? [row] : null), [row]);
  const key = ["db", "rows", workspace, props.table, "editor"];
  const names = useRefNames(workspace, tables, refs, rows, key);
  const named = useMemo(() => referrers(tables, props.table), [tables, props.table]);
  // Only a row that is there is named: not a new one, nor one whose id is being typed.
  const saved = useMemo(() => (props.id ? [{ id: props.id }] : null), [props.id]);
  const counts = useRefCounts(workspace, named, saved, key);
  const [picking, setPicking] = useState<string | null>(null);
  const [error, setError] = useState("");
  if (!refs.length && !(props.id && named.length)) return null;

  const set = (f: FieldInfo, ids: string[]) => {
    const typed = withRef(props.text, f, ids);
    setError("error" in typed ? typed.error : "");
    if ("text" in typed) props.onText(typed.text);
  };

  return (
    <section aria-label="Table refs" className="grid gap-2 rounded-lg border p-3">
      <div className="text-xs font-medium text-muted-foreground">Table refs</div>
      {refs.map((f) => {
        const ids = row ? idsOf(row[f.name]) : [];
        const to = f.to!;
        return (
          <div key={f.name} className="grid gap-1.5">
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="w-28 shrink-0 truncate font-mono text-[13px]" title={describeField(f)}>
                {f.name}
              </span>
              {ids.map((id) => (
                <span key={id} className="inline-flex items-center">
                  <RefLink workspace={workspace} to={to} id={id} name={names.get(f.name)?.get(id)} />
                  {!props.readOnly && (
                    <button
                      type="button"
                      aria-label={`Take ${id} out of ${f.name}`}
                      className="grid size-5 place-items-center rounded-sm text-muted-foreground hover:bg-muted hover:text-foreground"
                      onClick={() => set(f, ids.filter((x) => x !== id))}
                    >
                      <X className="size-3" />
                    </button>
                  )}
                </span>
              ))}
              {!ids.length && <span className="text-xs text-muted-foreground">none</span>}
              {!props.readOnly && (
                <Button type="button" variant="ghost" size="sm" className="h-6 px-2 text-xs" onClick={() => setPicking(picking === f.name ? null : f.name)}>
                  {picking === f.name ? "Done" : f.list ? "Add" : ids.length ? "Change" : "Choose"}
                </Button>
              )}
            </div>
            {picking === f.name && (
              <RefPicker
                workspace={workspace}
                to={to}
                fields={tables.find((t) => t.name === to)?.fields ?? []}
                taken={ids}
                onPick={(id) => {
                  set(f, f.list ? [...ids, id] : [id]);
                  if (!f.list) setPicking(null);
                }}
              />
            )}
          </div>
        );
      })}
      {props.id && named.length > 0 && (
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="w-28 shrink-0 text-xs text-muted-foreground">Named by</span>
          {named.map((r) => (
            <NamedBy key={backColumn(r)} workspace={workspace} by={r} id={props.id!} count={counts.get(backColumn(r))?.get(props.id!)} label={backLabel(r, named)} />
          ))}
          {!named.some((r) => counts.get(backColumn(r))?.get(props.id!)) && <span className="text-xs text-muted-foreground">no rows</span>}
        </div>
      )}
      {error && <p className="text-xs text-destructive">{error}</p>}
    </section>
  );
}

/** The rows of ``to`` to pick from, by name: those whose id or name holds what is typed, PICK_LIMIT at most. */
function RefPicker(props: { workspace: string; to: string; fields: FieldInfo[]; taken: string[]; onPick(id: string): void }) {
  const client = useApi();
  const { workspace, to } = props;
  const [typed, setTyped] = useState("");
  const found = useQuery({
    queryKey: ["db", "rows", workspace, to, "pick", typed],
    placeholderData: keepPreviousData,
    queryFn: ({ signal }) =>
      client.post("/db/{workspace}/_query", { params: { path: { workspace } }, body: { ...pickSql(to, props.fields, typed), limit: PICK_LIMIT }, signal }),
  });
  const rows = found.data?.rows ?? [];
  const error = errorText(found.error);
  return (
    <div className="grid gap-1 sm:ml-[7.4rem]">
      <Input
        type="search"
        autoFocus
        aria-label={`Find a row of ${to}`}
        placeholder={`Find a row of ${to}`}
        autoComplete="off"
        className="h-8"
        value={typed}
        onChange={(e) => setTyped(e.target.value)}
      />
      <ul aria-label={`Rows of ${to}`} className="max-h-48 overflow-auto rounded-md border">
        {rows.map((r) => {
          const id = String(r.id);
          const name = r.name == null ? null : String(r.name);
          return (
            <li key={id}>
              <button
                type="button"
                disabled={props.taken.includes(id)}
                className="flex w-full items-center gap-2 px-2 py-1 text-left text-sm hover:bg-muted disabled:opacity-50"
                onClick={() => props.onPick(id)}
              >
                <span className="truncate">{name ?? id}</span>
                {name !== null && <span className="ml-auto shrink-0 font-mono text-xs text-muted-foreground">{id}</span>}
              </button>
            </li>
          );
        })}
        {found.data && !rows.length && (
          <li className="px-2 py-1 text-xs text-muted-foreground">{typed.trim() ? `No row of ${to} holds "${typed.trim()}"` : `${to} has no rows yet`}</li>
        )}
      </ul>
      {error && <p className="text-xs text-destructive">{error}</p>}
    </div>
  );
}
