// The Ledger card: verifying the workspace's ledger, its issues, and retrying the time stamps.

import { useState } from "react";
import { RotateCw, ShieldCheck } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Card, CardAction, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { useAction } from "../../shared/action";
import { useApi } from "../../shared/api";
import { Facts, Notice, StatusDot, SubmitButton } from "../../shared/components";
import { DataGrid, type GridColumn } from "../../shared/grid";
import type { VerifyReport } from "../../shared/types";
import { issuesBySeq, verifySummary, workspaceQuery } from "./evidence";

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
export function LedgerCard({ workspace }: { workspace: string }) {
  const client = useApi();
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
              <Facts
                size="xs"
                rows={[
                  ["Head hash", <code className="break-all">{report.headHash ?? "–"}</code>],
                  ["Signing key", <code className="break-all">{report.signingKeySha256}</code>],
                ]}
              />
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
