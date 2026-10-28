// The Jobs section's list of recent jobs: status, progress and timing, to
// cancel, delete or open the log of. StatusBadge, a job's status as a badge,
// is shared with the schedules and the log.

import { useState } from "react";
import { Play, RotateCw, ScrollText, X } from "lucide-react";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Progress } from "@/components/ui/progress";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";
import type { ApiClient } from "../shared/api";
import { failed } from "../shared/errors";
import { ConfirmButton, Notice, StatusLabel } from "../shared/components";
import { fmtAgo, fmtClock } from "../shared/format";
import { DataGrid, type GridColumn } from "../shared/grid";
import type { Job, JobType, Schedule } from "../shared/types";
import { fmtSeconds, isActive, jobDuration, jobStatus, progressValue, scheduleName, sortJobs } from "./jobs";
import { SubmitDialog } from "./JobsSubmit";

export function JobsCard(props: {
  client: ApiClient;
  jobs: Job[] | null;
  error: string;
  schedules: Schedule[];
  types: JobType[] | null;
  typesError: string;
  onChange(): Promise<unknown>;
  onOpenLog(jobId: string): void;
}) {
  const { client, jobs } = props;
  const [submitting, setSubmitting] = useState(false);

  const cancel = (j: Job) => client.post("/jobs/{id}/cancel", { params: { path: { id: j.id } } }).then(() => props.onChange(), failed(`Could not cancel ${j.job}`));
  const remove = (j: Job) =>
    client.delete("/jobs/{id}", { params: { path: { id: j.id } } }).then(() => {
      toast.success(`Deleted job ${j.id}.`);
      return props.onChange();
    }, failed(`Could not delete job ${j.id}`));

  const rows = jobs ? sortJobs(jobs) : [];
  const active = rows.filter(isActive).length;
  const now = Date.now() / 1000;

  const columns: GridColumn<Job>[] = [
    {
      key: "job",
      label: "Job",
      headClassName: "pl-4",
      className: "pl-4",
      render: (j) => (
        <>
          <span className="font-mono text-[13px] font-medium">{j.job}</span>
          {j.priority !== "normal" && (
            <Badge variant={j.priority === "high" ? "secondary" : "outline"} className="ml-2 align-middle text-[11px]">
              {j.priority} priority
            </Badge>
          )}
          <div className="font-mono text-xs text-muted-foreground">{j.id}</div>
        </>
      ),
    },
    { key: "status", label: "Status", render: (j) => <StatusBadge status={j.status} /> },
    { key: "progress", label: "Progress", headClassName: "min-w-44", render: (j) => <JobProgress job={j} /> },
    {
      key: "started",
      label: "Started",
      className: "whitespace-nowrap text-muted-foreground",
      title: (j) => (j.started ? fmtClock(j.started, true) : undefined),
      render: (j) => (j.started ? fmtAgo(j.started) : j.status === "queued" ? "waiting" : "–"),
    },
    {
      key: "finished",
      label: "Finished",
      className: "whitespace-nowrap text-muted-foreground",
      title: (j) => (j.finished ? fmtClock(j.finished, true) : undefined),
      render: (j) => fmtAgo(j.finished),
    },
    {
      key: "duration",
      label: "Duration",
      className: "whitespace-nowrap tabular-nums",
      render: (j) => {
        const duration = jobDuration(j, now);
        return duration === null ? <span className="text-muted-foreground">–</span> : fmtSeconds(duration);
      },
    },
    { key: "schedule", label: "Schedule", className: "[overflow-wrap:anywhere]", render: (j) => scheduleName(j, props.schedules) ?? <span className="text-muted-foreground">–</span> },
    {
      key: "actions",
      headClassName: "w-0 pr-4",
      className: "pr-4",
      render: (j) => (
        <span className="flex items-center justify-end gap-2">
          <Button type="button" variant="outline" size="sm" onClick={() => props.onOpenLog(j.id)}>
            <ScrollText aria-hidden="true" />
            Log
          </Button>
          {isActive(j) ? (
            <Button type="button" variant="outline" size="sm" className="text-destructive hover:text-destructive" onClick={() => cancel(j)}>
              <X aria-hidden="true" />
              Cancel
            </Button>
          ) : (
            <ConfirmButton title={`Delete job ${j.id}?`} body="Its log, result and output files are removed. This cannot be undone." onConfirm={() => remove(j)} />
          )}
        </span>
      ),
    },
  ];

  return (
    <Card className="gap-4">
      <CardHeader>
        <CardTitle>Jobs</CardTitle>
        <CardDescription>{jobs ? (active ? `${jobs.length} most recent · ${active} queued or running` : `${jobs.length} most recent`) : "Loading…"}</CardDescription>
        <CardAction className="flex items-center gap-2">
          <Button type="button" variant="ghost" size="icon" className="size-8" aria-label="Refresh" onClick={props.onChange}>
            <RotateCw aria-hidden="true" />
          </Button>
          <Button type="button" size="sm" onClick={() => setSubmitting(true)}>
            <Play aria-hidden="true" />
            Submit job
          </Button>
        </CardAction>
      </CardHeader>
      <CardContent className="grid gap-3">
        {props.error && <Notice notice={{ text: props.error, kind: "error" }} />}
        {!jobs && !props.error && <Skeleton className="h-24" />}
        {jobs && rows.length === 0 && <p className="text-sm text-muted-foreground">No jobs yet. Submit one, or let a schedule above start it.</p>}
        {rows.length > 0 && (
          <div className="overflow-x-auto rounded-lg border">
            <DataGrid columns={columns} rows={rows} rowKey={(j) => j.id} headerClassName="bg-muted/50" empty="No jobs yet" />
          </div>
        )}
      </CardContent>

      {submitting && <SubmitDialog client={client} types={props.types} typesError={props.typesError} onClose={() => setSubmitting(false)} onSubmitted={props.onChange} />}
    </Card>
  );
}

export function StatusBadge({ status }: { status: string }) {
  return (
    <Badge variant="outline">
      <StatusLabel status={jobStatus(status)} live={status === "running"} className="gap-1.5" />
    </Badge>
  );
}

function JobProgress({ job }: { job: Job }) {
  const value = progressValue(job);
  return (
    <div className="grid gap-1.5">
      {value === null ? (
        // Running without a reported progress: a bar that breathes instead of filling.
        <div className="relative h-2 w-full overflow-hidden rounded-full bg-primary/15" role="progressbar" aria-label="Running, progress not reported">
          <div className="h-full w-full animate-pulse rounded-full bg-primary/50" />
        </div>
      ) : (
        <Progress
          value={value * 100}
          aria-label={`${Math.round(value * 100)}% done`}
          indicatorClassName={cn(job.status === "failed" && "bg-critical", job.status === "cancelled" && "bg-warning")}
        />
      )}
      {job.message && (
        <div className="max-w-64 truncate text-xs text-muted-foreground" title={job.message}>
          {job.message}
        </div>
      )}
      {job.error && (
        <div className="max-w-64 truncate text-xs text-critical-text" title={job.error}>
          {job.error}
        </div>
      )}
    </div>
  );
}
