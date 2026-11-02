// One capture: the record's fields, its artifacts with downloads, what the enrichers found, and the signed record.

import { Download } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { cn } from "@/lib/utils";
import { useAction } from "../../shared/action";
import type { ApiClient } from "../../shared/api";
import { downloadResponse } from "../../shared/browser";
import { errorText } from "../../shared/query";
import { CodeBlock, Disclosure, Facts, Notice, StatusLabel, SubmitButton, type Fact } from "../../shared/components";
import { fmtBytes, fmtIso } from "../../shared/format";
import { DataGrid, type GridColumn } from "../../shared/grid";
import type { Capture, CaptureRecord } from "../../shared/types";
import { pretty } from "../../shared/json";
import { DEFAULT_WORKSPACE, enrichmentLine, evidenceHash, shortHash, workspaceQuery } from "./evidence";
import { DetailSkeleton } from "../layout";

export function CaptureView({ client, workspace, caseId, captureId }: { client: ApiClient; workspace: string; caseId: string; captureId: string }) {
  const query = client.useQuery(
    "get",
    "/evidence/captures/{capture_id}",
    { params: { path: { capture_id: captureId }, query: workspaceQuery(workspace) } },
    // The API serves the signed record as it is, typed as any object; Capture types it (CaptureRecord).
    { select: (d) => d as Capture },
  );
  const capture = query.data ?? null;
  const error = errorText(query.error);

  const download = useAction(
    async (name: string) =>
      downloadResponse(
        await client.raw("get", "/evidence/captures/{capture_id}/artifacts/{name}", { params: { path: { capture_id: captureId, name }, query: workspaceQuery(workspace) } }),
        name,
      ),
    { failed: (name) => `Could not download ${name}` },
  );

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
        <SubmitButton
          type="button"
          variant="ghost"
          size="icon"
          className="size-8"
          aria-label={`Download ${a.name}`}
          busy={download.running?.[0] === a.name}
          icon={<Download aria-hidden="true" />}
          onClick={() => download.run(a.name)}
        />
      ),
    },
  ];
  const page = (record.client.page ?? {}) as { documentUrl?: string; tabUrl?: string; title?: string };
  const url = page.documentUrl ?? page.tabUrl;
  const stamp = capture.timestamp;
  const enrichment = record.enrichment;
  const rows: Fact[] = [
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
          <Facts rows={rows} />

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

          <Disclosure label="Signed record">
            <CodeBlock className="max-h-[32rem]">{pretty(record)}</CodeBlock>
          </Disclosure>
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
