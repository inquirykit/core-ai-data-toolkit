import { useQuery, useQueryClient } from "@tanstack/react-query";
import type { ApiClient } from "../../shared/api";
import { errorText, poll } from "../../shared/query";
import { isActive, mergeDownloads, newlyFinished, type DownloadView } from "./hub";
import { DownloadsCard } from "./HubDownloads";
import { LocalCard } from "./HubLocal";
import { SearchCard } from "./HubSearch";

/** Downloads are polled fast while one moves, slowly otherwise (the list rarely changes by itself). */
const POLL_ACTIVE_MS = 1500;
const POLL_IDLE_MS = 10000;

/**
 * /admin#/hub: search the Hugging Face Hub and download a model's weights
 * before a request needs them, watch the downloads, and see (and free) what
 * is on disk.
 */
export function HubSection({ client }: { client: ApiClient }) {
  const local = client.useQuery("get", "/hub/local");
  const queries = useQueryClient();
  // Each poll's rows keep the speed worked out from the previous one's (what the cache holds).
  const downloads = useQuery({
    queryKey: ["hub", "downloads"],
    queryFn: async ({ signal, queryKey }) => {
      const previous = queries.getQueryData<DownloadView[]>(queryKey);
      const { downloads: rows } = await client.get("/hub/downloads", { signal });
      // A download that just finished changed what is on disk.
      if (previous && newlyFinished(previous.map((v) => v.row), rows)) void local.refetch();
      return mergeDownloads(previous ?? [], rows, Date.now());
    },
    ...poll((views?: DownloadView[]) => (views?.some((v) => isActive(v.row)) ? POLL_ACTIVE_MS : POLL_IDLE_MS)),
  });

  const rows = downloads.data?.map((v) => v.row) ?? [];
  const localIds = new Set(local.data?.repos.map((r) => r.id) ?? []);

  return (
    <section className="grid min-w-0 gap-5">
      <SearchCard client={client} downloads={rows} localIds={localIds} onQueued={downloads.refetch} />
      <DownloadsCard client={client} downloads={downloads.data ?? null} error={errorText(downloads.error)} onChange={downloads.refetch} />
      <LocalCard client={client} local={local.data ?? null} error={errorText(local.error)} downloads={rows} onChange={local.refetch} />
    </section>
  );
}
