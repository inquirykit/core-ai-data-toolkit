// The workspace's cases and making one, and a case's captures with the ledger beside them.

import { useState, type FormEvent } from "react";
import { ChevronRight, FolderOpen, Plus, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useAction } from "../../shared/action";
import type { ApiClient } from "../../shared/api";
import { errorText } from "../../shared/query";
import { Notice, StatusLabel, SubmitButton } from "../../shared/components";
import { fmtIso, plural } from "../../shared/format";
import { DataGrid, type GridColumn } from "../../shared/grid";
import type { CaptureSummary, EvidenceCase } from "../../shared/types";
import { captureLabel, DEFAULT_WORKSPACE, evidenceHash, shortHash, timestampState, workspaceQuery } from "./evidence";
import { ListCard, LoadedCard, NavRow } from "../layout";
import { LedgerCard } from "./EvidenceLedger";

export function CasesCard(props: {
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
      description={cases ? plural(cases.length, "case") : "Loading…"}
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
export function CaseView({ client, workspace, current }: { client: ApiClient; workspace: string; current: EvidenceCase }) {
  const query = client.useQuery(
    "get",
    "/evidence/cases/{case_id}/captures",
    { params: { path: { case_id: current.id }, query: workspaceQuery(workspace) } },
    { select: (d) => d.captures },
  );
  const captures = query.data ?? null;
  const error = errorText(query.error);

  return (
    <div className="grid gap-5">
      <LoadedCard
        title={current.name}
        description={(list) => `${plural(list.length, "capture")} · created ${fmtIso(current.createdAt)}`}
        data={captures}
        error={error}
        onRefresh={() => void query.refetch()}
        skeleton="h-40"
        isEmpty={(list) => !list.length}
        empty={
          workspace === DEFAULT_WORKSPACE ? (
            "No captures in this case yet. Capture a page with the browser extension, choosing this case."
          ) : (
            <>
              No captures in this case yet. The browser extension captures into the default workspace; other clients post to{" "}
              <code>/evidence/captures?workspace={workspace}</code>.
            </>
          )
        }
      >
        {(list) => (
          <div className="overflow-hidden rounded-lg border">
            <DataGrid columns={CAPTURE_COLUMNS} rows={list} rowKey={(c) => c.captureId} rowHref={(c) => evidenceHash(workspace, current.id, c.captureId)} empty="No captures" />
          </div>
        )}
      </LoadedCard>
      <LedgerCard client={client} workspace={workspace} />
    </div>
  );
}
