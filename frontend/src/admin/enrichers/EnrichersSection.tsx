import type { ReactNode } from "react";
import { Search } from "lucide-react";
import type { ApiClient } from "../../shared/api";
import { useHash } from "../../shared/browser";
import { errorText } from "../../shared/query";
import { EmptyCard } from "../../shared/components";
import { ENTITY_TYPES } from "../../shared/types";
import { locationFromHash } from "./enrichers";
import { DetailSkeleton, MasterDetail } from "../layout";
import { EnricherEditor } from "./EnrichersEditor";
import { EnrichersCard } from "./EnrichersList";
import { LookupCard } from "./EnrichersLookup";

/**
 * /admin#/enrichers: the enrichers on the left; on the right a lookup
 * across all of them, or the chosen one's editor and a test bench.
 */
export function EnrichersSection({ client }: { client: ApiClient }) {
  const loc = useHash(locationFromHash);
  const query = client.useQuery("get", "/enrichers");
  const list = query.data ?? null;
  const error = errorText(query.error);
  const reload = query.refetch;

  const types = list?.types.length ? list.types : ENTITY_TYPES;
  const byName = (name: string | undefined) => (name ? list?.enrichers.find((e) => e.name === name) : undefined);

  let view: ReactNode;
  if (!list) view = <DetailSkeleton />;
  else if (loc.view === "lookup") view = <LookupCard client={client} types={types} />;
  else if (loc.view === "new") {
    const source = byName(loc.dup);
    view = (
      <EnricherEditor
        key={`new:${loc.dup ?? ""}`}
        client={client}
        types={types}
        loaded={null}
        source={source}
        onChange={reload}
      />
    );
  } else {
    const current = byName(loc.name);
    view = current ? (
      <EnricherEditor key={current.name} client={client} types={types} loaded={current} onChange={reload} />
    ) : (
      <EmptyCard icon={Search} title={`No enricher named ${loc.name}`} body="It may have been deleted. Pick one on the left." />
    );
  }

  return (
    <MasterDetail list={<EnrichersCard list={list} loc={loc} error={error} reload={reload} />}>
      {view}
    </MasterDetail>
  );
}
