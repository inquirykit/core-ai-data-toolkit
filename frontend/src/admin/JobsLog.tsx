// The dialog with a job's log, opened from a row of the jobs list or from a
// schedule's last run.

import { useEffect, useRef } from "react";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Skeleton } from "@/components/ui/skeleton";
import type { ApiClient } from "../shared/api";
import { Notice } from "../shared/components";
import { errorText, poll, useReloadOn } from "../shared/query";
import type { Job } from "../shared/types";
import { isActive } from "./jobs";
import { StatusBadge } from "./JobsTable";

/** A running job's log is fetched again this often while its dialog is open. */
const LOG_POLL_MS = 2000;

/** A job's log, fetched again every few seconds while it runs. ``job`` is the row from the list, when it is in it. */
export function LogDialog(props: { client: ApiClient; id: string; job: Job | undefined; onClose(): void }) {
  const { client, id } = props;
  // The row fetched on its own, for a job older than the list shows (a schedule's last run).
  const fetched = client.useQuery("get", "/jobs/{id}", { params: { path: { id } } }, { enabled: !props.job });
  const pre = useRef<HTMLPreElement>(null);
  const job = props.job ?? fetched.data;
  const live = job ? isActive(job) : true;

  // Plain text, not JSON; polled while the job runs, and read once more when it ends.
  const log = client.useQuery("get", "/jobs/{id}/log", { params: { path: { id } }, parseAs: "text" }, poll(live ? LOG_POLL_MS : 0));
  useReloadOn(live, log.refetch);
  const text = log.data ?? null;
  const error = errorText(fetched.error) || errorText(log.error);

  // Follow the end of the log while it grows.
  useEffect(() => {
    if (live && pre.current) pre.current.scrollTop = pre.current.scrollHeight;
  }, [text, live]);

  return (
    <Dialog open onOpenChange={(open) => !open && props.onClose()}>
      <DialogContent className="max-h-[90svh] sm:max-w-4xl">
        <DialogHeader>
          <DialogTitle className="flex flex-wrap items-center gap-2">
            Log of {job?.job ?? "job"}
            {job && <StatusBadge status={job.status} />}
          </DialogTitle>
          <DialogDescription className="font-mono text-xs">
            {id}
            {job?.message && ` · ${job.message}`}
            {live && " · refreshing"}
          </DialogDescription>
        </DialogHeader>
        {error && <Notice notice={{ text: error, kind: "error" }} />}
        {text === null && !error && <Skeleton className="h-40" />}
        {text !== null && (
          <pre ref={pre} className="max-h-[60svh] overflow-auto rounded-md border bg-muted/40 p-3 font-mono text-xs leading-relaxed whitespace-pre-wrap [overflow-wrap:anywhere]">
            {text || <span className="text-muted-foreground">{live ? "Nothing printed yet." : "The job printed nothing."}</span>}
          </pre>
        )}
        {job?.error && <p className="text-sm text-critical-text [overflow-wrap:anywhere]">{job.error}</p>}
      </DialogContent>
    </Dialog>
  );
}
