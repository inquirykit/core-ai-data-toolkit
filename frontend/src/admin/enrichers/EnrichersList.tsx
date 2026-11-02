// The enrichers, with whether each can run now and why not.

import { Search } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import { StatusDot } from "../../shared/components";
import type { EnricherInfo, EnricherList } from "../../shared/types";
import { enricherHash, origin, usability, type EnricherLocation } from "./enrichers";
import { ListCard, NavRow, NewLink, NoRows } from "../layout";

export function EnrichersCard(props: { list: EnricherList | null; loc: EnricherLocation; error: string; reload(): Promise<unknown> }) {
  const { list, loc } = props;
  const chosen = loc.view === "edit" ? loc.name : null;
  return (
    <ListCard
      title="Enrichers"
      description={list ? `${list.enrichers.length} · ${list.enrichers.filter((e) => e.usable).length} usable` : "Loading…"}
      label="Enrichers"
      onRefresh={props.reload}
      action={<NewLink href={enricherHash({ view: "new" })} />}
      loaded={Boolean(list)}
      error={props.error}
      footer={
        list && (
          <p className="text-xs text-muted-foreground">
            Terms allowed: {list.terms_allowed.length ? list.terms_allowed.join(", ") : "none"}.{" "}
            <code className="text-[11px]">ENRICHER_TERMS</code> changes it.
          </p>
        )
      }
    >
      <NavRow href={enricherHash({ view: "lookup" })} active={loc.view === "lookup"}>
        <Search aria-hidden="true" />
        Lookup
      </NavRow>
      {list?.enrichers.map((e) => (
        <EnricherRow key={e.name} info={e} active={e.name === chosen} />
      ))}
      {list && !list.enrichers.length && <NoRows>No enrichers yet</NoRows>}
    </ListCard>
  );
}
function EnricherRow({ info, active }: { info: EnricherInfo; active: boolean }) {
  const use = usability(info);
  return (
    <NavRow href={enricherHash({ view: "edit", name: info.name })} active={active} tall>
      <span className="flex min-w-0 items-center gap-2">
        <span title={use.label} className="inline-flex">
          <StatusDot color={use.color} />
        </span>
        <span className={cn("truncate font-mono text-[13px]", active && "font-medium")}>{info.name}</span>
        <span className="ml-auto shrink-0 text-[11px] text-muted-foreground">{origin(info)}</span>
      </span>
      <span className="flex flex-wrap gap-1 pl-4">
        {info.inputs.slice(0, 4).map((t) => (
          <Badge key={t} variant="secondary" className="h-4 px-1 font-mono text-[10px] font-normal">
            {t}
          </Badge>
        ))}
        {info.inputs.length > 4 && <span className="text-[10px] text-muted-foreground">+{info.inputs.length - 4}</span>}
      </span>
    </NavRow>
  );
}
