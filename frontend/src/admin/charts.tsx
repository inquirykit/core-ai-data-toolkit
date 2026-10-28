// The Overview's request charts (ECharts, through shared/echart.tsx): requests
// per interval as stacked OK and failed columns, p95 latency as a line, and
// the same figures as a table; and what the Activity chart shares with them:
// the legend, a bucket's tooltip rows, the box width.
import { useLayoutEffect, useMemo, useRef, useState, type RefObject } from "react";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { cn } from "@/lib/utils";
import { EChart, TOOLTIP_BOX, tooltipHtml, useChartColors, type ChartColors, type ChartOption, type TooltipRow } from "../shared/echart";
import { fmtClock, fmtCompact, fmtDuration, fmtInt, fmtMs } from "../shared/format";
import type { Stats, TimelinePoint } from "../shared/types";

/** A series' color swatch, in the legend and the tooltip. */
function Swatch({ color, className }: { color: string; className?: string }) {
  return <span className={cn("size-2.5 shrink-0 rounded-[3px]", className)} style={{ background: color }} aria-hidden="true" />;
}

/** How a series is drawn: a column (its swatch), a line, a dashed line, or a dot. */
export type LegendMark = "rect" | "line" | "dashed" | "dot";
export interface LegendItem {
  label: string;
  color: string;
  /** "rect" when left out */
  mark?: LegendMark;
}

/** The series a chart shows, by color and mark; a ``[label, color]`` pair is a column's. */
export function ChartLegend({ items, className }: { items: (LegendItem | [label: string, color: string])[]; className?: string }) {
  return (
    <div className={cn("flex items-center gap-4 text-xs text-muted-foreground", className)}>
      {items.map((item) => {
        const { label, color, mark = "rect" } = Array.isArray(item) ? { label: item[0], color: item[1] } : item;
        return (
          <span className="flex items-center gap-1.5" key={label}>
            {mark === "rect" ? <Swatch color={color} /> : <Glyph mark={mark} color={color} />}
            {label}
          </span>
        );
      })}
    </div>
  );
}

/** A line, dashed line or dot as the chart draws it, legend-sized. */
function Glyph({ mark, color }: { mark: Exclude<LegendMark, "rect">; color: string }) {
  return (
    <svg width={14} height={10} aria-hidden="true" className="shrink-0">
      {mark === "line" && <line x1={0} x2={14} y1={5} y2={5} stroke={color} strokeWidth={2} strokeLinecap="round" />}
      {mark === "dashed" && <line x1={0} x2={14} y1={5} y2={5} stroke={color} strokeOpacity={0.5} strokeWidth={1.5} strokeDasharray="3 3" />}
      {mark === "dot" && <circle cx={7} cy={5} r={4} fill={color} />}
    </svg>
  );
}

// ------------------------------------------------------------------ helpers
const failedIn = (p: TimelinePoint) => p.client_error + p.error;

/** What an interval's tooltip lists, in the Overview and in Activity & logs. */
export function bucketRows(p: TimelinePoint): TooltipRow[] {
  return [
    ["OK", fmtInt(p.ok), "var(--series-1)"],
    ["Failed", fmtInt(failedIn(p)), "var(--critical)"],
    ["Client errors (4xx)", fmtInt(p.client_error)],
    ["Server errors (5xx)", fmtInt(p.error)],
    ["p50 latency", fmtMs(p.p50_ms)],
    ["p95 latency", fmtMs(p.p95_ms)],
  ];
}

function bucketTitle(p: TimelinePoint, bucketS: number, withDay: boolean): string {
  return `${fmtClock(p.start, withDay)} – ${fmtClock(p.start + bucketS, withDay)}`;
}

/** Whether value ``i`` has no neighbour: a line would not show it, so it gets a dot. */
export const isolated = (values: (number | null)[], i: number) => values[i] != null && values[i - 1] == null && values[i + 1] == null;

/** The element's width, kept current as it resizes. */
export function useWidth<T extends HTMLElement>(): [RefObject<T | null>, number] {
  const ref = useRef<T>(null);
  const [width, setWidth] = useState(0);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    setWidth(el.clientWidth);
    const observer = new ResizeObserver(([entry]) => setWidth(entry.contentRect.width));
    observer.observe(el);
    return () => observer.disconnect();
  }, []);
  return [ref, width];
}

// ------------------------------------------------------------------ charts
const CHART_H = 200;

/**
 * What the Overview's two charts share: a time axis over the window with three labels, a value axis with three
 * gridlines, a tooltip per interval, and a line saying when there is nothing to show.
 */
function frame(stats: Stats, c: ChartColors, fmtTick: (v: number) => string, empty: string | null, minInterval = 0): ChartOption {
  const withDay = stats.window === "7d";
  const { timeline, bucket_s: bucketS } = stats;
  const start = timeline.length ? timeline[0].start : 0;
  const end = timeline.length ? timeline[timeline.length - 1].start + bucketS : 1;
  const text = { color: c["muted-foreground"], fontSize: 11.5, fontFamily: "Inter Variable, sans-serif" };
  return {
    animationDuration: 700,
    grid: { left: 44, right: 14, top: 16, bottom: 26 },
    xAxis: {
      type: "time",
      min: start * 1000,
      max: end * 1000,
      splitNumber: 2,
      axisLine: { lineStyle: { color: c.axis } },
      axisTick: { show: false },
      splitLine: { show: false },
      axisLabel: { ...text, hideOverlap: true, formatter: (ms: number) => fmtClock(ms / 1000, withDay) },
    },
    yAxis: {
      type: "value",
      splitNumber: 2,
      minInterval,
      axisLabel: { ...text, formatter: fmtTick },
      splitLine: { lineStyle: { color: c.grid } },
    },
    tooltip: {
      ...TOOLTIP_BOX,
      trigger: "axis",
      axisPointer: { type: "shadow", shadowStyle: { color: c.foreground, opacity: 0.05 } },
      formatter: (params: { dataIndex: number }[]) => {
        const p = timeline[params[0]?.dataIndex];
        return p ? tooltipHtml(bucketTitle(p, bucketS, withDay), bucketRows(p)) : "";
      },
    },
    graphic: [{ id: "empty", type: "text", left: "center", top: "middle", invisible: !empty, style: { text: empty ?? "", fill: c["muted-foreground"], fontSize: 13 } }],
  };
}

