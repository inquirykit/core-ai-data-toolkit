import type { ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { FolderOpen, Layers } from "lucide-react";
import { useApi, ApiError } from "../../shared/api";
import { useHash } from "../../shared/browser";
import { errorText } from "../../shared/query";
import { EmptyCard } from "../../shared/components";
import { INBOX, locationFromHash, workspaceQuery } from "./evidence";
import { DetailSkeleton, MasterDetail } from "../layout";
import { CaptureView } from "./EvidenceCapture";
import { CaseView, CasesCard } from "./EvidenceCases";
import { SearchBox, SearchView } from "./EvidenceSearch";
import { WorkspaceCard } from "./EvidenceWorkspaces";

/**
 * /admin#/evidence: the workspace (one of /db's, by its name), a search box
 * and the cases on the left; on the right the chosen case's captures and a
 * check of the workspace's whole ledger, one capture's signed record with its
 * files, or a search's captures across the cases.
 */
export function EvidenceSection() {
  const client = useApi();
  const loc = useHash(locationFromHash);
  const workspace = loc.workspace;
  // The workspaces are the Data section's (GET /db, without the service's own).
  const loadedWorkspaces = client.useQuery("get", "/db", {}, { select: (d) => d.workspaces });
  const loadedCases = useQuery({
    queryKey: ["evidence", "cases", workspace],
    queryFn: async ({ signal }) => {
      try {
        return { workspace, list: (await client.get("/evidence/cases", { params: { query: workspaceQuery(workspace) }, signal })).cases };
      } catch (e) {
        // An unknown workspace: the view says so.
        if (e instanceof ApiError && e.status === 404) return { workspace, list: [] };
        throw e;
      }
    },
  });
  const workspaces = loadedWorkspaces.data ?? null;
  // None while the cases shown are another workspace's.
  const cases = loadedCases.data?.workspace === workspace ? loadedCases.data.list : null;
  const error = errorText(loadedCases.error) || errorText(loadedWorkspaces.error);

  const caseId = loc.caseId ?? INBOX;
  const current = cases?.find((c) => c.id === caseId);
  // Known until the workspaces say otherwise.
  const known = !workspaces || workspaces.some((w) => w.name === workspace);
  let view: ReactNode;
  if (!known) view = <EmptyCard icon={Layers} title={`No workspace ${workspace}`} body="The link may be wrong. Pick a workspace on the left." />;
  else if (!cases) view = <DetailSkeleton />;
  else if (loc.search) view = <SearchView key={workspace} workspace={workspace} search={loc.search} cases={cases} />;
  else if (!current) view = <EmptyCard icon={FolderOpen} title={`No case ${caseId}`} body="It may have been deleted, or the link is wrong. Pick a case on the left." />;
  else if (loc.captureId) {
    view = <CaptureView key={`${workspace}/${loc.captureId}`} workspace={workspace} caseId={caseId} captureId={loc.captureId} />;
  } else view = <CaseView key={`${workspace}/${caseId}`} workspace={workspace} current={current} />;

  return (
    <MasterDetail
      list={
        <>
          <WorkspaceCard workspaces={workspaces} active={workspace} />
          <SearchBox key={`${workspace}/${loc.search?.q ?? ""}`} workspace={workspace} search={loc.search} />
          <CasesCard
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
