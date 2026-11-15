// The master-detail shape of the sections that list things on the left and
// show the chosen one on the right: Data, Evals, Servers, Enrichers, Evidence;
// LoadedCard, the card of one load that the right column (and Hub, Jobs,
// Audit) is made of; and the paging of a long list, by cursors.
import { useState, type ReactNode } from "react";
import { ChevronLeft, ChevronRight, Plus, RotateCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";
import { Notice } from "../shared/components";
import { fmtInt } from "../shared/format";

/** The two columns: ``list`` (one or more ListCards) sticks on the left, the chosen thing's view goes on the right. */
export function MasterDetail({ list, children }: { list: ReactNode; children: ReactNode }) {
  return (
    <section className="grid grid-cols-[minmax(0,1fr)] items-start gap-5 lg:grid-cols-[17rem_minmax(0,1fr)]">
      <div className="grid gap-3 lg:sticky lg:top-20">{list}</div>
      {children}
    </section>
  );
}

/** The right column until the list arrives. */
export const DetailSkeleton = () => <Skeleton className="h-80 rounded-xl" />;

/**
 * The list on the left: a title, what it holds, Refresh and an ``action`` (New, say), then the rows
 * (NavRows) in a nav. Until ``loaded``, a few shimmering rows stand in; ``error`` goes above them.
 */
export function ListCard(props: {
  title: ReactNode;
  description?: ReactNode;
  /** The nav's accessible name */
  label: string;
  onRefresh?(): void;
  action?: ReactNode;
  loaded: boolean;
  error?: string;
  /** Between the header and the rows: a form, say */
  top?: ReactNode;
  footer?: ReactNode;
  children: ReactNode;
}) {
  return (
    <Card className="gap-3 py-4">
      <CardHeader className="px-4">
        <CardTitle>{props.title}</CardTitle>
        {props.description && <CardDescription>{props.description}</CardDescription>}
        {(props.onRefresh || props.action) && (
          <CardAction className="flex gap-1">
            {props.onRefresh && (
              <Button type="button" variant="ghost" size="icon" className="size-8" aria-label="Refresh" onClick={props.onRefresh}>
                <RotateCw aria-hidden="true" />
              </Button>
            )}
            {props.action}
          </CardAction>
        )}
      </CardHeader>
      <CardContent className="grid gap-2 px-2">
        {props.top}
        {props.error && <Notice className="px-2" notice={{ text: props.error, kind: "error" }} />}
        {!props.loaded && !props.error && (
          <div className="grid gap-1.5 px-2">
            <Skeleton className="h-8" />
            <Skeleton className="h-10" />
            <Skeleton className="h-10" />
          </div>
        )}
        <nav aria-label={props.label} className="grid gap-0.5">
          {props.children}
        </nav>
      </CardContent>
      {props.footer && <CardFooter className="grid gap-2 border-t px-4 pt-3 [.border-t]:pt-3">{props.footer}</CardFooter>}
    </Card>
  );
}

/**
 * A card of the right column whose body is one load: ``title``, a ``description`` made from the data
 * ("Loading…" until it comes), Refresh and any other ``actions``; then ``top`` (a form), the error, a
 * shimmer while loading, what ``empty`` says when ``isEmpty`` finds nothing, else ``children`` of the
 * data; ``after`` goes last, data or not (a pager, a form).
 */
export function LoadedCard<T>(props: {
  title: ReactNode;
  description?: ReactNode | ((data: T) => ReactNode);
  data: T | null;
  error?: string;
  onRefresh?(): void;
  actions?: ReactNode;
  top?: ReactNode;
  isEmpty?(data: T): boolean;
  empty?: ReactNode;
  /** The shimmer's height while loading */
  skeleton?: string;
  after?: ReactNode;
  children(data: T): ReactNode;
}) {
  const { data, error } = props;
  const description = typeof props.description === "function" ? (data === null ? "Loading…" : props.description(data)) : props.description;
  const empty = data !== null && (props.isEmpty?.(data) ?? false);
  return (
    <Card className="gap-4">
      <CardHeader>
        <CardTitle>{props.title}</CardTitle>
        {description && <CardDescription>{description}</CardDescription>}
        {(props.onRefresh || props.actions) && (
          <CardAction className="flex items-center gap-2">
            {props.onRefresh && (
              <Button type="button" variant="ghost" size="icon" className="size-8" aria-label="Refresh" onClick={props.onRefresh}>
                <RotateCw aria-hidden="true" />
              </Button>
            )}
            {props.actions}
          </CardAction>
        )}
      </CardHeader>
      <CardContent className="grid gap-3">
        {props.top}
        {error && <Notice notice={{ text: error, kind: "error" }} />}
        {data === null && !error && <Skeleton className={props.skeleton ?? "h-24"} />}
        {empty && <p className="text-sm text-muted-foreground">{props.empty}</p>}
        {data !== null && !empty && props.children(data)}
        {props.after}
      </CardContent>
    </Card>
  );
}

/** A ListCard's "New" action: a link to the page that makes one. */
export function NewLink({ href }: { href: string }) {
  return (
    <Button type="button" size="sm" className="h-8" asChild>
      <a href={href}>
        <Plus aria-hidden="true" />
        New
      </a>
    </Button>
  );
}

/**
 * One row of a ListCard: a link, marked when it is the page shown. One line (its icons sized and muted)
 * unless ``tall``, which is a grid for a name with a line under it. A ``disabled`` row is grayed out and
 * leads nowhere; its ``title`` says why. ``className`` goes last, so it can tint a row and its icons.
 */
export function NavRow(props: { href: string; active: boolean; title?: string; tall?: boolean; disabled?: boolean; className?: string; children: ReactNode }) {
  const className = cn(
    "min-w-0 rounded-md px-2 text-sm outline-none transition-colors",
    props.tall ? "grid gap-1 py-1.5" : "flex h-8 items-center gap-2 [&_svg]:size-3.5 [&_svg]:shrink-0 [&_svg]:text-muted-foreground",
    props.disabled ? "cursor-not-allowed opacity-50" : "hover:bg-muted focus-visible:ring-[3px] focus-visible:ring-ring/50",
    props.active && "bg-muted text-foreground",
    props.active && !props.tall && "font-medium",
    props.className,
  );
  if (props.disabled) {
    return (
      <span role="link" aria-disabled="true" title={props.title} className={className}>
        {props.children}
      </span>
    );
  }
  return (
    <a href={props.href} title={props.title} aria-current={props.active ? "page" : undefined} className={className}>
      {props.children}
    </a>
  );
}

/** The line under an empty ListCard's title. */
export function NoRows({ children }: { children: ReactNode }) {
  return <p className="px-2 py-1 text-xs text-muted-foreground">{children}</p>;
}

// ---------------------------------------------------------------- paging
export const PAGE_SIZES: [string, string][] = [
  ["25", "25"],
  ["100", "100"],
  ["500", "500"],
];

/**
 * Where a list paged by cursors stands: the cursor of this page (the page before's ``next``; undefined: the first),
 * how many pages come before it, and the moves between them. A change of ``resetKey`` (a new search, new filters)
 * goes back to the first page while rendering, as React recommends over an effect.
 */
export function useCursorPages(resetKey: unknown) {
  // The cursors of the pages before this one, and this one's.
  const [cursors, setCursors] = useState<(string | undefined)[]>([undefined]);
  const [key, setKey] = useState(resetKey);
  if (!Object.is(key, resetKey)) {
    setKey(resetKey);
    setCursors([undefined]);
  }
  return {
    cursor: cursors[cursors.length - 1],
    index: cursors.length - 1,
    hasPrevious: cursors.length > 1,
    first: () => setCursors([undefined]),
    previous: () => setCursors((c) => c.slice(0, -1)),
    next: (cursor: string | undefined) => setCursors((c) => [...c, cursor]),
  };
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
