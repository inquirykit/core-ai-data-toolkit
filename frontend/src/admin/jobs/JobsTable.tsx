// The Jobs section's list of recent jobs: status, progress and timing, to
// cancel, delete or open the log of. StatusBadge, a job's status as a badge,
// is shared with the schedules and the log.

import { useState } from "react";
import { Play, ScrollText, X } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Progress } from "@/components/ui/progress";
import { cn } from "@/lib/utils";
import { useAction } from "../../shared/action";
import type { ApiClient } from "../../shared/api";
import { ConfirmButton, StatusLabel, SubmitButton } from "../../shared/components";
import { fmtAgo, fmtClock } from "../../shared/format";
import { DataGrid, type GridColumn } from "../../shared/grid";
import type { Job, JobType, Schedule } from "../../shared/types";
import { fmtRetries, fmtSeconds, fmtUntil, isActive, jobDuration, jobStatus, progressValue, scheduleName, sortJobs } from "./jobs";
import { SubmitDialog } from "./JobsSubmit";
import { LoadedCard } from "../layout";

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

  const cancel = useAction(
    async (j: Job) => {
      await client.post("/jobs/{id}/cancel", { params: { path: { id: j.id } } });
      await props.onChange();
    },
    { failed: (j) => `Could not cancel ${j.job}` },
  );
  const remove = useAction(
    async (j: Job) => {
      await client.delete("/jobs/{id}", { params: { path: { id: j.id } } });
      await props.onChange();
    },
    { success: (_, j) => `Deleted job ${j.id}.`, failed: (j) => `Could not delete job ${j.id}` },
  );

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
          {fmtRetries(j) && (
            <Badge variant="outline" className="ml-2 align-middle text-[11px]" title={`The first retry waits ${j.retry_delay_s} s, each next one twice as long`}>
              {fmtRetries(j)}
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
      render: (j) => (j.started ? fmtAgo(j.started) : j.status === "queued" ? (j.not_before && j.not_before > now ? `retry ${fmtUntil(j.not_before, now)}` : "waiting") : "–"),
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
            <SubmitButton
              type="button"
              variant="outline"
              size="sm"
              className="text-destructive hover:text-destructive"
              busy={cancel.running?.[0].id === j.id}
              icon={<X aria-hidden="true" />}
              onClick={() => cancel.run(j)}
            >
              Cancel
            </SubmitButton>
          ) : (
            <ConfirmButton
              title={`Delete job ${j.id}?`}
              body="Its log, result and output files are removed. This cannot be undone."
              busy={remove.running?.[0].id === j.id}
              onConfirm={() => remove.run(j)}
            />
          )}
        </span>
      ),
    },
  ];

  return (
    <>
      <LoadedCard
        title="Jobs"
        description={(list) => (active ? `${list.length} most recent · ${active} queued or running` : `${list.length} most recent`)}
        data={jobs}
        error={props.error}
        onRefresh={props.onChange}
        actions={
          <Button type="button" size="sm" onClick={() => setSubmitting(true)}>
            <Play aria-hidden="true" />
            Submit job
          </Button>
        }
        isEmpty={() => rows.length === 0}
        empty="No jobs yet. Submit one, or let a schedule above start it."
      >
        {() => (
          <div className="overflow-x-auto rounded-lg border">
            <DataGrid columns={columns} rows={rows} rowKey={(j) => j.id} headerClassName="bg-muted/50" empty="No jobs yet" />
          </div>
        )}
      </LoadedCard>
      {submitting && <SubmitDialog client={client} types={props.types} typesError={props.typesError} onClose={() => setSubmitting(false)} onSubmitted={props.onChange} />}
    </>
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
