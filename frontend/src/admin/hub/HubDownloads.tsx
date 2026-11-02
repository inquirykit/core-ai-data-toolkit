// The downloads, queued, running and finished, with their progress, speed, cancel and dismiss.

import { X } from "lucide-react";
import { useAction } from "../../shared/action";
import { Badge } from "@/components/ui/badge";
import { Progress } from "@/components/ui/progress";
import { cn } from "@/lib/utils";
import type { ApiClient } from "../../shared/api";
import { StatusLabel, SubmitButton } from "../../shared/components";
import { fmtAgo, statusOf } from "../../shared/format";
import type { HubDownload } from "../../shared/types";
import { DOWNLOAD_STATUS, downloadProgress, fmtSpeed, isActive, type DownloadView } from "./hub";
import { LoadedCard } from "../layout";

export function DownloadsCard(props: { client: ApiClient; downloads: DownloadView[] | null; error: string; onChange(): Promise<unknown> }) {
  const { client, downloads } = props;
  const remove = useAction(
    async (d: HubDownload) => {
      await client.delete("/hub/downloads/{download_id}", { params: { path: { download_id: d.id } } });
      await props.onChange();
    },
    { failed: (d) => (isActive(d) ? `Could not cancel ${d.repo_id}` : `Could not dismiss ${d.repo_id}`) },
  );

  const moving = downloads?.filter((v) => isActive(v.row)).length ?? 0;
  return (
    <LoadedCard
      title="Downloads"
      description={(views) => (moving ? `${views.length} · ${moving} in progress` : `${views.length}`)}
      data={downloads}
      error={props.error}
      onRefresh={props.onChange}
      skeleton="h-16"
      isEmpty={(views) => views.length === 0}
      empty="No downloads yet. Search above and download a model so the first request does not have to."
    >
      {(views) => (
        <ul className="grid gap-2">
          {views.map((v) => (
            <DownloadRow key={v.row.id} view={v} busy={remove.running?.[0].id === v.row.id} onRemove={() => remove.run(v.row)} />
          ))}
        </ul>
      )}
    </LoadedCard>
  );
}
function DownloadRow({ view, busy, onRemove }: { view: DownloadView; busy: boolean; onRemove(): void }) {
  const { row: d } = view;
  const progress = downloadProgress(d);
  const live = isActive(d);
  const when = d.finished ? `finished ${fmtAgo(d.finished)}` : d.started ? `started ${fmtAgo(d.started)}` : "waiting";
  return (
    <li className="grid gap-2 rounded-lg border p-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="min-w-0 flex-1 font-mono text-[13px] font-medium [overflow-wrap:anywhere]">{d.repo_id}</span>
        <Badge variant="outline">
          <StatusLabel status={statusOf(DOWNLOAD_STATUS, d.status)} live={d.status === "running"} className="gap-1.5" />
        </Badge>
        <SubmitButton type="button" variant="outline" size="sm" className={cn(live && "text-destructive hover:text-destructive")} busy={busy} icon={<X aria-hidden="true" />} onClick={onRemove}>
          {live ? "Cancel" : "Dismiss"}
        </SubmitButton>
      </div>
      {progress.fraction === null ? (
        // No total yet: a bar that breathes instead of filling.
        <div className="relative h-2 w-full overflow-hidden rounded-full bg-primary/15" role="progressbar" aria-label="Downloading, size not yet known">
          <div className="h-full w-full animate-pulse rounded-full bg-primary/50" />
        </div>
      ) : (
        <Progress
          value={progress.fraction * 100}
          aria-label={`${Math.round(progress.fraction * 100)}% downloaded`}
          indicatorClassName={cn(d.status === "failed" && "bg-critical", d.status === "cancelled" && "bg-warning")}
        />
      )}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground tabular-nums">
        <span>{progress.label}</span>
        {d.status === "running" && view.speed !== null && <span>{fmtSpeed(view.speed)}</span>}
        <span>{when}</span>
      </div>
      {d.error && <p className="text-xs text-critical-text [overflow-wrap:anywhere]">{d.error}</p>}
    </li>
  );
}
