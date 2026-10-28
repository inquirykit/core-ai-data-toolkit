// Small pieces the list and the details share.
import type { ReactNode } from "react";
import type { LucideIcon } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import { StatusDot } from "../components";
import { fmtGB } from "../format";
import type { CatalogModel } from "./catalog";

export interface PickedModel {
  id: string;
  task: string;
}

/** "Loaded" or "Loading" with its dot; nothing for an unloaded model. */
export function StateBadge({ model }: { model: CatalogModel }) {
  if (model.status === "unloaded") return null;
  const loaded = model.status === "loaded";
  return (
    <span className="inline-flex shrink-0 items-center gap-1.5 text-xs whitespace-nowrap text-muted-foreground">
      <StatusDot color={loaded ? "var(--good)" : "var(--warning)"} live={!loaded} />
      {loaded ? "Loaded" : "Loading"}
    </span>
  );
}

export function Tags({ tags }: { tags?: string[] }) {
  if (!tags?.length) return null;
  return (
    <span className="inline-flex flex-wrap gap-1">
      {tags.map((t) => (
        <Badge key={t} variant="secondary" className="rounded-full font-normal text-muted-foreground">
          {t}
        </Badge>
      ))}
    </span>
  );
}

/** Where the weights are: downloaded size, or the model's own server. */
export function diskText(model: CatalogModel): string {
  if (model.kind === "server") return "own server";
  return model.on_disk_gb != null ? `↓ ${fmtGB(model.on_disk_gb)}` : "";
}

/** One labelled block of the details view. */
export function Section({ title, aside, children }: { title: string; aside?: ReactNode; children: ReactNode }) {
  return (
    <section className="grid gap-3 rounded-xl border bg-card p-4 shadow-xs">
      <div className="flex items-baseline justify-between gap-3">
        <h4 className="text-sm font-semibold">{title}</h4>
        {aside}
      </div>
      {children}
    </section>
  );
}

/** Labels and values in two columns (a <dl>): the details' facts and figures. */
export const kvClass =
  "grid grid-cols-[max-content_minmax(0,1fr)] gap-x-5 gap-y-1.5 text-sm [&_dd]:min-w-0 [&_dd]:wrap-anywhere [&_dt]:text-muted-foreground";

/** Nothing to show: a muted icon above a line or two (and maybe a button). */
export function EmptyState({ icon: Icon, className, children }: { icon: LucideIcon; className?: string; children: ReactNode }) {
  return (
    <div
      className={cn(
        "flex animate-in flex-col items-center gap-3 px-4 py-12 text-center text-sm text-muted-foreground duration-300 fade-in-0",
        className,
      )}
    >
      <span className="grid size-11 place-items-center rounded-full bg-muted" aria-hidden="true">
        <Icon className="size-5 text-muted-foreground/80" strokeWidth={1.75} />
      </span>
      {children}
    </div>
  );
}
