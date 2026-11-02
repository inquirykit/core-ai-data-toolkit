// What is on disk: the Hub cache's repos with their size and last use, and deleting their weights.

import { useAction } from "../../shared/action";
import { Badge } from "@/components/ui/badge";
import type { ApiClient } from "../../shared/api";
import { ConfirmButton, StatusDot } from "../../shared/components";
import { fmtAgo, fmtBytes, fmtInt } from "../../shared/format";
import { DataGrid, type GridColumn } from "../../shared/grid";
import type { HubDownload, HubLocal, HubLocalRepo } from "../../shared/types";
import { isActive, sortLocal } from "./hub";
import { LoadedCard } from "../layout";

export function LocalCard(props: { client: ApiClient; local: HubLocal | null; error: string; downloads: HubDownload[]; onChange(): Promise<unknown> }) {
  const { client, local } = props;
  const remove = useAction(
    async (repo: HubLocalRepo) => {
      await client.delete("/hub/local/{repo_id}", { params: { path: { repo_id: repo.id } } });
      await props.onChange();
    },
    { success: (_, repo) => `Deleted ${repo.id} from disk.`, failed: (repo) => `Could not delete ${repo.id}` },
  );

  const downloading = new Set(props.downloads.filter(isActive).map((d) => d.repo_id));
  const repos = local ? sortLocal(local.repos) : [];

  const columns: GridColumn<HubLocalRepo>[] = [
    {
      key: "id",
      label: "Model ID",
      headClassName: "pl-4",
      className: "pl-4",
      render: (r) => (
        <>
          <span className="font-mono text-[13px] font-medium [overflow-wrap:anywhere]">{r.id}</span>
          <div className="text-xs text-muted-foreground">{fmtInt(r.files)} files</div>
        </>
      ),
    },
    {
      key: "size",
      label: "Size",
      align: "right",
      headTitle: "Everything in the repo's cache folder; the weights alone underneath when other files take room",
      render: (r) => (
        <>
          {fmtBytes(r.size_bytes)}
          {r.weight_bytes !== r.size_bytes && <div className="text-xs text-muted-foreground">{fmtBytes(r.weight_bytes)} weights</div>}
        </>
      ),
    },
    { key: "used", label: "Last used", className: "whitespace-nowrap text-muted-foreground", render: (r) => fmtAgo(r.last_accessed) },
    {
      key: "loaded",
      label: "Loaded",
      render: (r) =>
        r.loaded ? (
          <span className="flex flex-wrap items-center gap-1.5">
            <Badge className="gap-1.5">
              <StatusDot color="var(--good)" className="shadow-none" />
              loaded
            </Badge>
            {r.loaded_tasks.length > 0 && <span className="font-mono text-xs text-muted-foreground">{r.loaded_tasks.join(", ")}</span>}
          </span>
        ) : (
          <span className="text-muted-foreground">–</span>
        ),
    },
    {
      key: "tasks",
      label: "Claimed tasks",
      render: (r) =>
        r.tasks.length ? (
          <span className="flex flex-wrap gap-1">
            {r.tasks.map((t) => (
              <Badge key={t} variant="secondary" className="font-mono text-[11px] font-normal">
                {t}
              </Badge>
            ))}
          </span>
        ) : (
          <span className="text-muted-foreground">–</span>
        ),
    },
    {
      key: "delete",
      headClassName: "w-0 pr-4",
      className: "pr-4 text-right",
      render: (r) => (
        <ConfirmButton
          title={`Delete ${r.id} from disk?`}
          body={`Its ${fmtInt(r.files)} files (${fmtBytes(r.size_bytes)}) are removed; the next request for it downloads them again. This cannot be undone.`}
          hint={r.loaded ? "Unload it first" : downloading.has(r.id) ? "It is being downloaded" : undefined}
          busy={remove.running?.[0].id === r.id}
          onConfirm={() => remove.run(r)}
        />
      ),
    },
  ];
  return (
    <LoadedCard
      title="On disk"
      description={(l) => `${l.repos.length} repos, ${fmtBytes(l.total_bytes)}`}
      data={local}
      error={props.error}
      onRefresh={props.onChange}
      isEmpty={() => repos.length === 0}
      empty="Nothing downloaded yet."
    >
      {() => (
        <div className="overflow-hidden rounded-lg border">
          <DataGrid columns={columns} rows={repos} rowKey={(r) => r.id} headerClassName="bg-muted/50" empty="Nothing downloaded yet" />
        </div>
      )}
    </LoadedCard>
  );
}
