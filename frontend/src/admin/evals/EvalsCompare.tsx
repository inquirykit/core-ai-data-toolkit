// Two runs of a dataset side by side in the Evals section, example by
// example, with each run's summary and the judge's verdict where it
// compared them.

import { useState } from "react";
import { ChevronRight, FlaskConical } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Card, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { cn } from "@/lib/utils";
import type { ApiClient } from "../../shared/api";
import { errorText } from "../../shared/query";
import { EmptyCard } from "../../shared/components";
import { fmtInt, fmtMs } from "../../shared/format";
import { DataGrid, type GridColumn } from "../../shared/grid";
import type { Comparison, Dataset } from "../../shared/types";
import { evalsHref, fmtScore, inputPreview, metricLabel, outputPreview, referencePreview, scoreTone } from "./evals";
import { MetricChip, TONE_CLASS } from "./EvalsRun";
import { DetailSkeleton } from "../layout";

export function CompareView({ client, dataset, runs }: { client: ApiClient; dataset: Dataset; runs: [string, string] }) {
  const [open, setOpen] = useState<string | null>(null);
  const query = client.useQuery("get", "/admin/runs/compare/{run_a}/{run_b}", { params: { path: { run_a: runs[0], run_b: runs[1] } } });
  const { data } = query;
  const error = errorText(query.error);
  if (error) return <EmptyCard icon={FlaskConical} title="Could not compare" body={error} />;
  if (!data) return <DetailSkeleton />;
  const pairMetric = (side: "a" | "b") => `judge_vs_${side === "a" ? data.b.id : data.a.id}`;
  const sides: ["a" | "b", Comparison["a"]][] = [
    ["a", data.a],
    ["b", data.b],
  ];
  /** The judge's verdict on a row, from whichever run was judged against the other, worded for this side. */
  const verdictOf = (row: Comparison["rows"][number]) => {
    const verdict = row.a.scores.find((s) => s.metric === pairMetric("a")) ?? row.b?.scores.find((s) => s.metric === pairMetric("b"));
    const fromA = verdict?.metric === pairMetric("a");
    const label = verdict?.label ? (fromA ? { win: "A", loss: "B", tie: "tie" } : { win: "B", loss: "A", tie: "tie" })[verdict.label] : null;
    return { label, rationale: verdict?.rationale };
  };
  // The opened row shows whole, the others cut to a few lines.
  const columns: GridColumn<Comparison["rows"][number]>[] = [
    {
      key: "input",
      label: "Input",
      className: "max-w-xs whitespace-normal",
      render: (row) => (
        <>
          <span className={cn("[overflow-wrap:anywhere]", open !== row.example_id && "line-clamp-3")}>{inputPreview(row.input, 4000)}</span>
          {row.reference != null && (
            <div className={cn("mt-1 text-xs text-muted-foreground [overflow-wrap:anywhere]", open !== row.example_id && "line-clamp-2")}>
              reference: {referencePreview(row.reference, 4000)}
            </div>
          )}
        </>
      ),
    },
    ...sides.map(
      ([side, run]): GridColumn<Comparison["rows"][number]> => ({
        key: side,
        label: `${side.toUpperCase()} · ${run.name}`,
        className: "max-w-sm whitespace-normal",
        render: (row) => {
          const item = row[side];
          return (
            <>
              {!item ? (
                <span className="text-muted-foreground">not run</span>
              ) : item.status === "ok" ? (
                <span className={cn("[overflow-wrap:anywhere]", open !== row.example_id && "line-clamp-4")}>{outputPreview(item.output, 8000)}</span>
              ) : (
                <span className="text-critical-text [overflow-wrap:anywhere]">{item.error ?? "failed"}</span>
              )}
              {item && (
                <div className="mt-1 flex flex-wrap gap-x-2 text-xs text-muted-foreground tabular-nums">
                  <span>{fmtMs(item.duration_ms)}</span>
                  {item.scores
                    .filter((s) => !s.metric.startsWith("judge_vs_"))
                    .map((s) => (
                      <span key={s.id} className={cn(scoreTone(s.value) && TONE_CLASS[scoreTone(s.value)!])}>
                        {metricLabel(s.metric)} {fmtScore(s.value, s.metric, s.label)}
                      </span>
                    ))}
                </div>
              )}
            </>
          );
        },
      }),
    ),
    {
      key: "verdict",
      label: "Verdict",
      className: "whitespace-normal",
      render: (row) => {
        const { label, rationale } = verdictOf(row);
        return label ? (
          <>
            <Badge variant={label === "tie" ? "secondary" : "default"}>{label === "tie" ? "Tie" : `${label} better`}</Badge>
            {rationale && <p className={cn("mt-1 text-xs text-muted-foreground [overflow-wrap:anywhere]", open !== row.example_id && "line-clamp-3")}>{rationale}</p>}
          </>
        ) : (
          <span className="text-xs text-muted-foreground">Not judged: open a run and judge it against the other</span>
        );
      },
    },
  ];
  return (
    <Card className="gap-0 overflow-hidden py-0">
      <CardHeader className="border-b py-4">
        <CardTitle className="flex flex-wrap items-center gap-2">
          <a href={evalsHref(dataset.id)} className="text-muted-foreground hover:text-foreground">
            {dataset.name}
          </a>
          <ChevronRight className="size-4 text-muted-foreground" aria-hidden="true" />
          <a href={evalsHref(dataset.id, data.a.id)} className="hover:underline">
            {data.a.name}
          </a>
          <span className="text-muted-foreground">vs</span>
          <a href={evalsHref(dataset.id, data.b.id)} className="hover:underline">
            {data.b.name}
          </a>
        </CardTitle>
        <CardDescription>{fmtInt(data.rows.length)} examples side by side</CardDescription>
      </CardHeader>
      <div className="grid gap-3 border-b px-6 py-3 sm:grid-cols-2">
        {sides.map(([side, run]) => (
          <div key={side} className="min-w-0">
            <div className="text-xs font-medium text-muted-foreground">
              {side.toUpperCase()} · <span className="font-mono">{run.model_id}</span>
            </div>
            <div className="mt-1 flex flex-wrap gap-2">
              {Object.keys(run.summary).length ? (
                Object.keys(run.summary)
                  .sort()
                  .map((m) => <MetricChip key={m} metric={m} summary={run.summary[m]} runs={[data.a, data.b]} />)
              ) : (
                <span className="text-sm text-muted-foreground">No scores yet</span>
              )}
            </div>
          </div>
        ))}
      </div>
      <div className="max-h-[70svh] overflow-y-auto">
        <DataGrid
          columns={columns}
          rows={data.rows}
          rowKey={(row) => row.example_id}
          rowProps={() => ({ className: "align-top" })}
          onRow={(row) => setOpen(open === row.example_id ? null : row.example_id)}
          empty="No examples"
        />
      </div>
    </Card>
  );
}
