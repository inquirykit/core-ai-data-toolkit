// /admin#/autogluon: AutoGluon's predictors, trained from a table here and
// served as autogluon:<job id>. Until AutoGluon's environment is installed
// the section is one card with a Set up button (EnvSetup); then the list of
// predictors, a page to train a new one (AutoGluonNew.tsx), and each
// predictor's page (AutoGluonPredictor.tsx). Predictors are the training
// jobs, so they are listed from GET /jobs, filtered by job.

import { Plus, Sparkles } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { useAction } from "../../shared/action";
import { useApi } from "../../shared/api";
import { useHash } from "../../shared/browser";
import { CodeBlock, EmptyCard, Notice, SubmitButton, StatusBadge } from "../../shared/components";
import { fmtAgo, fmtInt } from "../../shared/format";
import { AUTOGLUON_PREFIX } from "../../shared/model-browser/catalog";
import { DataGrid, type GridColumn } from "../../shared/grid";
import { errorText, poll, useReloadOn } from "../../shared/query";
import type { Job } from "../../shared/types";
import { DetailSkeleton, LoadedCard, Pager, useCursorPages } from "../layout";
import { autogluonFromHash, autogluonHref, ENV, headlineScore, KIND_LABEL, newHref, predictorOf, scoreText, SETUP_JOB, TRAIN_JOB, type Predictor } from "./autogluon";
import { NewPredictor } from "./AutoGluonNew";
import { PredictorPage } from "./AutoGluonPredictor";
import { isActive, pollInterval } from "../../shared/tasks";

export function AutoGluonSection() {
  const client = useApi();
  const loc = useHash(autogluonFromHash);
  const types = client.useQuery("get", "/jobs/types", undefined, { select: (d) => d.jobs });
  const tabular = types.data?.find((t) => t.name === TRAIN_JOB.tabular);

  if (types.error) return <Notice notice={{ text: errorText(types.error), kind: "error" }} />;
  if (!types.data) return <DetailSkeleton />;
  if (!tabular) return <EmptyCard icon={Sparkles} title="No AutoGluon jobs" body="This service has no autogluon-* jobs registered (jobs/autogluon/)." />;
  if (!tabular.env_ready) return <EnvSetup update={false} onReady={types.refetch} />;
  return (
    <section className="grid min-w-0 gap-5">
      {!tabular.env_current && <EnvSetup update onReady={types.refetch} />}
      {loc.page === "list" && <PredictorsCard />}
      {loc.page === "new" && <NewPredictor key={loc.from ?? "new"} from={loc.from} types={types.data} />}
      {loc.page === "predictor" && <PredictorPage key={loc.id} id={loc.id} />}
    </section>
  );
}

// ------------------------------------------------------------------ setup
/** How often the install is looked at while it runs. */
const SETUP_POLL_MS = 2000;

/**
 * AutoGluon's environment, installed (or updated) by the setup-env job: a button that queues it, then the job's
 * progress and log until it ends. Nothing installs unless the button is clicked.
 */
function EnvSetup(props: { update: boolean; onReady(): void }) {
  const client = useApi();
  const latest = client.useQuery("get", "/jobs", { params: { query: { job: [SETUP_JOB], limit: 5 } } }, {
    select: (d) => d.jobs.find((j) => j.params.name === ENV) ?? null,
    ...poll((d?: { jobs: Job[] }) => (d?.jobs.some((j) => j.params.name === ENV && isActive(j)) ? SETUP_POLL_MS : 0)),
  });
  const job = latest.data ?? null;
  const running = Boolean(job && isActive(job));
  // Once it ends, the environment is looked at again: the section opens when it is ready.
  useReloadOn(running, props.onReady);
  const log = client.useQuery("get", "/jobs/{id}/log", { params: { path: { id: job?.id ?? "" }, query: { tail: 4000 } }, parseAs: "text" }, {
    enabled: running,
    ...poll(running ? SETUP_POLL_MS : 0),
  });

  const start = useAction(
    async () => {
      await client.post("/jobs", { body: { job: SETUP_JOB, params: { name: ENV }, priority: "high" } });
      await latest.refetch();
    },
    { notice: true },
  );
  const cancel = useAction(async (id: string) => {
    await client.post("/jobs/{id}/cancel", { params: { path: { id } } });
    await latest.refetch();
  });

  const failed = job && !running && job.status !== "succeeded" ? job : null;
  return (
    <Card className="gap-4">
      <CardHeader>
        <CardTitle>{props.update ? "AutoGluon's environment is out of date" : "Set up AutoGluon"}</CardTitle>
        <CardDescription>
          {props.update
            ? "It was installed from another requirements-autogluon.txt than this service's. Updating installs the packages it names now; predictors keep working meanwhile."
            : "AutoGluon runs in a Python environment of its own (about 1.8 GB, a few minutes to install), set apart from the service's models. It installs from PyPI once you click Set up, here or in a container alike."}
        </CardDescription>
      </CardHeader>
      <CardContent className="grid gap-3">
        {running ? (
          <>
            <div className="flex flex-wrap items-center gap-2 text-sm">
              <StatusBadge status={job!.status} />
              <span className="text-muted-foreground">{job!.status === "queued" ? "waiting for a running job to finish" : (job!.message ?? "installing")}</span>
              <Button type="button" variant="outline" size="sm" className="ml-auto" disabled={cancel.busy} onClick={() => void cancel.run(job!.id)}>
                Cancel
              </Button>
            </div>
            {log.data && <CodeBlock className="max-h-64 text-xs">{log.data}</CodeBlock>}
          </>
        ) : (
          <div className="flex flex-wrap items-center gap-3">
            <SubmitButton type="button" busy={start.busy} onClick={start.run}>
              {props.update ? "Update" : "Set up"}
            </SubmitButton>
            {failed && (
              <span className="text-sm text-critical-text">
                The last install {failed.status === "cancelled" ? "was cancelled" : `failed: ${failed.error ?? "see the setup-env job's log in Jobs"}`}
              </span>
            )}
          </div>
        )}
        <Notice notice={start.notice} />
      </CardContent>
    </Card>
  );
}

