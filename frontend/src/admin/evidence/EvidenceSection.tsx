import type { ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { FolderOpen, Layers, ShieldCheck } from "lucide-react";
import { ApiError, type ApiClient } from "../../shared/api";
import { useHash } from "../../shared/browser";
import { message } from "../../shared/errors";
import { errorText } from "../../shared/query";
import { EmptyCard } from "../../shared/components";
import { INBOX, locationFromHash, workspaceQuery } from "./evidence";
import { DetailSkeleton, MasterDetail } from "../layout";
import { CaptureView } from "./EvidenceCapture";
import { CaseView, CasesCard } from "./EvidenceCases";
import { SearchBox, SearchView } from "./EvidenceSearch";
import { WorkspaceCard } from "./EvidenceWorkspaces";

/** The server's detail when evidence capture is off (EVIDENCE_DIR or ARCADEDB_PATH empty). */
const isOff = (e: unknown) => e instanceof ApiError && e.status === 503;
/**
 * /admin#/evidence: the workspace, a search box and the cases on the left; on
 * the right the chosen case's captures and a check of the workspace's whole
 * ledger, one capture's signed record with its files, or a search's captures
 * across the cases.
 */
export function EvidenceSection({ client }: { client: ApiClient }) {
  const loc = useHash(locationFromHash);
  const workspace = loc.workspace;
  // Both loads answer why when evidence capture is off, rather than failing.
  const loadedWorkspaces = useQuery({
    queryKey: ["evidence", "workspaces"],
    queryFn: async ({ signal }) => {
      try {
        return { list: (await client.get("/evidence/workspaces", { signal })).workspaces, off: "" };
      } catch (e) {
        if (isOff(e)) return { list: null, off: message(e) };
        throw e;
      }
    },
  });
  const loadedCases = useQuery({
    queryKey: ["evidence", "cases", workspace],
    queryFn: async ({ signal }) => {
      try {
        return { workspace, list: (await client.get("/evidence/cases", { params: { query: workspaceQuery(workspace) }, signal })).cases, off: "" };
      } catch (e) {
        // An unknown workspace: the view says so.
        if (e instanceof ApiError && e.status === 404) return { workspace, list: [], off: "" };
        if (isOff(e)) return { workspace, list: null, off: message(e) };
        throw e;
      }
    },
  });
  const workspaces = loadedWorkspaces.data?.list ?? null;
  // None while the cases shown are another workspace's.
  const cases = loadedCases.data?.workspace === workspace ? loadedCases.data.list : null;
  const off = loadedWorkspaces.data?.off || loadedCases.data?.off;
  const error = errorText(loadedCases.error) || errorText(loadedWorkspaces.error);

  if (off) {
    return (
      <EmptyCard
        icon={ShieldCheck}
        title="Evidence capture is off"
        body={
          <>
            {off}. Set <code>EVIDENCE_DIR</code> and <code>ARCADEDB_PATH</code> and restart the server.
          </>
        }
      />
    );
  }

  const caseId = loc.caseId ?? INBOX;
  const current = cases?.find((c) => c.id === caseId);
  const known = !workspaces || workspaces.some((w) => w.id === workspace);
  let view: ReactNode;
  if (!known) view = <EmptyCard icon={Layers} title={`No workspace ${workspace}`} body="The link may be wrong. Pick a workspace on the left." />;
  else if (!cases) view = <DetailSkeleton />;
  else if (loc.search) view = <SearchView key={workspace} client={client} workspace={workspace} search={loc.search} cases={cases} />;
  else if (!current) view = <EmptyCard icon={FolderOpen} title={`No case ${caseId}`} body="It may have been deleted, or the link is wrong. Pick a case on the left." />;
  else if (loc.captureId) {
    view = <CaptureView key={`${workspace}/${loc.captureId}`} client={client} workspace={workspace} caseId={caseId} captureId={loc.captureId} />;
  } else view = <CaseView key={`${workspace}/${caseId}`} client={client} workspace={workspace} current={current} />;

  return (
    <MasterDetail
      list={
        <>
          <WorkspaceCard client={client} workspaces={workspaces} active={workspace} reload={loadedWorkspaces.refetch} />
          <SearchBox key={`${workspace}/${loc.search?.q ?? ""}`} workspace={workspace} search={loc.search} />
          <CasesCard
            client={client}
            workspace={workspace}
            cases={known ? cases : []}
            active={loc.search ? "" : caseId}
            error={error}
            reload={loadedCases.refetch}
          />
        </>
      }
    >
      {view}
    </MasterDetail>
  );
}
