// The model server entries down the side, each with its status and serving backend.

import { ServerCog } from "lucide-react";
import { cn } from "@/lib/utils";
import { StatusDot } from "../../shared/components";
import type { ModelServerInfo, ModelServers } from "../../shared/types";
import { backendLabel, ORIGIN, serversHash, STATUS_COLOR, type ServersLocation } from "./servers";
import { ListCard, NavRow, NewLink, NoRows } from "../layout";

export function EntriesCard(props: { data: ModelServers | null; loc: ServersLocation; error: string; reload(): Promise<unknown> }) {
  const { data, loc } = props;
  const chosen = loc.view === "model" ? loc.id : null;
  const loaded = data?.models.filter((m) => m.status === "loaded").length ?? 0;
  return (
    <ListCard
      title="Models"
      description={data ? `${data.models.length} · ${loaded} loaded` : "Loading…"}
      label="Model servers"
      onRefresh={props.reload}
      action={data && <NewLink href={serversHash({ view: "new" })} />}
      loaded={Boolean(data || props.error)}
      // Without data the failure is the whole view, on the right.
      error={data ? props.error : ""}
    >
      <NavRow href={serversHash({ view: "overview" })} active={loc.view === "overview"}>
        <ServerCog aria-hidden="true" />
        Providers & files
      </NavRow>
      {data?.models.map((m) => (
        <EntryRow key={m.model_id} info={m} active={m.model_id === chosen} />
      ))}
      {data && !data.models.length && <NoRows>No model servers yet</NoRows>}
    </ListCard>
  );
}
function EntryRow({ info, active }: { info: ModelServerInfo; active: boolean }) {
  return (
    <NavRow href={serversHash({ view: "model", id: info.model_id })} active={active} tall>
      <span className="flex min-w-0 items-center gap-2">
        <span title={info.serving ? `${info.status}: ${info.serving}` : info.status} className="inline-flex">
          <StatusDot color={STATUS_COLOR[info.status] ?? "var(--muted-foreground)"} />
        </span>
        <span className={cn("truncate font-mono text-[13px]", active && "font-medium")}>{info.model_id}</span>
        <span className="ml-auto shrink-0 text-[11px] text-muted-foreground">{ORIGIN[info.origin]}</span>
      </span>
      {/* The backends in the order they are tried. */}
      <span className="truncate pl-4 text-[11px] text-muted-foreground">{info.backends.map(backendLabel).join(" → ")}</span>
    </NavRow>
  );
}
