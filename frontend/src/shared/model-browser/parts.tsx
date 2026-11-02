// Small pieces the list and the details share.
import type { ReactNode } from "react";
import { Badge } from "@/components/ui/badge";
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
