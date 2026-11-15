import type { ReactNode } from "react";
import { CircleAlert, Search } from "lucide-react";
import { useApi } from "../../shared/api";
import { useHash } from "../../shared/browser";
import { EmptyCard } from "../../shared/components";
import { errorText } from "../../shared/query";
import { emptyEntry, entryForm, locationFromHash } from "./servers";
import { DetailSkeleton, MasterDetail } from "../layout";
import { EntryEditor } from "./ServersEntryEditor";
import { EntriesCard } from "./ServersEntries";
import { OverviewView } from "./ServersOverview";

/**
 * /admin#/servers: the model server entries on the left; on the right where
 * they come from and the providers, or one entry's editor with its backends.
 */
export function ServersSection() {
  const client = useApi();
  const loc = useHash(locationFromHash);
  const query = client.useQuery("get", "/admin/model-servers");
  const data = query.data ?? null;
  const error = errorText(query.error);
  const reload = query.refetch;
  const byId = (id: string | undefined) => (id ? data?.models.find((m) => m.model_id === id) : undefined);

  let view: ReactNode;
  if (!data) view = error ? <EmptyCard icon={CircleAlert} title="Could not load the model servers" body={error} /> : <DetailSkeleton />;
  else if (loc.view === "overview") view = <OverviewView data={data} reload={reload} />;
  else if (loc.view === "new") {
    const source = byId(loc.dup);
    view = (
      <EntryEditor
        key={`new:${loc.dup ?? ""}`}
        data={data}
        loaded={null}
        initial={source ? { ...entryForm(source, ""), model_id: "" } : emptyEntry(data.providers[0]?.name)}
        reload={reload}
      />
    );
  } else {
    const current = byId(loc.id);
    view = current ? (
      <EntryEditor key={current.model_id} data={data} loaded={current} initial={entryForm(current, current.model_id)} reload={reload} />
    ) : (
      <EmptyCard icon={Search} title={`No model server entry for ${loc.id}`} body="It may have been deleted. Pick one on the left." />
    );
  }

  return (
    <MasterDetail list={<EntriesCard data={data} loc={loc} error={error} reload={reload} />}>
      {view}
    </MasterDetail>
  );
}
