// The Activity & logs section's requests of the range, as a table; a
// request's outcome's label and colour (STATUS_STYLE) is also the trace
// dialog's.
import type { KeyboardEvent } from "react";
import { Badge } from "@/components/ui/badge";
import { StatusLabel } from "../../shared/components";
import { fmtMs, fmtTime, statusOf } from "../../shared/format";
import { DataGrid, type GridColumn } from "../../shared/grid";
import type { Status } from "../../shared/types";
import type { RequestItem } from "./activity";

/** A request's outcome: its label and colour, here and in the trace dialog. */
export const STATUS_STYLE: Record<string, Status> = {
  ok: { label: "OK", color: "var(--good)" },
  client_error: { label: "Client error", color: "var(--serious)" },
  error: { label: "Server error", color: "var(--critical)" },
};

/**
 * The requests of Activity & logs. A row opens its input and output
 * (onOpen with its trace id); rows found by searching the traces also show
 * the first words of the request.
 */
export function RecentRequests({ requests, withPreview, onOpen }: { requests: RequestItem[]; withPreview: boolean; onOpen(traceId: string): void }) {
  const columns: GridColumn<RequestItem>[] = [
    { key: "time", label: "Time", className: "text-muted-foreground tabular-nums", title: (r) => new Date(r.ts * 1000).toLocaleString(), render: (r) => fmtTime(r.ts) },
    {
      key: "model",
      label: "Model",
      className: "whitespace-normal",
      render: (r) => (
        <>
          <span className="font-mono text-[13px] [overflow-wrap:anywhere]">{r.model_id}</span>
          <div className="text-xs text-muted-foreground">
            {r.task}
            {r.tags.map((tag) => (
              <Badge key={tag} variant="secondary" className="ml-1.5 px-1.5 py-0 text-[11px]">
                {tag}
              </Badge>
            ))}
          </div>
        </>
      ),
    },
    {
      key: "status",
      label: "Status",
      className: "whitespace-normal",
      render: (r) => (
        <>
          <StatusLabel status={statusOf(STATUS_STYLE, r.status)} className="whitespace-nowrap" />
          {r.error && <div className="mt-0.5 max-w-64 text-xs text-muted-foreground [overflow-wrap:anywhere]">{r.error}</div>}
        </>
      ),
    },
    {
      key: "duration",
      label: "Duration",
      align: "right",
      render: (r) => (
        <>
          {fmtMs(r.duration_ms)}
          {r.cold && <div className="text-xs text-muted-foreground">cold start</div>}
        </>
      ),
    },
  ];
  if (withPreview) {
    columns.push({
      key: "request",
      label: "Request",
      className: "max-w-md whitespace-normal",
      render: (r) => <span className="line-clamp-2 text-sm [overflow-wrap:anywhere]">{r.preview || <span className="text-muted-foreground">(no text)</span>}</span>,
    });
  }
  // A row opens its trace by click or Enter; one without a trace says why not.
  const rowProps = (r: RequestItem) => {
    const open = r.trace_id ? () => onOpen(r.trace_id!) : undefined;
    return {
      className: open && "cursor-pointer",
      tabIndex: open ? 0 : undefined,
      onClick: open,
      onKeyDown: open && ((e: KeyboardEvent) => e.key === "Enter" && open()),
      title: open ? "Input and output" : "Recorded before tracing: no input or output",
    };
  };
  return <DataGrid columns={columns} rows={requests} rowKey={(r) => r.key} rowProps={rowProps} empty={withPreview ? "No requests match" : "No requests yet"} />;
}
