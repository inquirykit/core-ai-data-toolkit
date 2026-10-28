// The Activity & logs page's chart (ECharts, through shared/echart.tsx):
// requests and latency over a range the reader zooms and pans, model events
// marked beneath, and a navigator strip of the whole week under that. Drag
// across the plot to zoom into a range, scroll to zoom, shift+drag to pan;
// drag the navigator's window or its edges, or click it to move the window
// there (a slider that also drew new windows would leave the window itself
// undraggable: ECharts moves it only by a bar under the strip then). The
// range is the page's (it loads each range from the server, at the
// resolution the width allows): the chart reports the range asked for and
// draws what it is given. So the plot's axis always spans just the range,
// which leaves ECharts' own pan no room: shift+drag pans here instead. The
// range arithmetic is in activity.ts.

import { useEffect, useMemo, useRef, type KeyboardEvent, type PointerEvent } from "react";
import type { ECharts } from "echarts/core";
import { alpha, EChart, TOOLTIP_BOX, tooltipHtml, useChartColors, type ChartEvents, type ChartOption, type TooltipRow } from "../shared/echart";
import { fmtCompact, fmtMs, fmtTime, statusOf } from "../shared/format";
import type { ModelEvent, Timeline } from "../shared/types";
import { clusterByX, fmtBucket, fmtStamp, panRange, zoomRange, type Range } from "./activity";
import { bucketRows, isolated, mid, stackedColumns, useWidth } from "./charts";
import { EVENT_STYLE } from "./panels";

// ----------------------------------------------------------------- layout
const M = { l: 48, r: 16 };
const REQ = { top: 8, height: 140 };
const LAT = { top: REQ.top + REQ.height + 28, height: 96 };
const EVT = { top: LAT.top + LAT.height + 8, height: 18 };
const NAV = { top: EVT.top + EVT.height + 28, height: 40 };
export const CHART_H = NAV.top + NAV.height + 4;

/** Events closer than this many pixels are drawn as one, with their count. */
const CLUSTER_PX = 7;

/** Which kind an event cluster is coloured by: the worst news wins. */
const SEVERITY = ["load_failed", "evict", "limits", "load", "unload"];
const shortKey = (key: string) => (key.length > 36 ? `…${key.slice(-35)}` : key);
const now = () => Date.now() / 1000;

export interface ActivityChartProps {
  /** What the plot spans */
  range: Range;
  data: Timeline | null;
  /** Events inside ``range``, newest first */
  events: ModelEvent[];
  /** The navigator's data, over its own (longer) range */
  context: Timeline | null;
  /** A fetch for a changed range is in flight: the old marks stay, dimmed */
  loading: boolean;
  onRange(range: Range): void;
  /** Double-click or Home: back to the live preset */
  onReset(): void;
}

/** "per 30 s", "per 5 min", "per 2.8 h": how long one point covers. */
function fmtBucketLength(s: number): string {
  if (s < 60) return `${Math.round(s)} s each`;
  if (s < 3600) return `${Math.round(s / 60)} min each`;
  return `${(s / 3600).toFixed(s < 36000 ? 1 : 0)} h each`;
}

function clusterTooltip(cluster: ModelEvent[], range: Range): string {
  const list = [...cluster].sort((a, b) => a.ts - b.ts);
  if (list.length === 1) {
    const [e] = list;
    const { label, color } = statusOf(EVENT_STYLE, e.kind);
    const rows: TooltipRow[] = [[label, fmtTime(e.ts), color]];
    if (e.duration_ms != null) rows.push(["Took", fmtMs(e.duration_ms)]);
    if (e.detail) rows.push(["Detail", e.detail]);
    return tooltipHtml(e.model_key, rows);
  }
  const rows: TooltipRow[] = list.slice(0, 8).map((e) => {
    const { label, color } = statusOf(EVENT_STYLE, e.kind);
    return [`${fmtTime(e.ts)} ${label}`, shortKey(e.model_key), color];
  });
  if (list.length > 8) rows.push([`…and ${list.length - 8} more`, ""]);
  return tooltipHtml(`${list.length} events · ${fmtStamp(list[0].ts, range)} – ${fmtStamp(list[list.length - 1].ts, range)}`, rows);
}

/** Dragging across the plot selects a range (``on``), or, while shift is held, pans it (the plot's inside zoom). */
const selecting = (chart: ECharts | null, on: boolean) =>
  chart?.dispatchAction({ type: "takeGlobalCursor", key: "brush", brushOption: on ? { brushType: "lineX", brushMode: "single" } : { brushType: false } });

