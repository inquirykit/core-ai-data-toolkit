// Two runs of a dataset side by side in the Evals section, example by
// example, with each run's summary and the judge's verdict where it
// compared them.

import { useState } from "react";
import { ChevronRight, FlaskConical } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Card, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { cn } from "@/lib/utils";
import type { ApiClient } from "../shared/api";
import { errorText } from "../shared/query";
import { EmptyCard } from "../shared/components";
import { fmtInt, fmtMs } from "../shared/format";
import type { Comparison, Dataset } from "../shared/types";
import { evalsHref, fmtScore, inputPreview, metricLabel, outputPreview, referencePreview, scoreTone } from "./evals";
import { MetricChip, TONE_CLASS } from "./EvalsRun";
import { DetailSkeleton } from "./layout";

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
        <Table>
          <TableHeader>
            <TableRow className="hover:bg-transparent">
              <TableHead>Input</TableHead>
              <TableHead>A · {data.a.name}</TableHead>
              <TableHead>B · {data.b.name}</TableHead>
              <TableHead>Verdict</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {data.rows.map((row) => {
              const verdict = row.a.scores.find((s) => s.metric === pairMetric("a")) ?? row.b?.scores.find((s) => s.metric === pairMetric("b"));
              const fromA = verdict?.metric === pairMetric("a");
              const label = verdict?.label ? (fromA ? { win: "A", loss: "B", tie: "tie" } : { win: "B", loss: "A", tie: "tie" })[verdict.label] : null;
              const isOpen = open === row.example_id;
              return (
                <TableRow key={row.example_id} className="cursor-pointer align-top" onClick={() => setOpen(isOpen ? null : row.example_id)}>
                  <TableCell className="max-w-xs whitespace-normal">
                    <span className={cn("[overflow-wrap:anywhere]", !isOpen && "line-clamp-3")}>{inputPreview(row.input, 4000)}</span>
                    {row.reference != null && (
                      <div className={cn("mt-1 text-xs text-muted-foreground [overflow-wrap:anywhere]", !isOpen && "line-clamp-2")}>reference: {referencePreview(row.reference, 4000)}</div>
                    )}
                  </TableCell>
                  {[row.a, row.b].map((side, i) => (
                    <TableCell key={i} className="max-w-sm whitespace-normal">
                      {!side ? (
                        <span className="text-muted-foreground">not run</span>
                      ) : side.status === "ok" ? (
                        <span className={cn("[overflow-wrap:anywhere]", !isOpen && "line-clamp-4")}>{outputPreview(side.output, 8000)}</span>
                      ) : (
                        <span className="text-critical-text [overflow-wrap:anywhere]">{side.error ?? "failed"}</span>
                      )}
                      {side && (
                        <div className="mt-1 flex flex-wrap gap-x-2 text-xs text-muted-foreground tabular-nums">
                          <span>{fmtMs(side.duration_ms)}</span>
                          {side.scores
                            .filter((s) => !s.metric.startsWith("judge_vs_"))
                            .map((s) => (
                              <span key={s.id} className={cn(scoreTone(s.value) && TONE_CLASS[scoreTone(s.value)!])}>
                                {metricLabel(s.metric)} {fmtScore(s.value, s.metric, s.label)}
                              </span>
                            ))}
                        </div>
                      )}
                    </TableCell>
                  ))}
                  <TableCell className="whitespace-normal">
                    {label ? (
                      <>
                        <Badge variant={label === "tie" ? "secondary" : "default"}>{label === "tie" ? "Tie" : `${label} better`}</Badge>
                        {verdict?.rationale && <p className={cn("mt-1 text-xs text-muted-foreground [overflow-wrap:anywhere]", !isOpen && "line-clamp-3")}>{verdict.rationale}</p>}
                      </>
                    ) : (
                      <span className="text-xs text-muted-foreground">Not judged: open a run and judge it against the other</span>
                    )}
                  </TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      </div>
    </Card>
  );
}
