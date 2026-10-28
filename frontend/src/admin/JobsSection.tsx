import { useState } from "react";
import { Info } from "lucide-react";
import { Alert, AlertDescription } from "@/components/ui/alert";
import type { ApiClient } from "../shared/api";
import { errorText, poll, useReloadOn } from "../shared/query";
import type { Job } from "../shared/types";
import { JOBS_LIMIT, jobsOff, pollInterval } from "./jobs";
import { LogDialog } from "./JobsLog";
import { SchedulesCard } from "./JobsSchedules";
import { JobsCard } from "./JobsTable";
import { WebhooksCard } from "./JobsWebhooks";

/**
 * /admin#/jobs: the schedules that run jobs on a cron or every so often, the
 * jobs themselves (submit, watch, cancel, read the log), and the webhooks
 * told when one finishes.
 */
export function JobsSection({ client }: { client: ApiClient }) {
  const webhooks = client.useQuery("get", "/jobs/webhooks");
  const types = client.useQuery("get", "/jobs/types", undefined, { select: (d) => d.jobs });
  const typesError = errorText(types.error);
  // The server's detail when jobs are off (JOBS_DIR or ARCADEDB_PATH empty): then nothing else is shown. It comes
  // from the job types, which need the runner; the webhooks answer with jobs off too (eval runs post to them).
  const off = jobsOff(typesError) ? typesError : null;
  // Polled fast while a job is queued or running.
  const jobs = client.useQuery("get", "/jobs", { params: { query: { limit: JOBS_LIMIT } } }, {
    select: (d) => d.jobs,
    ...poll((d?: { jobs: Job[] }) => pollInterval(d?.jobs ?? null)),
    enabled: !off,
  });
  // A schedule's next and last run change as jobs run, so both lists are polled together, and the
  // schedules are loaded again at once when the pace changes (a job started, or the last one ended).
  const interval = pollInterval(jobs.data ?? null);
  const schedules = client.useQuery("get", "/jobs/schedules", undefined, { select: (d) => d.schedules, ...poll(interval), enabled: !off });
  useReloadOn(interval, schedules.refetch);
  // The job whose log is open.
  const [logId, setLogId] = useState<string | null>(null);

  if (off) {
    return (
      <Alert role="note">
        <Info aria-hidden="true" />
        <AlertDescription>
          <p>
            {off}. Set <code>JOBS_DIR</code> to a folder and <code>ARCADEDB_PATH</code> to the database, and restart to run jobs and schedules.
          </p>
        </AlertDescription>
      </Alert>
    );
  }

  return (
    <section className="grid min-w-0 gap-5">
      <SchedulesCard
        client={client}
        schedules={schedules.data ?? null}
        error={errorText(schedules.error)}
        types={types.data ?? null}
        typesError={typesError}
        onChange={schedules.refetch}
        onJobStarted={jobs.refetch}
        onOpenLog={setLogId}
      />
      <JobsCard
        client={client}
        jobs={jobs.data ?? null}
        error={errorText(jobs.error)}
        schedules={schedules.data ?? []}
        types={types.data ?? null}
        typesError={typesError}
        onChange={jobs.refetch}
        onOpenLog={setLogId}
      />
      <WebhooksCard client={client} webhooks={webhooks.data ?? null} error={errorText(webhooks.error)} />
      {logId && <LogDialog key={logId} client={client} id={logId} job={jobs.data?.find((j) => j.id === logId)} onClose={() => setLogId(null)} />}
    </section>
  );
}
