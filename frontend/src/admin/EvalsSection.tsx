// /admin#/evals: datasets on the left; on the right the chosen dataset
// (EvalsDataset.tsx: its examples and runs), a run (EvalsRun.tsx: its items
// and scores, with scoring and the LLM judge), or two runs side by side
// (EvalsCompare.tsx).

import { useState, type FormEvent } from "react";
import { ChevronRight, FlaskConical, Plus } from "lucide-react";
import { Input } from "@/components/ui/input";
import { useAction } from "../shared/action";
import type { ApiClient } from "../shared/api";
import { useHash } from "../shared/browser";
import { errorText } from "../shared/query";
import { EmptyCard, Field, Notice, SubmitButton } from "../shared/components";
import { fmtInt } from "../shared/format";
import type { Dataset } from "../shared/types";
import { evalsFromHash, evalsHref } from "./evals";
import { CompareView } from "./EvalsCompare";
import { DatasetView } from "./EvalsDataset";
import { RunView } from "./EvalsRun";
import { DetailSkeleton, ListCard, MasterDetail, NavRow, NoRows } from "./layout";
import type { CatalogInfo } from "./panels";

export function EvalsSection({ client, catalog }: { client: ApiClient; catalog: CatalogInfo }) {
  const loc = useHash(evalsFromHash);
  const query = client.useQuery("get", "/admin/datasets", undefined, { select: (d) => d.datasets });
  const datasets = query.data ?? null;
  const error = errorText(query.error);
  const reload = query.refetch;

  const current = datasets?.find((d) => d.id === loc.dataset);
  return (
    <MasterDetail list={<DatasetsCard datasets={datasets} current={loc.dataset} client={client} error={error} reload={reload} />}>
      {!datasets ? (
        <DetailSkeleton />
      ) : loc.dataset && !current ? (
        <EmptyCard icon={FlaskConical} title="No such dataset" body="It may have been deleted. Pick one on the left." />
      ) : current && loc.compare ? (
        <CompareView key={loc.compare.join()} client={client} dataset={current} runs={loc.compare} />
      ) : current && loc.run ? (
        <RunView key={loc.run} client={client} dataset={current} runId={loc.run} onChange={reload} />
      ) : current ? (
        <DatasetView key={current.id} client={client} dataset={current} catalog={catalog} onChange={reload} />
      ) : (
        <EmptyCard
          icon={FlaskConical}
          title={datasets.length ? "Pick a dataset" : "No datasets yet"}
          body="A dataset is a set of example inputs, with reference answers when you have them. Save requests to one from Activity & logs, or add examples here; then run models over it, score the outputs, and compare runs side by side."
        />
      )}
    </MasterDetail>
  );
}

// ---------------------------------------------------------------- datasets
function DatasetsCard(props: { datasets: Dataset[] | null; current: string | null; client: ApiClient; error: string; reload(): Promise<unknown> }) {
  const [name, setName] = useState("");

  const create = useAction(
    async (event: FormEvent) => {
      event.preventDefault();
      const created = await props.client.post("/admin/datasets", { body: { name: name.trim() } });
      setName("");
      await props.reload();
      location.hash = evalsHref(created.id);
    },
    { notice: true },
  );

  const runs = props.datasets?.reduce((n, d) => n + d.runs, 0) ?? 0;
  return (
    <ListCard
      title="Datasets"
      description={props.datasets ? `${props.datasets.length} · ${fmtInt(runs)} ${runs === 1 ? "run" : "runs"}` : "Loading…"}
      label="Datasets"
      onRefresh={props.reload}
      loaded={Boolean(props.datasets)}
      error={props.error}
      footer={
        <form className="grid gap-2" onSubmit={create.run}>
          <Field label="New dataset">
            <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="Name" required maxLength={120} />
          </Field>
          <SubmitButton size="sm" busy={create.busy} icon={<Plus aria-hidden="true" />} disabled={!name.trim()}>
            Create
          </SubmitButton>
          <Notice notice={create.notice} />
        </form>
      }
    >
      {props.datasets?.map((d) => (
        <NavRow key={d.id} href={evalsHref(d.id)} active={d.id === props.current}>
          <span className="min-w-0 flex-1 truncate">{d.name}</span>
          <span className="text-xs text-muted-foreground tabular-nums">
            {fmtInt(d.examples)} · {fmtInt(d.runs)} run{d.runs === 1 ? "" : "s"}
          </span>
          <ChevronRight aria-hidden="true" />
        </NavRow>
      ))}
      {props.datasets && !props.datasets.length && <NoRows>None yet</NoRows>}
    </ListCard>
  );
}
