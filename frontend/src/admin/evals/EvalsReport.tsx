// The classification report of a run whose outputs are labels (on EvalsRun.tsx,
// its figures on EvalsCompare.tsx too): accuracy and macro F1, each label's
// precision, recall and F1, and the confusion matrix, a row per reference
// label and a column per predicted one, each cell shaded by its share of the
// row: green on the diagonal (right), red off it.

import { cn } from "@/lib/utils";
import { fmtInt } from "../../shared/format";
import { DataGrid, type GridColumn } from "../../shared/grid";
import type { RunDetail } from "../../shared/types";
import { TONE_CLASS, fmtShare, scoreTone } from "./evals";

export type ClassificationReport = NonNullable<RunDetail["classification"]>;
type LabelRow = ClassificationReport["labels"][number];

/** One figure of a report, toned as a score is. */
export function StatChip({ label, value, hint }: { label: string; value: number; hint?: string }) {
  const tone = scoreTone(value);
  return (
    <div className="rounded-md border px-2.5 py-1.5 text-sm">
      <span className="text-xs text-muted-foreground">{label}</span>
      <div className={cn("font-semibold tabular-nums", tone && TONE_CLASS[tone])}>
        {fmtShare(value)}
        {hint && <span className="ml-1.5 text-xs font-normal text-muted-foreground">{hint}</span>}
      </div>
    </div>
  );
}

/** Accuracy and macro F1, as chips. */
export function ReportChips({ report }: { report: ClassificationReport }) {
  return (
    <>
      <StatChip label="accuracy" value={report.accuracy} hint={`of ${fmtInt(report.count)}`} />
      <StatChip label="macro F1" value={report.macro_f1} />
    </>
  );
}

const share = (value: number) => <span className={cn("tabular-nums", TONE_CLASS[scoreTone(value) ?? "critical"])}>{fmtShare(value)}</span>;

const LABEL_COLUMNS: GridColumn<LabelRow>[] = [
  { key: "label", label: "Label", className: "font-medium [overflow-wrap:anywhere] whitespace-normal", render: (r) => r.label },
  { key: "precision", label: "Precision", align: "right", render: (r) => share(r.precision) },
  { key: "recall", label: "Recall", align: "right", render: (r) => share(r.recall) },
  { key: "f1", label: "F1", align: "right", render: (r) => share(r.f1) },
  { key: "support", label: "Examples", align: "right", className: "text-muted-foreground tabular-nums", render: (r) => fmtInt(r.support) },
];

export function ClassificationReportView({ report }: { report: ClassificationReport }) {
  return (
    <section className="grid gap-4 border-b px-6 py-4 lg:grid-cols-2" aria-label="Classification report">
      <div className="grid content-start gap-3">
        <div className="flex flex-wrap items-center gap-2">
          <ReportChips report={report} />
          <span className="text-xs text-muted-foreground">over the examples with a reference</span>
        </div>
        <DataGrid columns={LABEL_COLUMNS} rows={report.labels} rowKey={(r) => r.label} empty="No labels" />
      </div>
      {report.confusion ? (
        <Confusion labels={report.labels.map((l) => l.label)} matrix={report.confusion} />
      ) : (
        <p className="text-sm text-muted-foreground">Too many labels for a confusion matrix.</p>
      )}
    </section>
  );
}

function Confusion({ labels, matrix }: { labels: string[]; matrix: number[][] }) {
  return (
    <div className="min-w-0 overflow-x-auto">
      <table className="text-xs">
        <caption className="mb-2 text-left text-xs text-muted-foreground">Rows: the reference. Columns: what the model said.</caption>
        <thead>
          <tr>
            <th />
            {labels.map((l) => (
              <th key={l} scope="col" className="max-w-24 truncate px-1.5 pb-1 text-center font-medium text-muted-foreground" title={l}>
                {l}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {matrix.map((row, i) => {
            const total = row.reduce((a, b) => a + b, 0);
            return (
              <tr key={labels[i]}>
                <th scope="row" className="max-w-32 truncate pr-2 text-right font-medium text-muted-foreground" title={labels[i]}>
                  {labels[i]}
                </th>
                {row.map((n, j) => (
                  <td
                    key={j}
                    className="h-8 min-w-10 border border-background px-1.5 text-center tabular-nums"
                    style={n ? { backgroundColor: `color-mix(in oklab, var(${i === j ? "--good" : "--critical"}) ${Math.round(15 + (70 * n) / (total || 1))}%, transparent)` } : undefined}
                    title={`${labels[i]} → ${labels[j]}: ${n}`}
                  >
                    {n || ""}
                  </td>
                ))}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
