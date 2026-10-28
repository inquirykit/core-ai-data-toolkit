// The "By model" tab of Activity & logs: each model's figures over the range shown.
import { fmtAgo, fmtInt, fmtMs, fmtPct } from "../shared/format";
import { DataGrid, type GridColumn } from "../shared/grid";
import type { Timeline } from "../shared/types";

type ModelFigures = Timeline["models"][number];

const failedOf = (m: ModelFigures): number => m.errors + m.client_errors;

const COLUMNS: GridColumn<ModelFigures>[] = [
  {
    key: "model",
    label: "Model",
    className: "whitespace-normal",
    render: (m) => (
      <>
        <span className="font-mono text-[13px] [overflow-wrap:anywhere]">{m.model_id}</span>
        <div className="text-xs text-muted-foreground">{m.task}</div>
      </>
    ),
  },
  { key: "requests", label: "Requests", align: "right", render: (m) => fmtInt(m.requests) },
  {
    key: "failed",
    label: "Failed",
    align: "right",
    className: (m) => (failedOf(m) > 0 ? "text-critical-text" : undefined),
    render: (m) => (
      <>
        {fmtInt(failedOf(m))}
        {m.requests > 0 && failedOf(m) > 0 && <div className="text-xs text-muted-foreground">{fmtPct(failedOf(m) / m.requests)}</div>}
      </>
    ),
  },
  { key: "cold", label: "Cold starts", align: "right", render: (m) => fmtInt(m.cold_starts) },
  { key: "p50", label: "p50", align: "right", render: (m) => fmtMs(m.p50_ms) },
  { key: "p95", label: "p95", align: "right", render: (m) => fmtMs(m.p95_ms) },
  { key: "last", label: "Last request", align: "right", className: "text-muted-foreground", render: (m) => fmtAgo(m.last_request) },
];

export function ModelsTable({ data }: { data: Timeline | null }) {
  return <DataGrid columns={COLUMNS} rows={data?.models ?? []} rowKey={(m) => m.model_key} empty="No requests in this range" />;
}
