import { useState } from "react";
import { useApi } from "../../shared/api";
import { errorText, poll, useReloadOn } from "../../shared/query";
import type { Job } from "../../shared/types";
import { useCursorPages } from "../layout";
import { JOBS_LIMIT } from "./jobs";
import { LogDialog } from "./JobsLog";
import { SchedulesCard } from "./JobsSchedules";
import { JobsCard } from "./JobsTable";
import { WebhooksCard } from "./JobsWebhooks";
import { POLL_IDLE_MS, pollInterval } from "../../shared/tasks";

/**
 * /admin#/jobs: the schedules that run jobs on a cron or every so often, the
 * jobs themselves (submit, watch, cancel, read the log), and the webhooks
 * told when one finishes.
 */
export function JobsSection() {
  const client = useApi();
  // The webhooks list their latest deliveries, which a retry changes long after the job ended: polled slowly.
  const webhooks = client.useQuery("get", "/queue/webhooks", undefined, poll(POLL_IDLE_MS));
  const types = client.useQuery("get", "/jobs/types", undefined, { select: (d) => d.jobs });
  const typesError = errorText(types.error);
  // Paged by each page's next (undefined: the newest page).
  const pages = useCursorPages(null);
  // Polled fast while a job is queued or running.
  const jobs = client.useQuery("get", "/jobs", { params: { query: { limit: JOBS_LIMIT, cursor: pages.cursor } } }, {
    ...poll((d?: { jobs: Job[] }) => pollInterval(d?.jobs ?? null)),
  });
  // A schedule's next and last run change as jobs run, so both lists are polled together, and the
  // schedules are loaded again at once when the pace changes (a job started, or the last one ended).
  const interval = pollInterval(jobs.data?.jobs ?? null);
  const schedules = client.useQuery("get", "/jobs/schedules", undefined, { select: (d) => d.schedules, ...poll(interval) });
  useReloadOn(interval, schedules.refetch);
  // A job that ended has its deliveries queued.
  useReloadOn(interval, webhooks.refetch);
  // The job whose log is open.
  const [logId, setLogId] = useState<string | null>(null);

  return (
    <section className="grid min-w-0 gap-5">
      <SchedulesCard
        schedules={schedules.data ?? null}
        error={errorText(schedules.error)}
        types={types.data ?? null}
        typesError={typesError}
        onChange={schedules.refetch}
        onJobStarted={jobs.refetch}
        onOpenLog={setLogId}
      />
      <JobsCard
        jobs={jobs.data?.jobs ?? null}
        next={jobs.data?.next ?? undefined}
        error={errorText(jobs.error)}
        pages={pages}
        loading={jobs.isFetching}
        schedules={schedules.data ?? []}
        types={types.data ?? null}
        typesError={typesError}
        onChange={jobs.refetch}
        onOpenLog={setLogId}
      />
      <WebhooksCard webhooks={webhooks.data ?? null} error={errorText(webhooks.error)} />
      {logId && <LogDialog key={logId} id={logId} job={jobs.data?.jobs.find((j) => j.id === logId)} onClose={() => setLogId(null)} />}
    </section>
  );
}
