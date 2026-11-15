// A saved workflow's runs, newest first: status, rows and pages, error and
// times, and a Cancel for one queued or running. Clicking one shows its counts
// on the canvas; clicking it again hides them.

import { X } from "lucide-react";
import { cn } from "@/lib/utils";
import { useAction } from "../../shared/action";
import { useApi } from "../../shared/api";
import { StatusLabel, SubmitButton } from "../../shared/components";
import { fmtAgo, fmtClock, fmtInt } from "../../shared/format";
import { DataGrid } from "../../shared/grid";
import { errorText } from "../../shared/query";
import type { WorkflowRun } from "../../shared/types";
import { LoadedCard } from "../layout";
import { isActive, taskStatus } from "../../shared/tasks";

export function WorkflowRuns(props: {
  runs: WorkflowRun[] | null;
  error: unknown;
  onRefresh(): Promise<unknown>;
  /** The run whose counts the canvas shows */
  chosen: string | null;
  onChoose(id: string | null): void;
}) {
  const client = useApi();
  const { runs, chosen, onChoose } = props;
  const cancel = useAction(
    async (run: WorkflowRun) => {
      await client.post("/workflows/runs/{run_id}/cancel", { params: { path: { run_id: run.id } } });
      await props.onRefresh();
    },
    { failed: "Not cancelled" },
  );
  return (
    <LoadedCard
      title="Runs"
      description="Newest first. Click one to see its rows along each edge on the canvas."
      data={runs}
      error={errorText(props.error)}
      onRefresh={props.onRefresh}
    >
      {(rows) => (
        <DataGrid<WorkflowRun>
          aria-label="Runs"
          rows={rows}
          rowKey={(r) => r.id}
          empty="No runs yet: Run starts one."
          onRow={(r) => onChoose(r.id === chosen ? null : r.id)}
          rowProps={(r) => ({ className: cn(r.id === chosen && "bg-muted"), "aria-selected": r.id === chosen })}
          columns={[
            {
              key: "status",
              label: "Status",
              render: (r) => (
                <>
                  <StatusLabel status={taskStatus(r.status)} live={r.status === "running"} />
                  <div className="font-mono text-xs text-muted-foreground">{r.schedule_id ? "on its trigger" : "by hand"}</div>
                </>
              ),
            },
            { key: "rows", label: "Rows", align: "right", render: (r) => fmtInt(r.rows) },
            { key: "pages", label: "Pages", align: "right", render: (r) => fmtInt(r.pages) },
            {
              key: "error",
              label: "Error",
              className: "max-w-sm whitespace-normal text-xs text-critical-text",
              render: (r) => r.error ?? "",
            },
            { key: "created", label: "Queued", title: (r) => fmtClock(r.created, true), render: (r) => fmtAgo(r.created) },
            { key: "finished", label: "Finished", title: (r) => (r.finished ? fmtClock(r.finished, true) : undefined), render: (r) => fmtAgo(r.finished) },
            {
              key: "cancel",
              render: (r) =>
                isActive(r) && (
                  <SubmitButton
                    type="button"
                    variant="outline"
                    size="sm"
                    busy={cancel.running?.[0].id === r.id}
                    icon={<X aria-hidden="true" />}
                    onClick={(e) => {
                      e.stopPropagation();
                      void cancel.run(r);
                    }}
                  >
                    Cancel
                  </SubmitButton>
                ),
            },
          ]}
        />
      )}
    </LoadedCard>
  );
}