// ------------------------------------------------------------- the list
const LIST_LIMIT = 50;

function PredictorsCard() {
  const client = useApi();
  const pages = useCursorPages(null);
  const jobs = client.useQuery("get", "/jobs", { params: { query: { job: Object.values(TRAIN_JOB), limit: LIST_LIMIT, cursor: pages.cursor } } }, {
    ...poll((d?: { jobs: Job[] }) => pollInterval(d?.jobs ?? null)),
  });
  const rows = (jobs.data?.jobs ?? []).map(predictorOf).filter((p): p is Predictor => p !== null);

  const columns: GridColumn<Predictor>[] = [
    {
      key: "name",
      label: "Predictor",
      headClassName: "pl-4",
      className: "pl-4",
      render: (p) => (
        <>
          <a className="font-medium hover:underline" href={autogluonHref(p.id)}>
            {p.name}
          </a>
          <Badge variant="outline" className="ml-2 align-middle text-[11px]">
            {KIND_LABEL[p.kind]}
          </Badge>
          <div className="font-mono text-xs text-muted-foreground">{AUTOGLUON_PREFIX + p.id}</div>
        </>
      ),
    },
    { key: "predicts", label: "Predicts", className: "[overflow-wrap:anywhere]", render: (p) => p.predicts },
    {
      key: "score",
      label: "Score",
      render: (p) => {
        if (!p.result) return <span className="text-muted-foreground">–</span>;
        const s = scoreText(headlineScore(p.result).value, p.result.lower_is_better);
        return (
          <span title={s.hint}>
            <span className="tabular-nums">{s.text}</span> <span className="text-xs text-muted-foreground">{p.result.eval_metric}</span>
          </span>
        );
      },
    },
    { key: "rows", label: "Rows", align: "right", render: (p) => (p.result ? fmtInt(p.result.train_rows) : "–") },
    { key: "trained", label: "Trained", className: "whitespace-nowrap text-muted-foreground", render: (p) => fmtAgo(p.job.finished ?? p.job.created) },
    {
      key: "status",
      label: "State",
      render: (p) => (
        <span className="grid gap-1">
          <StatusBadge status={p.status} />
          {p.status === "running" && p.job.message && <span className="max-w-48 truncate text-xs text-muted-foreground">{p.job.message}</span>}
        </span>
      ),
    },
  ];

  return (
    <LoadedCard
      title="Predictors"
      description={(list) => (list.length ? `${rows.length} ${pages.hasPrevious ? "older" : "most recent"}` : "Train one from a table: a column to predict, or a series to forecast")}
      data={jobs.data?.jobs ?? null}
      error={errorText(jobs.error)}
      onRefresh={jobs.refetch}
      actions={
        <Button type="button" size="sm" asChild>
          <a href={newHref()}>
            <Plus aria-hidden="true" />
            New predictor
          </a>
        </Button>
      }
      isEmpty={() => rows.length === 0 && !pages.hasPrevious}
      empty="No predictors yet. Click New predictor, drop in a CSV, pick the column, and train."
      after={
        jobs.data &&
        (pages.hasPrevious || jobs.data.next) && (
          <Pager
            start={pages.index * LIST_LIMIT}
            shown={jobs.data.jobs.length}
            count={null}
            hasPrevious={pages.hasPrevious}
            hasNext={Boolean(jobs.data.next)}
            loading={jobs.isFetching}
            onPrevious={pages.previous}
            onNext={() => pages.next(jobs.data?.next ?? undefined)}
          />
        )
      }
    >
      {() => (
        <div className="overflow-x-auto rounded-lg border">
          <DataGrid columns={columns} rows={rows} rowKey={(p) => p.id} headerClassName="bg-muted/50" empty="No older predictors" />
        </div>
      )}
    </LoadedCard>
  );
}
