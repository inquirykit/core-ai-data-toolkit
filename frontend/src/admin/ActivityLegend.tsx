// The legend of the activity chart (ActivityChart.tsx): its series by colour
// and mark, drawn by charts.tsx's ChartLegend.
import { ChartLegend, type LegendItem } from "./charts";
import { EVENT_STYLE } from "./panels";

const ITEMS: LegendItem[] = [
  { label: "OK", color: "var(--series-1)", mark: "rect" },
  { label: "Failed", color: "var(--critical)", mark: "rect" },
  { label: "p95", color: "var(--series-1)", mark: "line" },
  { label: "p50", color: "var(--series-1)", mark: "dashed" },
  ...["load", "evict", "load_failed", "unload"].map((kind): LegendItem => ({ ...EVENT_STYLE[kind], mark: "dot" })),
];

export function ActivityLegend({ className }: { className?: string }) {
  return <ChartLegend items={ITEMS} className={className ? `flex-wrap gap-x-3 gap-y-1 ${className}` : "flex-wrap gap-x-3 gap-y-1"} />;
}
