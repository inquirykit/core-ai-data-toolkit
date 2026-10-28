import { useEffect, useState, type FormEvent } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Download, ExternalLink, LoaderCircle, RotateCw, Search, X } from "lucide-react";
import { toast } from "sonner";
import { useAction } from "../shared/action";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Progress } from "@/components/ui/progress";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { cn } from "@/lib/utils";
import type { ApiClient } from "../shared/api";
import { failed } from "../shared/errors";
import { ConfirmButton, Field, Notice, SelectField, StatusDot, StatusLabel, SubmitButton, type NoticeState } from "../shared/components";
import { fmtAgo, fmtBytes, fmtCompact, fmtInt, statusOf } from "../shared/format";
import { errorText, poll } from "../shared/query";
import type { HubDownload, HubLocal, HubLocalRepo, HubRepo, HubSearchResult } from "../shared/types";
import {
  DOWNLOAD_STATUS,
  downloadProgress,
  fmtSpeed,
  hubUrl,
  isActive,
  mergeDownloads,
  newlyFinished,
  searchAction,
  searchQuery,
  sortLocal,
  TASK_FILTERS,
  type DownloadView,
  type TaskFilter,
} from "./hub";

const SEARCH_DEBOUNCE_MS = 400;
/** Downloads are polled fast while one moves, slowly otherwise (the list rarely changes by itself). */
const POLL_ACTIVE_MS = 1500;
const POLL_IDLE_MS = 10000;
const GATED_HINT = "Needs a Hugging Face token (HF_TOKEN) and accepted terms";

/** An ISO 8601 time as "x ago": the Hub reports when a model changed that way, not in Unix seconds. */
const fmtAgoIso = (iso: string | null): string => (iso ? fmtAgo(Date.parse(iso) / 1000) : "–");

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