/** A time axis label: the day at midnight, else the clock, with seconds for a range of minutes. */
function timeLabel(ms: number, span: number): string {
  const d = new Date(ms);
  if (span > 86400 && d.getHours() === 0 && d.getMinutes() === 0) return d.toLocaleDateString(undefined, { weekday: "short", day: "numeric" });
  return d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: span < 600 ? "2-digit" : undefined });
}

export function ActivityChart(props: ActivityChartProps) {
  const { range, data, events, context, onRange } = props;
  const c = useChartColors();
  const [ref, measured] = useWidth<HTMLDivElement>();
  const pw = Math.max(1, measured - M.l - M.r);
  const chart = useRef<ECharts | null>(null);
  // Shift held: the brush lets go of the plot, which shift+drag then pans (onPointerDown below).
  useEffect(() => {
    const follow = (event: globalThis.KeyboardEvent) => {
      if (event.key === "Shift") selecting(chart.current, event.type === "keyup");
    };
    const reset = () => selecting(chart.current, true);
    addEventListener("keydown", follow);
    addEventListener("keyup", follow);
    addEventListener("blur", reset);
    return () => {
      removeEventListener("keydown", follow);
      removeEventListener("keyup", follow);
      removeEventListener("blur", reset);
    };
  }, []);

  const option = useMemo((): ChartOption => {
    const points = data?.timeline ?? [];
    const bucketS = data?.bucket_s ?? (range.until - range.since) / 60;
    const span = range.until - range.since;
    const ctx = context ? { since: context.since, until: context.until } : range;
    const dim = props.loading ? 0.45 : 1;
    const text = { color: c["muted-foreground"], fontSize: 11.5, fontFamily: "Inter Variable, sans-serif" };
    const grid = (box: { top: number; height: number }, extra: object = {}) => ({ left: M.l, right: M.r, ...box, ...extra });
    const timeAxis = (gridIndex: number, extra: object = {}) => ({
      type: "time",
      gridIndex,
      min: range.since * 1000,
      max: range.until * 1000,
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: { show: false },
      splitLine: { show: gridIndex < 2, lineStyle: { color: c.grid } },
      ...extra,
    });
    const valueAxis = (gridIndex: number, fmt: (v: number) => string, extra: object = {}) => ({
      type: "value",
      gridIndex,
      splitNumber: 2,
      ...extra,
      axisLabel: { ...text, formatter: fmt },
      splitLine: { lineStyle: { color: c.grid } },
      axisLine: { show: false },
    });
    const hidden = (gridIndex: number, extra: object = {}) => ({ gridIndex, show: false, ...extra });
    const contextAxis = (gridIndex: number) => ({ type: "time", ...hidden(gridIndex), min: ctx.since * 1000, max: ctx.until * 1000 });

    // Events in the range, clustered where they crowd.
    const clusters = clusterByX(
      events.filter((e) => e.ts >= range.since && e.ts <= range.until).map((e) => ({ x: ((e.ts - range.since) / span) * pw, e })),
      CLUSTER_PX,
    ).map((cluster) => cluster.map((item) => item.e));
    const ctxMax = Math.max(1, ...(context?.timeline ?? []).map((p) => p.ok + p.client_error + p.error));
    const p95s = points.map((p) => p.p95_ms);
    const empty = data != null && data.totals.requests === 0;

    return {
      animation: false,
      grid: [
        grid(REQ, { tooltip: { trigger: "axis" } }),
        grid(LAT, { tooltip: { trigger: "axis" } }),
        grid(EVT),
        grid(NAV, { show: true, backgroundColor: alpha(c.muted, 0.5), borderWidth: 0 }),
      ],
      xAxis: [
        timeAxis(0),
        timeAxis(1),
        // The time labels go under the event lane.
        timeAxis(2, {
          axisLabel: { ...text, hideOverlap: true, formatter: (ms: number) => timeLabel(ms, span) },
          axisLine: { show: true, lineStyle: { color: c.grid } },
        }),
        contextAxis(3),
        // The navigator's window moves on an axis of its own, so dragging it does not zoom the bars under it.
        contextAxis(3),
      ],
      yAxis: [
        // Requests are counted in whole numbers.
        valueAxis(0, fmtCompact, { minInterval: 1 }),
        valueAxis(1, (v) => fmtMs(v).replace(" ", "")),
        { type: "value", ...hidden(2), min: -1, max: 1 },
        { type: "value", ...hidden(3), min: 0, max: ctxMax * 1.15 },
        { type: "value", ...hidden(3) },
      ],
      axisPointer: { link: [{ xAxisIndex: [0, 1] }], lineStyle: { color: c.axis } },
      tooltip: {
        ...TOOLTIP_BOX,
        trigger: "item",
        formatter: (params: { seriesId?: string; dataIndex: number } | { dataIndex: number }[]) => {
          const first = Array.isArray(params) ? params[0] : params;
          if (!first) return "";
          if (!Array.isArray(params) && params.seriesId === "events") return clusterTooltip(clusters[params.dataIndex], range);
          const p = points[first.dataIndex];
          return p ? tooltipHtml(fmtBucket(p.start, bucketS, range), bucketRows(p)) : "";
        },
      },
      dataZoom: [
        // Scroll to zoom; reset to the whole plot each time, as the page loads the range asked for.
        { type: "inside", id: "plot", xAxisIndex: [0, 1, 2], filterMode: "none", start: 0, end: 100, moveOnMouseMove: false, moveOnMouseWheel: false, zoomOnMouseWheel: true },
        {
          type: "slider",
          id: "nav",
          xAxisIndex: 4,
          filterMode: "none",
          left: M.l,
          right: M.r,
          top: NAV.top,
          height: NAV.height,
          startValue: range.since * 1000,
          endValue: range.until * 1000,
          showDataShadow: false,
          showDetail: false,
          brushSelect: false,
          backgroundColor: "transparent",
          borderColor: "transparent",
          fillerColor: alpha(c["series-1"], 0.08),
          handleStyle: { color: c["series-1"], borderColor: c["series-1"] },
          emphasis: { handleStyle: { borderColor: c["series-1"] } },
        },
      ],
      brush: {
        xAxisIndex: [0, 1],
        brushType: "lineX",
        brushMode: "single",
        transformable: false,
        brushStyle: { color: alpha(c.foreground, 0.08), borderColor: c.axis, borderWidth: 1 },
        outOfBrush: { colorAlpha: 1 },
      },
      graphic: [
        { id: "req-title", type: "text", right: M.r, top: REQ.top + 2, style: { text: `Requests per interval${data ? ` · ${fmtBucketLength(bucketS)}` : ""}`, fill: c["muted-foreground"], fontSize: 11 } },
        { id: "lat-title", type: "text", right: M.r, top: LAT.top - 16, style: { text: "Latency · p95 and p50", fill: c["muted-foreground"], fontSize: 11 } },
        { id: "empty", type: "text", left: "center", top: REQ.top + REQ.height / 2 - 6, invisible: !empty, style: { text: "No requests in this range", fill: c["muted-foreground"], fontSize: 13 } },
      ],
      series: [
        // Requests: OK, then failed stacked on top.
        ...stackedColumns(points, bucketS, c, 3, dim).map((s) => ({ ...s, type: "bar", stack: "requests", barMaxWidth: 24, barCategoryGap: "10%", xAxisIndex: 0, yAxisIndex: 0 })),
        // Latency: p95 solid, p50 dashed and lighter.
        {
          id: "p95",
          type: "line",
          xAxisIndex: 1,
          yAxisIndex: 1,
          data: points.map((p) => [mid(p, bucketS), p.p95_ms]),
          lineStyle: { color: c["series-1"], width: 2, cap: "round", opacity: dim },
          itemStyle: { color: c["series-1"], opacity: dim },
          symbol: "circle",
          symbolSize: (_: unknown, params: { dataIndex: number }) => (isolated(p95s, params.dataIndex) ? 6 : 0),
        },
        {
          id: "p50",
          type: "line",
          xAxisIndex: 1,
          yAxisIndex: 1,
          data: points.map((p) => [mid(p, bucketS), p.p50_ms]),
          lineStyle: { color: c["series-1"], width: 1.5, type: [3, 3], opacity: 0.5 * dim },
          itemStyle: { color: c["series-1"], opacity: 0.6 * dim },
          symbol: "circle",
          symbolSize: 0,
        },
        // Model events, a dot each, a counted one where they crowd.
        {
          id: "events",
          type: "scatter",
          xAxisIndex: 2,
          yAxisIndex: 2,
          data: clusters.map((cluster) => {
            const kind = cluster.map((e) => e.kind).sort((a, b) => SEVERITY.indexOf(a) - SEVERITY.indexOf(b))[0];
            const many = cluster.length > 1;
            return {
              value: [(cluster.reduce((sum, e) => sum + e.ts, 0) / cluster.length) * 1000, 0],
              symbolSize: many ? 14 : 9,
              itemStyle: { color: c.of(statusOf(EVENT_STYLE, kind).color), borderColor: c.card, borderWidth: 1.5 },
              label: { show: many, formatter: cluster.length > 9 ? "9+" : String(cluster.length), color: "#fff", fontSize: 9, fontWeight: 600 },
            };
          }),
        },
        // The navigator: the whole week's requests.
        {
          id: "context",
          type: "bar",
          xAxisIndex: 3,
          yAxisIndex: 3,
          silent: true,
          barMaxWidth: 8,
          itemStyle: { color: c["series-1"], opacity: 0.45, borderRadius: 1 },
          data: (context?.timeline ?? []).map((p) => [mid(p, context!.bucket_s), p.ok + p.client_error + p.error]),
        },
        // What the navigator's window moves along: the week, drawn by nothing.
        { id: "window", type: "line", xAxisIndex: 4, yAxisIndex: 4, silent: true, symbol: "none", lineStyle: { opacity: 0 }, data: [[ctx.since * 1000, 0], [ctx.until * 1000, 0]] },
      ],
    };
  }, [range, data, events, context, props.loading, c, pw]);

  const events$: ChartEvents = {
    // A scroll or shift+drag on the plot: the share of the range it now shows. The navigator: the time it spans.
    datazoom: (event, chart) => {
      const id = event.dataZoomId ?? event.batch?.[0]?.dataZoomId ?? "";
      const zooms = chart.getOption().dataZoom as { id: string; start: number; end: number; startValue: number; endValue: number }[];
      if (String(id).includes("nav")) {
        const nav = zooms.find((z) => z.id === "nav");
        if (nav) onRange({ since: nav.startValue / 1000, until: nav.endValue / 1000 });
        return;
      }
      const plot = zooms.find((z) => z.id === "plot");
      if (!plot) return;
      const span = range.until - range.since;
      onRange({ since: range.since + (span * plot.start) / 100, until: range.since + (span * plot.end) / 100 });
    },
    // A range dragged across the plot: zoom into it, and start a new selection.
    brushEnd: (event, chart) => {
      const [a, b] = (event.areas?.[0]?.coordRange ?? []) as number[];
      chart.dispatchAction({ type: "brush", areas: [] });
      if (a != null && b != null && b - a > 1000) onRange({ since: a / 1000, until: b / 1000 });
    },
  };

  // Shift+drag on the plot (above the navigator): the range follows the pointer.
  const panning = useRef<number | null>(null);
  const onPointerDown = (event: PointerEvent<HTMLDivElement>) => {
    const y = event.clientY - event.currentTarget.getBoundingClientRect().top;
    if (!event.shiftKey || event.button !== 0 || y > EVT.top + EVT.height) return;
    panning.current = event.clientX;
    event.currentTarget.setPointerCapture(event.pointerId);
  };
  const onPointerMove = (event: PointerEvent) => {
    if (panning.current == null) return;
    const dx = event.clientX - panning.current;
    panning.current = event.clientX;
    onRange(panRange(range, (-dx / pw) * (range.until - range.since), now()));
  };
  const stopPanning = () => {
    panning.current = null;
  };

  const onKeyDown = (event: KeyboardEvent) => {
    const span = range.until - range.since;
    const step = span * (event.shiftKey ? 0.5 : 0.1);
    const actions: Record<string, () => void> = {
      ArrowLeft: () => onRange(panRange(range, -step, now())),
      ArrowRight: () => onRange(panRange(range, step, now())),
      "+": () => onRange(zoomRange(range, 0.5, 0.5, now())),
      "=": () => onRange(zoomRange(range, 0.5, 0.5, now())),
      "-": () => onRange(zoomRange(range, 2, 0.5, now())),
      Home: props.onReset,
      "0": props.onReset,
    };
    const action = actions[event.key];
    if (action) {
      event.preventDefault();
      action();
    }
  };

  const label =
    `Requests and latency from ${fmtStamp(range.since, range)} to ${fmtStamp(range.until, range)}` +
    (data ? `: ${data.totals.requests} requests, p95 ${fmtMs(data.totals.p95_ms)}` : "");

  return (
    <div
      ref={ref}
      tabIndex={0}
      onKeyDown={onKeyDown}
      onDoubleClick={props.onReset}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={stopPanning}
      onPointerCancel={stopPanning}
      className="w-full min-w-0 touch-none rounded-md outline-none select-none focus-visible:ring-[3px] focus-visible:ring-ring/50"
    >
      <EChart
        option={option}
        events={events$}
        onReady={(instance) => {
          chart.current = instance;
          selecting(instance, true);
        }}
        height={CHART_H}
        aria-label={label}
      />
    </div>
  );
}
