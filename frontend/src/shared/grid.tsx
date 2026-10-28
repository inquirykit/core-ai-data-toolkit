// One table for the lists of /admin: the columns say what each cell holds,
// DataGrid draws the shadcn Table around them, with the one row that says
// there is nothing (or that the rows load) and what a click on a row does.
// The server-side browser of the Data section (TableBrowser.tsx) keeps its
// own table: its header cells are sort buttons.
import { Fragment, type ComponentProps, type ReactNode } from "react";
import { Table, TableBody, TableCaption, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { cn } from "@/lib/utils";

export interface GridColumn<T> {
  key: string;
  /** The header; none for a column of buttons */
  label?: ReactNode;
  /** Figures: right-aligned, in tabular numerals */
  align?: "left" | "right";
  /** On the header cell */
  headClassName?: string;
  /** On each cell: a text, or one made from the row */
  className?: string | ((row: T) => string | undefined);
  /** A cell's tooltip, from the row */
  title?(row: T): string | undefined;
  render(row: T, index: number): ReactNode;
}

export function DataGrid<T>(props: {
  columns: GridColumn<T>[];
  rows: T[];
  rowKey(row: T, index: number): string | number;
  /** What the one row says when there are none */
  empty: ReactNode;
  /** The rows are on their way: the empty row says so instead */
  loading?: boolean;
  /** What a click on a row does; the rows show a pointer */
  onRow?(row: T): void;
  /** Where a click on a row goes (a hash), when not ``onRow`` */
  rowHref?(row: T): string;
  /** Anything else a row takes: a title, a tabIndex, align-top. Its onClick is used when there is no ``onRow`` */
  rowProps?(row: T): ComponentProps<"tr">;
  /** A second row under one, across every column (an item opened for its full text); nothing for the rest */
  detail?(row: T): ReactNode;
  /** On the header: bg-muted/50 tints it, sticky top-0 … pins it while the body scrolls */
  headerClassName?: string;
  caption?: ReactNode;
  "aria-label"?: string;
}) {
  const { columns, rowHref } = props;
  const onRow = props.onRow ?? (rowHref && ((row: T) => (location.hash = rowHref(row))));
  return (
    <Table aria-label={props["aria-label"]}>
      {props.caption && <TableCaption>{props.caption}</TableCaption>}
      <TableHeader className={props.headerClassName}>
        <TableRow className="hover:bg-transparent">
          {columns.map((c) => (
            <TableHead key={c.key} className={cn(c.align === "right" && "text-right", c.headClassName)}>
              {c.label}
            </TableHead>
          ))}
        </TableRow>
      </TableHeader>
      <TableBody>
        {props.rows.length ? (
          props.rows.map((row, i) => {
            const extra = props.rowProps?.(row);
            const detail = props.detail?.(row);
            return (
              <Fragment key={props.rowKey(row, i)}>
                <TableRow
                  {...extra}
                  className={cn(onRow && "cursor-pointer", extra?.className)}
                  onClick={onRow ? () => onRow(row) : extra?.onClick}
                  // A row that opens something is reachable by keyboard: Tab to it, Enter opens it.
                  tabIndex={onRow ? (extra?.tabIndex ?? 0) : extra?.tabIndex}
                  onKeyDown={onRow ? (e) => e.key === "Enter" && e.target === e.currentTarget && onRow(row) : extra?.onKeyDown}
                >
                  {columns.map((c) => (
                    <TableCell
                      key={c.key}
                      className={cn(c.align === "right" && "text-right tabular-nums", typeof c.className === "function" ? c.className(row) : c.className)}
                      title={c.title?.(row)}
                    >
                      {c.render(row, i)}
                    </TableCell>
                  ))}
                </TableRow>
                {detail != null && detail !== false && (
                  <TableRow className="hover:bg-transparent">
                    <TableCell colSpan={columns.length} className="bg-muted/20 whitespace-normal">
                      {detail}
                    </TableCell>
                  </TableRow>
                )}
              </Fragment>
            );
          })
        ) : (
          <TableRow className="hover:bg-transparent">
            <TableCell colSpan={Math.max(1, columns.length)} className="h-24 text-center text-muted-foreground">
              {props.loading ? "Loading…" : props.empty}
            </TableCell>
          </TableRow>
        )}
      </TableBody>
    </Table>
  );
}