// ---------------------------------------------------------------- search
function SearchCard(props: { client: ApiClient; downloads: HubDownload[]; localIds: Set<string>; onQueued(): Promise<unknown> }) {
  const { client } = props;
  const [query, setQuery] = useState("");
  const [task, setTask] = useState<TaskFilter>("");
  // The repo's details, looked up before its confirmation, then the confirmation itself.
  const [pending, setPending] = useState<HubRepo | null>(null);

  const ask = useAction(async (id: string) => setPending(await client.get("/hub/repos/{repo_id}", { params: { path: { repo_id: id } } })), { notice: true });
  const confirm = useAction(
    async () => {
      if (!pending) return;
      const id = pending.id;
      setPending(null);
      await client.post("/hub/downloads", { body: { repo_id: id } });
      await props.onQueued();
      return id;
    },
    { notice: true, success: (id) => id && { text: `Downloading ${id}.`, kind: "ok" } },
  );

  // Search as the user types, a little after the last keystroke; no words, no results. Search goes at once.
  const [searched, setSearched] = useState<{ query: string; task: TaskFilter }>({ query: "", task: "" });
  useEffect(() => {
    const timer = setTimeout(() => setSearched({ query, task }), SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [query, task]);
  const searching = searched.query.trim() !== "";
  const results = client.useQuery("get", "/hub/search", { params: { query: searchQuery(searched.query, searched.task) } }, { enabled: searching });
  // A search replaces what the last lookup or download said.
  const { clear: clearAsk } = ask;
  const { clear: clearConfirm } = confirm;
  useEffect(() => {
    clearAsk();
    clearConfirm();
  }, [searched, clearAsk, clearConfirm]);
  const reply = searching ? results.data : null;
  const busy = results.isFetching && searching;
  // A 503 means the server runs offline (HF_HUB_OFFLINE); the detail says so.
  const notice = ask.notice.text ? ask.notice : confirm.notice;
  const resultsError = searching ? errorText(results.error) : "";
  const shown: NoticeState = notice.text || !resultsError ? notice : { text: resultsError, kind: "error" };

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (query === searched.query && task === searched.task) void results.refetch();
    else setSearched({ query, task });
  };

  return (
    <Card className="gap-4">
      <CardHeader>
        <CardTitle>Search Hugging Face</CardTitle>
        <CardDescription>Find a model on the Hub and fetch its weights now, so the first request does not wait for them.</CardDescription>
      </CardHeader>
      <CardContent className="grid gap-3">
        <form className="flex flex-wrap items-end gap-2" onSubmit={submit}>
          <Field label="Model" className="min-w-64 flex-1">
            <Input
              type="search"
              placeholder="Qwen2.5, bert-base, whisper…"
              autoComplete="off"
              spellCheck={false}
              value={query}
              onChange={(e) => setQuery(e.target.value)}
            />
          </Field>
          <SelectField
            label="Task"
            triggerClassName="w-48"
            aria-label="Task"
            value={task}
            onChange={(v) => setTask(v as TaskFilter)}
            none="All tasks"
            options={TASK_FILTERS.map((t) => ({ value: t, label: t, className: "font-mono text-xs" }))}
          />
          <SubmitButton busy={busy} icon={<Search aria-hidden="true" />} disabled={!query.trim()}>
            Search
          </SubmitButton>
        </form>
        <Notice notice={shown} />
        {reply && reply.results.length === 0 && <p className="text-sm text-muted-foreground">Nothing on the Hub matches "{reply.query}".</p>}
        {reply && reply.results.length > 0 && (
          <div className="overflow-hidden rounded-lg border">
            <Table>
              <TableHeader className="bg-muted/50">
                <TableRow className="hover:bg-transparent">
                  <TableHead className="pl-4">Model ID</TableHead>
                  <TableHead>Task</TableHead>
                  <TableHead className="text-right">Downloads</TableHead>
                  <TableHead className="text-right">Likes</TableHead>
                  <TableHead>Updated</TableHead>
                  <TableHead className="w-0 pr-4" />
                </TableRow>
              </TableHeader>
              <TableBody>
                {reply.results.map((r) => (
                  <SearchRow key={r.id} result={r} action={searchAction(r, props.downloads, props.localIds)} busy={ask.running?.[0] === r.id} onDownload={() => ask.run(r.id)} />
                ))}
              </TableBody>
            </Table>
          </div>
        )}
      </CardContent>

      <AlertDialog open={pending !== null} onOpenChange={(open) => !open && setPending(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle className="[overflow-wrap:anywhere]">Download {pending?.id}?</AlertDialogTitle>
            <AlertDialogDescription>
              {pending && (
                <>
                  {fmtInt(pending.files.length)} files, {fmtBytes(pending.total_bytes)} ({fmtBytes(pending.weight_bytes)} of weights)
                  {pending.license && ` · ${pending.license}`}. It downloads in the background; the first request for it will not have to.
                </>
              )}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction onClick={confirm.run}>
              <Download aria-hidden="true" />
              Download
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </Card>
  );
}

function SearchRow(props: { result: HubSearchResult; action: ReturnType<typeof searchAction>; busy: boolean; onDownload(): void }) {
  const { result: r, action } = props;
  return (
    <TableRow>
      <TableCell className="pl-4">
        <span className="flex items-center gap-1.5">
          <span className="font-mono text-[13px] font-medium [overflow-wrap:anywhere]">{r.id}</span>
          <a
            href={hubUrl(r.id)}
            target="_blank"
            rel="noopener noreferrer"
            aria-label={`${r.id} on Hugging Face`}
            className="inline-flex shrink-0 rounded text-muted-foreground outline-none hover:text-foreground focus-visible:ring-[3px] focus-visible:ring-ring/50"
          >
            <ExternalLink className="size-3.5" aria-hidden="true" />
          </a>
        </span>
        {r.library_name && <div className="text-xs text-muted-foreground">{r.library_name}</div>}
      </TableCell>
      <TableCell>
        {r.pipeline_tag ? (
          <Badge variant="secondary" className="font-mono font-normal">
            {r.pipeline_tag}
          </Badge>
        ) : (
          <span className="text-muted-foreground">–</span>
        )}
      </TableCell>
      <TableCell className="text-right tabular-nums">{fmtCompact(r.downloads)}</TableCell>
      <TableCell className="text-right tabular-nums">{fmtCompact(r.likes)}</TableCell>
      <TableCell className="whitespace-nowrap text-muted-foreground">{fmtAgoIso(r.last_modified)}</TableCell>
      <TableCell className="pr-4 text-right">
        {action === "on_disk" && <Badge variant="outline">On disk</Badge>}
        {action === "downloading" && (
          <Badge variant="outline" className="gap-1.5">
            <LoaderCircle className="animate-spin" aria-hidden="true" />
            Downloading…
          </Badge>
        )}
        {action === "gated" && (
          // A disabled button takes no pointer events, so the hint goes on its wrapper.
          <span title={GATED_HINT} className="inline-flex">
            <Button type="button" variant="outline" size="sm" disabled>
              Gated
            </Button>
          </span>
        )}
        {action === "download" && (
          <SubmitButton type="button" variant="outline" size="sm" busy={props.busy} icon={<Download aria-hidden="true" />} onClick={props.onDownload}>
            Download
          </SubmitButton>
        )}
      </TableCell>
    </TableRow>
  );
}

// ------------------------------------------------------------- downloads
function DownloadsCard(props: { client: ApiClient; downloads: DownloadView[] | null; error: string; onChange(): Promise<unknown> }) {
  const { client, downloads } = props;
  const remove = (d: HubDownload) =>
    client
      .delete("/hub/downloads/{download_id}", { params: { path: { download_id: d.id } } })
      .then(() => props.onChange(), failed(isActive(d) ? `Could not cancel ${d.repo_id}` : `Could not dismiss ${d.repo_id}`));

  const moving = downloads?.filter((v) => isActive(v.row)).length ?? 0;
  return (
    <Card className="gap-4">
      <CardHeader>
        <CardTitle>Downloads</CardTitle>
        <CardDescription>{downloads ? (moving ? `${downloads.length} · ${moving} in progress` : `${downloads.length}`) : "Loading…"}</CardDescription>
        <CardAction>
          <Button type="button" variant="ghost" size="icon" className="size-8" aria-label="Refresh" onClick={props.onChange}>
            <RotateCw aria-hidden="true" />
          </Button>
        </CardAction>
      </CardHeader>
      <CardContent className="grid gap-3">
        {props.error && <Notice notice={{ text: props.error, kind: "error" }} />}
        {!downloads && !props.error && <Skeleton className="h-16" />}
        {downloads && downloads.length === 0 && (
          <p className="text-sm text-muted-foreground">No downloads yet. Search above and download a model so the first request does not have to.</p>
        )}
        {downloads && downloads.length > 0 && (
          <ul className="grid gap-2">
            {downloads.map((v) => (
              <DownloadRow key={v.row.id} view={v} onRemove={() => remove(v.row)} />
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}

function DownloadRow({ view, onRemove }: { view: DownloadView; onRemove(): void }) {
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
        <Button type="button" variant="outline" size="sm" className={cn(live && "text-destructive hover:text-destructive")} onClick={onRemove}>
          <X aria-hidden="true" />
          {live ? "Cancel" : "Dismiss"}
        </Button>
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

// ----------------------------------------------------------------- local
function LocalCard(props: { client: ApiClient; local: HubLocal | null; error: string; downloads: HubDownload[]; onChange(): Promise<unknown> }) {
  const { client, local } = props;
  const remove = (repo: HubLocalRepo) =>
    client
      .delete("/hub/local/{repo_id}", { params: { path: { repo_id: repo.id } } })
      .then(() => {
        toast.success(`Deleted ${repo.id} from disk.`);
        return props.onChange();
      }, failed(`Could not delete ${repo.id}`));

  const downloading = new Set(props.downloads.filter(isActive).map((d) => d.repo_id));
  const repos = local ? sortLocal(local.repos) : [];
  return (
    <Card className="gap-4">
      <CardHeader>
        <CardTitle>On disk</CardTitle>
        <CardDescription>{local ? `${local.repos.length} repos, ${fmtBytes(local.total_bytes)}` : "Loading…"}</CardDescription>
        <CardAction>
          <Button type="button" variant="ghost" size="icon" className="size-8" aria-label="Refresh" onClick={props.onChange}>
            <RotateCw aria-hidden="true" />
          </Button>
        </CardAction>
      </CardHeader>
      <CardContent className="grid gap-3">
        {props.error && <Notice notice={{ text: props.error, kind: "error" }} />}
        {!local && !props.error && <Skeleton className="h-24" />}
        {local && repos.length === 0 && <p className="text-sm text-muted-foreground">Nothing downloaded yet.</p>}
        {repos.length > 0 && (
          <div className="overflow-hidden rounded-lg border">
            <Table>
              <TableHeader className="bg-muted/50">
                <TableRow className="hover:bg-transparent">
                  <TableHead className="pl-4">Model ID</TableHead>
                  <TableHead className="text-right" title="Everything in the repo's cache folder; the weights alone underneath when other files take room">
                    Size
                  </TableHead>
                  <TableHead>Last used</TableHead>
                  <TableHead>Loaded</TableHead>
                  <TableHead>Claimed tasks</TableHead>
                  <TableHead className="w-0 pr-4" />
                </TableRow>
              </TableHeader>
              <TableBody>
                {repos.map((r) => (
                  <TableRow key={r.id}>
                    <TableCell className="pl-4">
                      <span className="font-mono text-[13px] font-medium [overflow-wrap:anywhere]">{r.id}</span>
                      <div className="text-xs text-muted-foreground">{fmtInt(r.files)} files</div>
                    </TableCell>
                    <TableCell className="text-right tabular-nums">
                      {fmtBytes(r.size_bytes)}
                      {r.weight_bytes !== r.size_bytes && <div className="text-xs text-muted-foreground">{fmtBytes(r.weight_bytes)} weights</div>}
                    </TableCell>
                    <TableCell className="whitespace-nowrap text-muted-foreground">{fmtAgo(r.last_accessed)}</TableCell>
                    <TableCell>
                      {r.loaded ? (
                        <span className="flex flex-wrap items-center gap-1.5">
                          <Badge className="gap-1.5">
                            <StatusDot color="var(--good)" className="shadow-none" />
                            loaded
                          </Badge>
                          {r.loaded_tasks.length > 0 && <span className="font-mono text-xs text-muted-foreground">{r.loaded_tasks.join(", ")}</span>}
                        </span>
                      ) : (
                        <span className="text-muted-foreground">–</span>
                      )}
                    </TableCell>
                    <TableCell>
                      {r.tasks.length ? (
                        <span className="flex flex-wrap gap-1">
                          {r.tasks.map((t) => (
                            <Badge key={t} variant="secondary" className="font-mono text-[11px] font-normal">
                              {t}
                            </Badge>
                          ))}
                        </span>
                      ) : (
                        <span className="text-muted-foreground">–</span>
                      )}
                    </TableCell>
                    <TableCell className="pr-4 text-right">
                      <ConfirmButton
                        title={`Delete ${r.id} from disk?`}
                        body={`Its ${fmtInt(r.files)} files (${fmtBytes(r.size_bytes)}) are removed; the next request for it downloads them again. This cannot be undone.`}
                        hint={r.loaded ? "Unload it first" : downloading.has(r.id) ? "It is being downloaded" : undefined}
                        onConfirm={() => remove(r)}
                      />
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

