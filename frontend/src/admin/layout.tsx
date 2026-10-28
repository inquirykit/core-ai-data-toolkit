// The master-detail shape of the sections that list things on the left and
// show the chosen one on the right: Data, Evals, Servers, Enrichers, Evidence.
import type { ReactNode } from "react";
import { Plus, RotateCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";
import { Notice } from "../shared/components";

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
