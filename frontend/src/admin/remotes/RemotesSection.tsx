import type { ReactNode } from "react";
import { CloudUpload } from "lucide-react";
import { useApi } from "../../shared/api";
import { useHash } from "../../shared/browser";
import { EmptyCard, StatusDot } from "../../shared/components";
import { errorText } from "../../shared/query";
import { plural } from "../../shared/format";
import { cn } from "@/lib/utils";
import { DetailSkeleton, ListCard, MasterDetail, NavRow, NewLink, NoRows } from "../layout";
import { RemoteEditor } from "./RemotesEditor";
import { locationFromHash, readiness, remoteHash, summary, type RemoteInfo, type RemoteLocation } from "./remotes";

/**
 * /admin#/remotes: the places outside the service that buckets' files are copied to (GET /remotes) on the left;
 * on the right the chosen one's editor and test, or a new one's, started blank or from a preset. A bucket names
 * its remote on its page in Data.
 */
export function RemotesSection() {
  const client = useApi();
  const loc = useHash(locationFromHash);
  const query = client.useQuery("get", "/remotes");
  const list = query.data?.remotes ?? null;
  const reload = query.refetch;

  let view: ReactNode;
  if (!list) view = <DetailSkeleton />;
  else if (loc.view === "new") view = <RemoteEditor key={`new:${loc.preset ?? ""}`} loaded={null} preset={loc.preset} onChange={reload} />;
  else if (loc.view === "edit") {
    const current = list.find((r) => r.name === loc.name);
    view = current ? (
      <RemoteEditor key={current.name} loaded={current} onChange={reload} />
    ) : (
      <EmptyCard icon={CloudUpload} title={`No remote named ${loc.name}`} body="It may have been deleted. Pick one on the left." />
    );
  } else {
    view = (
      <EmptyCard
        icon={CloudUpload}
        title={list.length ? "Pick a remote" : "No remotes yet"}
        body={
          <>
            A remote is a place outside the service that a bucket's files are copied to: S3, Cloudflare R2, Backblaze B2, Google Cloud Storage, Azure
            Blob, a folder. Make one with New, test it, then name it on a bucket's page in Data: each file is copied after its upload, deleted there
            after its delete, and read back from there if it goes missing here. Its keys are named, never given: their values stay in the service's
            environment.
          </>
        }
      />
    );
  }

  return (
    <MasterDetail list={<RemotesCard list={list} loc={loc} error={errorText(query.error)} reload={reload} />}>
      {view}
    </MasterDetail>
  );
}

function RemotesCard(props: { list: RemoteInfo[] | null; loc: RemoteLocation; error: string; reload(): Promise<unknown> }) {
  const { list, loc } = props;
  const chosen = loc.view === "edit" ? loc.name : null;
  return (
    <ListCard
      title="Remotes"
      description={list ? `${plural(list.length, "remote")} · ${plural(list.reduce((n, r) => n + r.buckets.length, 0), "bucket")} copied` : "Loading…"}
      label="Remotes"
      onRefresh={props.reload}
      action={<NewLink href={remoteHash({ view: "new" })} />}
      loaded={Boolean(list)}
      error={props.error}
      footer={<p className="text-xs text-muted-foreground">Secrets are named as environment variables of the service; their values stay there.</p>}
    >
      {list?.map((r) => (
        <RemoteRow key={r.name} info={r} active={r.name === chosen} />
      ))}
      {list && !list.length && <NoRows>No remotes yet</NoRows>}
    </ListCard>
  );
}

function RemoteRow({ info, active }: { info: RemoteInfo; active: boolean }) {
  const ready = readiness(info);
  return (
    <NavRow href={remoteHash({ view: "edit", name: info.name })} active={active} tall>
      <span className="flex min-w-0 items-center gap-2">
        <span title={ready.label} className="inline-flex">
          <StatusDot color={ready.color} />
        </span>
        <span className={cn("truncate font-mono text-[13px]", active && "font-medium")}>{info.name}</span>
        <span className="ml-auto shrink-0 text-[11px] text-muted-foreground">{info.buckets.length ? plural(info.buckets.length, "bucket") : "unused"}</span>
      </span>
      <span className="truncate pl-4 text-xs text-muted-foreground" title={info.label ?? undefined}>
        {info.label || summary(info)}
      </span>
    </NavRow>
  );
}
