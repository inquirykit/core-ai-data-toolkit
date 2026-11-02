// Searching the Hub as you type, and downloading a result after its size is confirmed.

import { useEffect, useMemo, useState, type FormEvent } from "react";
import { Download, ExternalLink, LoaderCircle, Search } from "lucide-react";
import { useAction } from "../../shared/action";
import { AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle } from "@/components/ui/alert-dialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import type { ApiClient } from "../../shared/api";
import { useDebounced } from "../../shared/browser";
import { useTaskLabels } from "../../shared/categories";
import { Field, Notice, SelectField, SubmitButton, type NoticeState } from "../../shared/components";
import { fmtAgoIso, fmtBytes, fmtCompact, fmtInt } from "../../shared/format";
import { DataGrid, type GridColumn } from "../../shared/grid";
import { errorText } from "../../shared/query";
import type { HubDownload, HubRepo, HubSearchResult } from "../../shared/types";
import { hubUrl, searchAction, searchQuery, TASK_FILTERS, type TaskFilter } from "./hub";

const SEARCH_DEBOUNCE_MS = 400;

const GATED_HINT = "Needs a Hugging Face token (HF_TOKEN) and accepted terms";

// ---------------------------------------------------------------- search
export function SearchCard(props: { client: ApiClient; downloads: HubDownload[]; localIds: Set<string>; onQueued(): Promise<unknown> }) {
  const { client } = props;
  const taskLabel = useTaskLabels(client);
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

  // Search as the user types, a little after the last keystroke; no words, no results. Search (Enter) goes at once.
  const wanted = useMemo(() => ({ query, task }), [query, task]);
  const settled = useDebounced(wanted, SEARCH_DEBOUNCE_MS);
  const [rushed, setRushed] = useState<typeof wanted | null>(null);
  const searched = rushed === wanted ? rushed : settled;
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
    if (searched === wanted) void results.refetch();
    else setRushed(wanted);
  };

  const columns: GridColumn<HubSearchResult>[] = [
    {
      key: "id",
      label: "Model ID",
      headClassName: "pl-4",
      className: "pl-4",
      render: (r) => (
        <>
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
        </>
      ),
    },
    {
      key: "task",
      label: "Task",
      render: (r) =>
        r.pipeline_tag ? (
          <Badge variant="secondary" className="font-mono font-normal">
            {r.pipeline_tag}
          </Badge>
        ) : (
          <span className="text-muted-foreground">–</span>
        ),
    },
    { key: "downloads", label: "Downloads", align: "right", render: (r) => fmtCompact(r.downloads) },
    { key: "likes", label: "Likes", align: "right", render: (r) => fmtCompact(r.likes) },
    { key: "updated", label: "Updated", className: "whitespace-nowrap text-muted-foreground", render: (r) => fmtAgoIso(r.last_modified) },
    {
      key: "action",
      headClassName: "w-0 pr-4",
      className: "pr-4 text-right",
      render: (r) => <SearchAction action={searchAction(r, props.downloads, props.localIds)} busy={ask.running?.[0] === r.id} onDownload={() => ask.run(r.id)} />,
    },
  ];

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
            options={TASK_FILTERS.map((t) => ({ value: t, label: taskLabel(t) }))}
          />
          <SubmitButton busy={busy} icon={<Search aria-hidden="true" />} disabled={!query.trim()}>
            Search
          </SubmitButton>
        </form>
        <Notice notice={shown} />
        {reply && reply.results.length === 0 && <p className="text-sm text-muted-foreground">Nothing on the Hub matches "{reply.query}".</p>}
        {reply && reply.results.length > 0 && (
          <div className="overflow-hidden rounded-lg border">
            <DataGrid columns={columns} rows={reply.results} rowKey={(r) => r.id} headerClassName="bg-muted/50" empty="Nothing on the Hub matches" />
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
/** A search result's last cell: what can be done with it, by searchAction. */
function SearchAction(props: { action: ReturnType<typeof searchAction>; busy: boolean; onDownload(): void }) {
  const { action } = props;
  return (
    <>
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
    </>
  );
}