/** An interval's point on the time axis: its middle, in ms. */
export const mid = (p: TimelinePoint, bucketS: number) => (p.start + bucketS / 2) * 1000;

/**
 * The OK and failed columns of each interval, failed stacked on OK, as two bar series (the chart adds their axes
 * and widths). The column's top is rounded, whichever segment it is.
 */
export function stackedColumns(points: TimelinePoint[], bucketS: number, c: ChartColors, radius: number, opacity = 1) {
  const top = [radius, radius, 0, 0];
  return [
    {
      id: "ok",
      name: "OK",
      itemStyle: { color: c["series-1"], opacity },
      data: points.map((p) => ({ value: [mid(p, bucketS), p.ok], itemStyle: { borderRadius: failedIn(p) ? 0 : top } })),
    },
    { id: "failed", name: "Failed", itemStyle: { color: c.critical, opacity, borderRadius: top }, data: points.map((p) => [mid(p, bucketS), failedIn(p)]) },
  ];
}

export function RequestsChart({ stats }: { stats: Stats }) {
  const c = useChartColors();
  const t = stats.totals;
  const option = useMemo((): ChartOption => {
    const totals = stats.timeline.map((p) => p.ok + failedIn(p));
    const bar = { type: "bar", stack: "requests", barMaxWidth: 24, barCategoryGap: "10%" };
    return {
      ...frame(stats, c, fmtCompact, totals.some(Boolean) ? null : "No requests in this window", 1),
      series: [
        ...stackedColumns(stats.timeline, stats.bucket_s, c, 4).map((s) => ({ ...s, ...bar })),
      ],
    };
  }, [stats, c]);
  return (
    <EChart
      option={option}
      height={CHART_H}
      aria-label={`Requests per ${fmtDuration(stats.bucket_s)} interval: ${fmtInt(t.ok)} OK and ${fmtInt(t.client_errors + t.errors)} failed`}
    />
  );
}

export function LatencyChart({ stats }: { stats: Stats }) {
  const c = useChartColors();
  const option = useMemo((): ChartOption => {
    const values = stats.timeline.map((p) => p.p95_ms);
    return {
      ...frame(stats, c, (v) => fmtMs(v).replace(" ", ""), values.some((v) => v != null) ? null : "No requests in this window"),
      series: [
        {
          id: "p95",
          type: "line",
          name: "p95",
          data: stats.timeline.map((p) => [mid(p, stats.bucket_s), p.p95_ms]),
          lineStyle: { color: c["series-1"], width: 2, cap: "round" },
          itemStyle: { color: c["series-1"] },
          symbol: "circle",
          // A point with no neighbour would not show as a line: it gets a dot.
          symbolSize: (_: unknown, params: { dataIndex: number }) => (isolated(values, params.dataIndex) ? 6 : 0),
          // The last value, above and left of its point, inside the plot.
          endLabel: {
            show: true,
            color: c["muted-foreground"],
            fontSize: 12,
            align: "right",
            verticalAlign: "bottom",
            offset: [-2, -8],
            formatter: (p: { value: [number, number | null] }) => fmtMs(p.value[1]),
          },
        },
      ],
      tooltip: { ...(frame(stats, c, fmtCompact, null).tooltip as object), axisPointer: { type: "line", lineStyle: { color: c.axis } } },
    };
  }, [stats, c]);
  return <EChart option={option} height={CHART_H} aria-label={`p95 latency per ${fmtDuration(stats.bucket_s)} interval; overall ${fmtMs(stats.totals.p95_ms)}`} />;
}

export function TimelineTable({ stats }: { stats: Stats }) {
  const rows = stats.timeline.filter((p) => p.ok + failedIn(p));
  const withDay = stats.window === "7d";
  return (
    <Table>
      <TableHeader>
        <TableRow className="hover:bg-transparent">
          <TableHead>Interval</TableHead>
          <TableHead className="text-right">OK</TableHead>
          <TableHead className="text-right">Client errors</TableHead>
          <TableHead className="text-right">Server errors</TableHead>
          <TableHead className="text-right">p50</TableHead>
          <TableHead className="text-right">p95</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {rows.length ? (
          rows.map((p) => (
            <TableRow key={p.start}>
              <TableCell>{bucketTitle(p, stats.bucket_s, withDay)}</TableCell>
              <TableCell className="text-right tabular-nums">{fmtInt(p.ok)}</TableCell>
              <TableCell className="text-right tabular-nums">{fmtInt(p.client_error)}</TableCell>
              <TableCell className="text-right tabular-nums">{fmtInt(p.error)}</TableCell>
              <TableCell className="text-right tabular-nums">{fmtMs(p.p50_ms)}</TableCell>
              <TableCell className="text-right tabular-nums">{fmtMs(p.p95_ms)}</TableCell>
            </TableRow>
          ))
        ) : (
          <TableRow className="hover:bg-transparent">
            <TableCell colSpan={6} className="h-20 text-center text-muted-foreground">
              No requests in this window
            </TableCell>
          </TableRow>
        )}
      </TableBody>
    </Table>
  );
}
