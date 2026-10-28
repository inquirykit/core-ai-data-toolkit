// The grid the Data section draws a page of rows in: a table's rows
// (TableBrowser.tsx), a graph table's rows and a relationship's edges
// (DataGraphRows.tsx). Its header cells are sort buttons, which is why it is
// not shared/grid.tsx; Cell draws a value here and in the query panel's
// results, EdgeEnd an edge's end here and in a row's editor.
import type { KeyboardEvent, ReactNode } from "react";
import { ArrowDown, ArrowUp, ChevronLeft, ChevronRight, Zap } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { cn } from "@/lib/utils";
import { fmtInt } from "../shared/format";
import { cellKind, dataHash, describeField, preview, type FieldInfo, type Sort } from "./data";

export const PAGE_SIZES: [string, string][] = [
  ["25", "25"],
  ["100", "100"],
  ["500", "500"],
];

/** A page of rows under sortable column headers; the first column stays put while the grid scrolls sideways. */
export function RowsGrid<T extends Record<string, unknown>>(props: {
  columns: string[];
  rows: T[] | null;
  rowKey(row: NoInfer<T>, index: number): string;
  sort: Sort | null;
  onSort(column: string): void;
  /** The table's declared fields, to say a column's type and mark the indexed ones */
  declared?: Map<string, FieldInfo>;
  /** What a click (or Enter) on a row does; none for rows that open nothing */
  onRow?(row: NoInfer<T>): void;
  /** A cell drawn otherwise than by Cell: an edge's ends as links */
  cell?(column: string, row: NoInfer<T>): ReactNode | undefined;
  loading: boolean;
  /** What the one row says when there are none */
  empty: string;
}) {
  const { columns, declared, onRow } = props;
  return (
    <div className={cn("max-h-[65vh] overflow-auto rounded-lg border transition-opacity", props.loading && "opacity-60")}>
      <Table>
        <TableHeader className="sticky top-0 z-10 bg-muted/90 backdrop-blur">
          <TableRow className="hover:bg-transparent">
            {columns.map((c, i) => (
              <TableHead key={c} className={cn("p-0", i === 0 && "sticky left-0 z-10 bg-muted")}>
                <button
                  type="button"
                  className="flex h-10 w-full items-center gap-1 px-3 font-mono text-xs font-medium hover:text-foreground"
                  title={`Sort by ${c}${declared?.has(c) ? ` (${describeField(declared.get(c)!)})` : ""}`}
                  onClick={() => props.onSort(c)}
                >
                  {c}
                  {declared?.get(c)?.indexed && <Zap className="size-3 text-[var(--series-1)]" aria-label="indexed" />}
                  {props.sort?.field === c && (props.sort.desc ? <ArrowDown className="size-3" aria-label="descending" /> : <ArrowUp className="size-3" aria-label="ascending" />)}
                </button>
              </TableHead>
            ))}
          </TableRow>
        </TableHeader>
        <TableBody>
          {props.rows?.map((row, r) => (
            <TableRow
              key={props.rowKey(row, r)}
              tabIndex={onRow ? 0 : undefined}
              className={cn(onRow && "cursor-pointer focus-visible:bg-muted focus-visible:outline-none")}
              onClick={onRow && (() => onRow(row))}
              onKeyDown={onRow && ((e: KeyboardEvent) => e.key === "Enter" && onRow(row))}
            >
              {columns.map((c, i) => (
                <TableCell key={c} className={cn("max-w-[22rem] px-3 py-2", i === 0 && "sticky left-0 bg-background font-mono text-[13px] font-medium")}>
                  {props.cell?.(c, row) ?? <Cell value={row[c]} />}
                </TableCell>
              ))}
            </TableRow>
          ))}
          {props.rows && !props.rows.length && (
            <TableRow className="hover:bg-transparent">
              <TableCell colSpan={columns.length} className="h-28 text-center text-muted-foreground">
                {props.empty}
              </TableCell>
            </TableRow>
          )}
        </TableBody>
      </Table>
    </div>
  );
}

/** "26–50 of 300" and the buttons to the pages either side. */
export function Pager(props: { start: number; shown: number; count: number | null; hasPrevious: boolean; hasNext: boolean; loading: boolean; onPrevious(): void; onNext(): void }) {
  return (
    <div className="flex items-center justify-between gap-3 text-sm text-muted-foreground">
      <span className="tabular-nums">
        {props.shown ? `${fmtInt(props.start + 1)}–${fmtInt(props.start + props.shown)}${props.count != null ? ` of ${fmtInt(props.count)}` : ""}` : ""}
      </span>
      <div className="flex gap-2">
        <Button type="button" variant="outline" size="sm" disabled={!props.hasPrevious || props.loading} onClick={props.onPrevious}>
          <ChevronLeft aria-hidden="true" />
          Previous
        </Button>
        <Button type="button" variant="outline" size="sm" disabled={!props.hasNext || props.loading} onClick={props.onNext}>
          Next
          <ChevronRight aria-hidden="true" />
        </Button>
      </div>
    </div>
  );
}

/** One value in the grid, drawn by its JSON kind. */
export function Cell({ value }: { value: unknown }) {
  switch (cellKind(value)) {
    case "missing":
      return null;
    case "null":
      return <span className="font-mono text-xs text-muted-foreground italic">null</span>;
    case "boolean":
      return <span className={cn("font-mono text-xs", value ? "text-good-text" : "text-muted-foreground")}>{String(value)}</span>;
    case "number":
      return <span className="font-mono text-[13px] tabular-nums">{String(value)}</span>;
    case "string":
      return (
        <span className="block truncate" title={value as string}>
          {value as string}
        </span>
      );
    case "list": {
      const items = value as unknown[];
      return (
        <span className="flex flex-nowrap gap-1" title={JSON.stringify(value)}>
          {items.slice(0, 3).map((item, i) => (
            <Badge key={i} variant="secondary" className="max-w-32 truncate font-mono font-normal">
              {preview(item, 24)}
            </Badge>
          ))}
          {items.length > 3 && <span className="text-xs text-muted-foreground">+{items.length - 3}</span>}
          {!items.length && <span className="font-mono text-xs text-muted-foreground">[]</span>}
        </span>
      );
    }
    default:
      return (
        <span className="block truncate font-mono text-xs text-muted-foreground" title={JSON.stringify(value, null, 2)}>
          {preview(value)}
        </span>
      );
  }
}

/** An edge's end, ``table/id``: a link to the row drawn with its neighbors in the Graph query. */
export function EdgeEnd({ namespace, end }: { namespace: string; end: string }) {
  const cut = end.indexOf("/");
  return (
    <a
      className="font-mono text-[13px] hover:underline"
      href={dataHash({ namespace, graph: true, graphNode: end })}
      title="Show this row and its neighbors in the Graph query"
    >
      <span className="text-muted-foreground">{end.slice(0, cut + 1)}</span>
      {end.slice(cut + 1)}
    </a>
  );
}
