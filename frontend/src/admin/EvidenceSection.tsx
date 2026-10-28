import { useState, type FormEvent, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { ChevronRight, Download, FolderOpen, Layers, Plus, RotateCw, ShieldCheck, X } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";
import { useAction } from "../shared/action";
import { ApiError, type ApiClient } from "../shared/api";
import { downloadResponse, useHash } from "../shared/browser";
import { failed, message } from "../shared/errors";
import { errorText } from "../shared/query";
import { EmptyCard, Notice, SelectInput, StatusDot, StatusLabel, SubmitButton } from "../shared/components";
import { fmtBytes } from "../shared/format";
import { DataGrid, type GridColumn } from "../shared/grid";
import type { Capture, CaptureRecord, CaptureSummary, EvidenceCase, EvidenceWorkspace, VerifyReport } from "../shared/types";
import { pretty } from "./evals";
import {
  captureLabel,
  DEFAULT_WORKSPACE,
  enrichmentLine,
  evidenceHash,
  fmtIso,
  INBOX,
  issuesBySeq,
  locationFromHash,
  shortHash,
  timestampState,
  verifySummary,
  workspaceQuery,
} from "./evidence";
import { DetailSkeleton, ListCard, MasterDetail, NavRow } from "./layout";

/** The server's detail when evidence capture is off (EVIDENCE_DIR or ARCADEDB_PATH empty). */
const isOff = (e: unknown) => e instanceof ApiError && e.status === 503;

/**
 * /admin#/evidence: the workspace and its cases on the left; on the right the
 * chosen case's captures and a check of the workspace's whole ledger, or one
 * capture's signed record with its files.
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
  else if (!current) view = <EmptyCard icon={FolderOpen} title={`No case ${caseId}`} body="It may have been deleted, or the link is wrong. Pick a case on the left." />;
  else if (loc.captureId) {
    view = <CaptureView key={`${workspace}/${loc.captureId}`} client={client} workspace={workspace} caseId={caseId} captureId={loc.captureId} />;
  } else view = <CaseView key={`${workspace}/${caseId}`} client={client} workspace={workspace} current={current} />;

  return (
    <MasterDetail
      list={
        <>
          <WorkspaceCard client={client} workspaces={workspaces} active={workspace} reload={loadedWorkspaces.refetch} />
          <CasesCard client={client} workspace={workspace} cases={known ? cases : []} active={caseId} error={error} reload={loadedCases.refetch} />
        </>
      }
    >
      {view}
    </MasterDetail>
  );
}

// ------------------------------------------------------------- workspaces
/** The workspace picker. A new workspace takes a name and, optionally, the id of the workspace it stands for
 * in your own application (else one is made). */
function WorkspaceCard(props: { client: ApiClient; workspaces: EvidenceWorkspace[] | null; active: string; reload(): Promise<unknown> }) {
  const { workspaces, active } = props;
  const [adding, setAdding] = useState(false);
  const [name, setName] = useState("");
  const [id, setId] = useState("");

  const create = useAction(
    async (event: FormEvent) => {
      event.preventDefault();
      const trimmed = name.trim();
      if (!trimmed) return;
      const body = id.trim() ? { name: trimmed, id: id.trim() } : { name: trimmed };
      const made = await props.client.post("/evidence/workspaces", { body });
      setName("");
      setId("");
      setAdding(false);
      await props.reload();
      location.hash = evidenceHash(made.workspace.id, null);
    },
    { notice: true },
  );

  return (
    <Card className="gap-3 py-4">
      <CardHeader className="px-4">
        <CardTitle>Workspace</CardTitle>
        <CardDescription>Each has its own ledger: cases, chain and files.</CardDescription>
        <CardAction>
          <Button type="button" size="sm" className="h-8" onClick={() => setAdding((v) => !v)} aria-expanded={adding}>
            {adding ? <X aria-hidden="true" /> : <Plus aria-hidden="true" />}
            New
          </Button>
        </CardAction>
      </CardHeader>
      <CardContent className="grid gap-2 px-4">
        <SelectInput
          className="w-full"
          aria-label="Workspace"
          placeholder={workspaces ? "Pick a workspace" : "Loading…"}
          disabled={!workspaces}
          value={active}
          onChange={(value) => (location.hash = evidenceHash(value, null))}
          options={(workspaces ?? []).map((w) => ({ value: w.id, label: w.name }))}
        />
        {adding && (
          <form className="grid gap-2 pt-1" onSubmit={create.run}>
            <Input
              autoFocus
              aria-label="Workspace name"
              placeholder="Name"
              maxLength={200}
              value={name}
              onChange={(e) => setName(e.target.value)}
              disabled={create.busy}
            />
            <Input
              aria-label="Workspace id"
              placeholder="Id (optional): your application's"
              maxLength={36}
              className="font-mono text-xs"
              value={id}
              onChange={(e) => setId(e.target.value)}
              disabled={create.busy}
            />
            <SubmitButton size="sm" busy={create.busy} disabled={!name.trim()}>
              Create workspace
            </SubmitButton>
            <Notice notice={create.notice} />
          </form>
        )}
      </CardContent>
    </Card>
  );
}

// ----------------------------------------------------------------- cases
function CasesCard(props: {
  client: ApiClient;
  workspace: string;
  cases: EvidenceCase[] | null;
  active: string;
  error: string;
  reload(): Promise<unknown>;
}) {
  const { cases, active } = props;
  const [adding, setAdding] = useState(false);
  const [name, setName] = useState("");

  const create = useAction(
    async (event: FormEvent) => {
      event.preventDefault();
      const trimmed = name.trim();
      if (!trimmed) return;
      const made = await props.client.post("/evidence/cases", { params: { query: workspaceQuery(props.workspace) }, body: { name: trimmed } });
      setName("");
      setAdding(false);
      await props.reload();
      location.hash = evidenceHash(props.workspace, made.case.id);
    },
    { notice: true },
  );

  return (
    <ListCard
      title="Cases"
      description={cases ? `${cases.length} case${cases.length === 1 ? "" : "s"}` : "Loading…"}
      label="Cases"
      onRefresh={props.reload}
      action={
        <Button type="button" size="sm" className="h-8" onClick={() => setAdding((v) => !v)} aria-expanded={adding}>
          {adding ? <X aria-hidden="true" /> : <Plus aria-hidden="true" />}
          New
        </Button>
      }
      loaded={Boolean(cases)}
      error={props.error}
      top={
        adding && (
          <form className="grid gap-2 px-2 pb-1" onSubmit={create.run}>
            <Input
              autoFocus
              aria-label="Case name"
              placeholder="Case name"
              maxLength={200}
              value={name}
              onChange={(e) => setName(e.target.value)}
              disabled={create.busy}
            />
            <SubmitButton size="sm" busy={create.busy} disabled={!name.trim()}>
              Create case
            </SubmitButton>
            <Notice notice={create.notice} />
          </form>
        )
      }
    >
      {cases?.map((c) => (
        <NavRow key={c.id} href={evidenceHash(props.workspace, c.id)} active={c.id === active} title={`Created ${fmtIso(c.createdAt)}`}>
          <FolderOpen aria-hidden="true" />
          <span className="truncate">{c.name}</span>
        </NavRow>
      ))}
    </ListCard>
  );
}

// ------------------------------------------------------------------ case
const CAPTURE_COLUMNS: GridColumn<CaptureSummary>[] = [
  { key: "seq", label: "#", align: "right", headClassName: "w-12", className: "text-muted-foreground", render: (c) => c.seq },
  {
    key: "page",
    label: "Page",
    className: "max-w-[28rem]",
    render: (c) => (
      <>
        <div className="truncate font-medium">{captureLabel(c)}</div>
        {c.title && c.url && <div className="truncate text-xs text-muted-foreground">{c.url}</div>}
      </>
    ),
  },
  { key: "received", label: "Received", className: "whitespace-nowrap tabular-nums", render: (c) => fmtIso(c.receivedAt) },
  {
    key: "timestamp",
    label: "Timestamp",
    className: "whitespace-nowrap",
    render: (c) => <StatusLabel status={timestampState(c)} title={c.timestampedAt ? fmtIso(c.timestampedAt) : undefined} />,
  },
  {
    key: "hash",
    label: "Record hash",
    render: (c) => (
      <code className="text-xs" title={c.recordHash}>
        {shortHash(c.recordHash)}
      </code>
    ),
  },
  { key: "open", headClassName: "w-8", render: () => <ChevronRight className="size-4 text-muted-foreground" aria-hidden="true" /> },
];

function CaseView({ client, workspace, current }: { client: ApiClient; workspace: string; current: EvidenceCase }) {
  const query = client.useQuery(
    "get",
    "/evidence/cases/{case_id}/captures",
    { params: { path: { case_id: current.id }, query: workspaceQuery(workspace) } },
    { select: (d) => d.captures },
  );
  const captures = query.data ?? null;
  const error = errorText(query.error);
  const reload = () => void query.refetch();

  return (
    <div className="grid gap-5">
      <Card>
        <CardHeader>
          <CardTitle>{current.name}</CardTitle>
          <CardDescription>
            {captures ? `${captures.length} capture${captures.length === 1 ? "" : "s"}` : "Loading…"} · created {fmtIso(current.createdAt)}
          </CardDescription>
          <CardAction>
            <Button type="button" variant="ghost" size="icon" className="size-8" aria-label="Refresh captures" onClick={reload}>
              <RotateCw aria-hidden="true" />
            </Button>
          </CardAction>
        </CardHeader>
        <CardContent>
          {error && <Notice notice={{ text: error, kind: "error" }} />}
          {!captures && !error && <Skeleton className="h-40" />}
          {captures && !captures.length && (
            <p className="text-sm text-muted-foreground">
              {workspace === DEFAULT_WORKSPACE ? (
                "No captures in this case yet. Capture a page with the browser extension, choosing this case."
              ) : (
                <>
                  No captures in this case yet. The browser extension captures into the default workspace; other clients post to{" "}
                  <code>/evidence/captures?workspace={workspace}</code>.
                </>
              )}
            </p>
          )}
          {captures && captures.length > 0 && (
            <div className="overflow-hidden rounded-lg border">
              <DataGrid
                columns={CAPTURE_COLUMNS}
                rows={captures}
                rowKey={(c) => c.captureId}
                rowHref={(c) => evidenceHash(workspace, current.id, c.captureId)}
                empty="No captures"
              />
            </div>
          )}
        </CardContent>
      </Card>
      <LedgerCard client={client} workspace={workspace} />
    </div>
  );
}

// ---------------------------------------------------------------- ledger
type IssueGroup = ReturnType<typeof issuesBySeq>[number];

const ISSUE_COLUMNS: GridColumn<IssueGroup>[] = [
  { key: "seq", label: "#", align: "right", headClassName: "w-12", className: "align-top text-muted-foreground", render: ([seq]) => seq },
  {
    key: "issues",
    label: "Issues",
    render: ([, issues]) => (
      <ul className="grid gap-1">
        {issues.map((i, n) => (
          <li key={n} className="flex items-start gap-2">
            <Badge variant={i.severity === "error" ? "destructive" : "secondary"} className="mt-px">
              {i.severity}
            </Badge>
            <span>{i.message}</span>
          </li>
        ))}
      </ul>
    ),
  },
];

function LedgerCard({ client, workspace }: { client: ApiClient; workspace: string }) {
  const [report, setReport] = useState<VerifyReport | null>(null);

  const verify = useAction(
    async () => {
      retry.clear();
      setReport(await client.get("/evidence/verify", { params: { query: workspaceQuery(workspace) } }));
    },
    { notice: true },
  );

  const retry = useAction(
    async () => {
      const r = await client.post("/evidence/timestamp-pending");
      if (report) await verify.run();
      return r;
    },
    {
      notice: true,
      success: (r) => ({ text: `Timestamps, in every workspace: ${r.ok} granted, ${r.failed} failed`, kind: r.failed ? "error" : "ok" }),
    },
  );

  const notice = retry.notice.text ? retry.notice : verify.notice;
  const groups = report ? issuesBySeq(report) : [];
  return (
    <Card>
      <CardHeader>
        <CardTitle>Ledger</CardTitle>
        <CardDescription>
          Every record of this workspace, across its cases: sequence, chain links, canonical form, signatures, the workspace each names,
          files on disk and time stamp tokens.
        </CardDescription>
        <CardAction className="flex gap-2">
          <SubmitButton type="button" variant="outline" size="sm" busy={retry.busy} icon={<RotateCw aria-hidden="true" />} onClick={retry.run}>
            Retry timestamps
          </SubmitButton>
          <SubmitButton type="button" size="sm" busy={verify.busy} icon={<ShieldCheck aria-hidden="true" />} onClick={verify.run}>
            Verify
          </SubmitButton>
        </CardAction>
      </CardHeader>
      {(report || notice.text) && (
        <CardContent className="grid gap-3">
          <Notice notice={notice} />
          {report && (
            <>
              <p className="flex items-center gap-2 text-sm">
                <StatusDot color={report.ok ? "var(--good)" : "var(--critical)"} />
                {verifySummary(report)}
              </p>
              <dl className="grid gap-x-6 gap-y-1 text-xs sm:grid-cols-[auto_minmax(0,1fr)]">
                <dt className="text-muted-foreground">Head hash</dt>
                <dd>
                  <code className="break-all">{report.headHash ?? "–"}</code>
                </dd>
                <dt className="text-muted-foreground">Signing key</dt>
                <dd>
                  <code className="break-all">{report.signingKeySha256}</code>
                </dd>
              </dl>
              {groups.length > 0 && (
                <div className="overflow-hidden rounded-lg border">
                  <DataGrid columns={ISSUE_COLUMNS} rows={groups} rowKey={([seq]) => seq} empty="No issues" />
                </div>
              )}
            </>
          )}
        </CardContent>
      )}
      <CardFooter className="text-xs text-muted-foreground">
        A chain cannot show that the newest records were removed: note the head hash somewhere else, or rely on the time stamp tokens.
      </CardFooter>
    </Card>
  );
}

// --------------------------------------------------------------- capture
function CaptureView({ client, workspace, caseId, captureId }: { client: ApiClient; workspace: string; caseId: string; captureId: string }) {
  const query = client.useQuery(
    "get",
    "/evidence/captures/{capture_id}",
    { params: { path: { capture_id: captureId }, query: workspaceQuery(workspace) } },
    // The API serves the signed record as it is, typed as any object; Capture types it (CaptureRecord).
    { select: (d) => d as Capture },
  );
  const capture = query.data ?? null;
  const error = errorText(query.error);

  const download = (name: string) =>
    client
      .raw("get", "/evidence/captures/{capture_id}/artifacts/{name}", { params: { path: { capture_id: captureId, name }, query: workspaceQuery(workspace) } })
      .then((response) => downloadResponse(response, name))
      .catch(failed(`Could not download ${name}`));

  if (error) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>Capture</CardTitle>
          <CardAction>
            <BackLink workspace={workspace} caseId={caseId} />
          </CardAction>
        </CardHeader>
        <CardContent>
          <Notice notice={{ text: error, kind: "error" }} />
        </CardContent>
      </Card>
    );
  }
  if (!capture) return <DetailSkeleton />;

  const { record } = capture;
  const artifactColumns: GridColumn<CaptureRecord["artifacts"][number]>[] = [
    { key: "name", label: "Name", className: "font-medium", render: (a) => a.name },
    { key: "type", label: "Type", className: "text-muted-foreground", render: (a) => a.mediaType },
    { key: "size", label: "Size", align: "right", render: (a) => fmtBytes(a.size) },
    {
      key: "sha",
      label: "SHA-256",
      render: (a) => (
        <code className="text-xs" title={a.sha256}>
          {shortHash(a.sha256)}
        </code>
      ),
    },
    {
      key: "download",
      headClassName: "w-10",
      render: (a) => (
        <Button type="button" variant="ghost" size="icon" className="size-8" aria-label={`Download ${a.name}`} onClick={() => download(a.name)}>
          <Download aria-hidden="true" />
        </Button>
      ),
    },
  ];
  const page = (record.client.page ?? {}) as { documentUrl?: string; tabUrl?: string; title?: string };
  const url = page.documentUrl ?? page.tabUrl;
  const stamp = capture.timestamp;
  const enrichment = record.enrichment;
  const rows: [string, ReactNode][] = [
    ["Received", fmtIso(record.receivedAt)],
    ["Workspace", record.workspaceId ?? `${DEFAULT_WORKSPACE} (a v2 record, from before workspaces)`],
    ["Case", record.caseId],
    ["Record hash", <code className="break-all">{capture.recordHash}</code>],
    ["Previous record", <code className="break-all">{record.prevRecordHash}</code>],
    ["Signature", <code className="break-all">{capture.signature}</code>],
    ["Signing key", <code className="break-all">{record.service.signingKeySha256}</code>],
    ["Service", `${record.service.name} ${record.service.version} · ${record.schema}`],
    [
      "Timestamp",
      <StatusLabel
        status={
          stamp.genTime
            ? { label: `granted, ${fmtIso(stamp.genTime)}`, color: "var(--good)" }
            : { label: `none yet${stamp.error ? `: ${stamp.error}` : ""}`, color: "var(--warning)" }
        }
      />,
    ],
  ];

  return (
    <div className="grid gap-5">
      <Card>
        <CardHeader>
          <CardTitle className="truncate">{page.title?.trim() || url || captureId}</CardTitle>
          <CardDescription className="truncate">
            #{record.seq} · {url ?? captureId}
          </CardDescription>
          <CardAction>
            <BackLink workspace={workspace} caseId={caseId} />
          </CardAction>
        </CardHeader>
        <CardContent className="grid gap-5">
          <dl className="grid gap-x-6 gap-y-1.5 text-sm sm:grid-cols-[auto_minmax(0,1fr)]">
            {rows.map(([label, value]) => (
              <div key={label} className="contents">
                <dt className="text-muted-foreground">{label}</dt>
                <dd className="min-w-0 text-xs sm:text-sm">{value}</dd>
              </div>
            ))}
          </dl>

          <section className="grid gap-2">
            <h3 className="text-sm font-medium">Artifacts</h3>
            <div className="overflow-hidden rounded-lg border">
              <DataGrid columns={artifactColumns} rows={record.artifacts} rowKey={(a) => a.name} empty="No files" />
            </div>
          </section>

          <section className="grid gap-2">
            <h3 className="text-sm font-medium">Enrichment</h3>
            {"skipped" in enrichment ? (
              <p className="text-sm text-muted-foreground">Skipped: EVIDENCE_ENRICH is off.</p>
            ) : !enrichment.items.length ? (
              <p className="text-sm text-muted-foreground">Nothing to look up in this capture.</p>
            ) : (
              <ul className="grid gap-1 text-sm">
                {enrichment.items.map((item) => (
                  <li key={`${item.type}:${item.value}`} className="flex flex-wrap items-baseline gap-x-2">
                    <code className="text-xs">{item.value}</code>
                    <Badge variant="outline">{item.type}</Badge>
                    <span className={cn("text-muted-foreground", item.error && "text-critical-text")}>{enrichmentLine(item)}</span>
                  </li>
                ))}
              </ul>
            )}
          </section>

          <Collapsible>
            <CollapsibleTrigger className="group flex items-center gap-1 text-sm font-medium">
              <ChevronRight className="size-4 transition-transform group-data-[state=open]:rotate-90" aria-hidden="true" />
              Signed record
            </CollapsibleTrigger>
            <CollapsibleContent>
              <pre className="mt-2 max-h-[32rem] overflow-auto rounded-lg border bg-muted/40 p-3 text-xs">{pretty(record)}</pre>
            </CollapsibleContent>
          </Collapsible>
        </CardContent>
      </Card>
    </div>
  );
}

function BackLink({ workspace, caseId }: { workspace: string; caseId: string }) {
  return (
    <Button type="button" variant="outline" size="sm" asChild>
      <a href={evidenceHash(workspace, caseId)}>Back to case</a>
    </Button>
  );
}
