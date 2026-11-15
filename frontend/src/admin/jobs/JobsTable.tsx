// The Jobs section's list of recent jobs: status, progress and timing, to
// cancel, delete or open the log of, and what a finished one made (its
// result and files, JobsResult.tsx) under its row.

import { useState } from "react";
import { ChevronDown, ChevronRight, Play, ScrollText, X } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Progress } from "@/components/ui/progress";
import { cn } from "@/lib/utils";
import { useAction } from "../../shared/action";
import { useApi } from "../../shared/api";
import { ConfirmButton, StatusBadge, SubmitButton } from "../../shared/components";
import { fmtAgo, fmtClock, fmtSeconds } from "../../shared/format";
import { DataGrid, type GridColumn } from "../../shared/grid";
import { isActive, progressValue } from "../../shared/tasks";
import type { Job, JobType, Schedule } from "../../shared/types";
import { fmtRetries, fmtUntil, jobDuration, JOBS_LIMIT, scheduleName, sortJobs } from "./jobs";
import { EnvNotice, JobResult } from "./JobsResult";
import { SubmitDialog } from "./JobsSubmit";
import { LoadedCard, Pager, type useCursorPages } from "../layout";

export function JobsCard(props: {
  jobs: Job[] | null;
  /** The next page's cursor, when there is one */
  next?: string;
  error: string;
  /** The page shown: the newest, or an older one */
  pages: ReturnType<typeof useCursorPages>;
  loading: boolean;
  schedules: Schedule[];
  types: JobType[] | null;
  typesError: string;
  onChange(): Promise<unknown>;
  onOpenLog(jobId: string): void;
}) {
  const client = useApi();
  const { jobs, pages } = props;
  const [submitting, setSubmitting] = useState(false);
  // The finished job whose result shows under its row.
  const [opened, setOpened] = useState<string | null>(null);

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
          {j.status === "succeeded" ? (
            // A finished job opens what it made under its row.
            <button
              type="button"
              className="-ml-1 inline-flex items-center gap-0.5 rounded font-mono text-[13px] font-medium hover:underline"
              aria-expanded={opened === j.id}
              title={opened === j.id ? "Hide its result" : "Show its result and files"}
              onClick={() => setOpened(opened === j.id ? null : j.id)}
            >
              {opened === j.id ? <ChevronDown className="size-3.5" aria-hidden="true" /> : <ChevronRight className="size-3.5" aria-hidden="true" />}
              {j.job}
            </button>
          ) : (
            <span className="font-mono text-[13px] font-medium">{j.job}</span>
          )}
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
        description={(list) => {
          const shown = pages.hasPrevious ? `${list.length} older` : `${list.length} most recent`;
          return active ? `${shown} · ${active} queued or running` : shown;
        }}
        data={jobs}
        error={props.error}
        onRefresh={props.onChange}
        actions={
          <Button type="button" size="sm" onClick={() => setSubmitting(true)}>
            <Play aria-hidden="true" />
            Submit job
          </Button>
        }
        top={<EnvNotice types={props.types} onQueued={props.onChange} />}
        isEmpty={() => rows.length === 0 && !pages.hasPrevious}
        empty="No jobs yet. Submit one, or let a schedule above start it."
        after={
          jobs &&
          (pages.hasPrevious || props.next) && (
            <Pager
              start={pages.index * JOBS_LIMIT}
              shown={jobs.length}
              count={null}
              hasPrevious={pages.hasPrevious}
              hasNext={Boolean(props.next)}
              loading={props.loading}
              onPrevious={pages.previous}
              onNext={() => pages.next(props.next)}
            />
          )
        }
      >
        {() => (
          <div className="overflow-x-auto rounded-lg border">
            <DataGrid
              columns={columns}
              rows={rows}
              rowKey={(j) => j.id}
              headerClassName="bg-muted/50"
              empty="No older jobs"
              detail={(j) => (opened === j.id && j.status === "succeeded" ? <JobResult job={j} /> : null)}
            />
          </div>
        )}
      </LoadedCard>
      {submitting && <SubmitDialog types={props.types} typesError={props.typesError} onClose={() => setSubmitting(false)} onSubmitted={props.onChange} />}
    </>
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
