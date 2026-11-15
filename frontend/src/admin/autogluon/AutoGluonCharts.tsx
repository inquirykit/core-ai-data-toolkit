// The AutoGluon predictor page's charts: each model of the leaderboard by its
// score and how long it takes to predict, the columns' importance, and a
// series' recent history with its forecast and band.

import { useMemo } from "react";
import { EChart, TOOLTIP_BOX, alpha, tooltipHtml, useChartColors, type ChartOption } from "../../shared/echart";
import { fmtCompact, fmtMs } from "../../shared/format";
import { ChartLegend } from "../charts";
import { fmtScore, type Report, type SeriesPoint } from "./autogluon";

const text = (c: ReturnType<typeof useChartColors>) => ({ color: c["muted-foreground"], fontSize: 11.5, fontFamily: c.font });

/**
 * Each model of the leaderboard as a point: its validation score up (as a positive error when lower is better, so
 * down is better then), and how long it takes to predict the validation rows across, on a log scale. The best
 * model is ringed.
 */
export function LeaderboardChart(props: { rows: Record<string, unknown>[]; best: string; lowerIsBetter: boolean }) {
  const c = useChartColors();
  const points = useMemo(
    () =>
      props.rows
        .map((r) => ({ model: String(r.model), score: Number(r.score_val), seconds: Number(r.pred_time_val) }))
        .filter((p) => Number.isFinite(p.score) && Number.isFinite(p.seconds) && p.seconds > 0)
        .map((p) => ({ ...p, score: props.lowerIsBetter ? Math.abs(p.score) : p.score })),
    [props.rows, props.lowerIsBetter],
  );
  const option = useMemo((): ChartOption => ({
    animationDuration: 400,
    grid: { left: 8, right: 16, top: 16, bottom: 4, containLabel: true },
    xAxis: {
      type: "log",
      name: "prediction time",
      nameLocation: "middle",
      nameGap: 26,
      nameTextStyle: text(c),
      axisLabel: { ...text(c), formatter: (v: number) => fmtMs(v * 1000) },
      splitLine: { lineStyle: { color: c.grid } },
      axisLine: { lineStyle: { color: c.axis } },
    },
    yAxis: {
      type: "value",
      scale: true,
      splitNumber: 4,
      axisLabel: { ...text(c), formatter: (v: number) => fmtScore(v) },
      splitLine: { lineStyle: { color: c.grid } },
    },
    tooltip: {
      ...TOOLTIP_BOX,
      trigger: "item",
      formatter: (p: { dataIndex: number }) => {
        const m = points[p.dataIndex];
        return m ? tooltipHtml(m.model, [["Validation score", fmtScore(m.score)], ["Prediction time", fmtMs(m.seconds * 1000)]]) : "";
      },
    },
    series: [
      {
        type: "scatter",
        data: points.map((p) => ({
          value: [p.seconds, p.score],
          symbolSize: p.model === props.best ? 14 : 9,
          itemStyle: p.model === props.best ? { color: c["series-1"], borderColor: c.foreground, borderWidth: 2 } : { color: alpha(c["series-1"], 0.6) },
        })),
        // Only the best is named: models of equal score sit on top of each other; the tooltip names the rest.
        label: { show: true, position: "right", formatter: (p: { dataIndex: number }) => (points[p.dataIndex]?.model === props.best ? props.best : ""), ...text(c), fontSize: 10.5 },
      },
    ],
  }), [c, points, props.best]);
  if (!points.length) return null;
  return (
    <div className="grid gap-1">
      <EChart option={option} height={240} aria-label={`The models by validation score and prediction time; the best is ${props.best}`} />
      <p className="text-xs text-muted-foreground">
        Each model by its validation score ({props.lowerIsBetter ? "lower is better" : "higher is better"}) and the time it takes to predict the validation rows. The ringed one is the best, which is served.
      </p>
    </div>
  );
}

const MAX_FEATURES = 20;

/** How much each column matters: how much the score drops when its values are shuffled, top 20. */
export function ImportanceChart({ rows }: { rows: NonNullable<Report["importance"]> }) {
  const c = useChartColors();
  const shown = useMemo(() => [...rows].sort((a, b) => (b.importance ?? 0) - (a.importance ?? 0)).slice(0, MAX_FEATURES).reverse(), [rows]);
  const option = useMemo((): ChartOption => ({
    animationDuration: 400,
    grid: { left: 8, right: 16, top: 4, bottom: 4, containLabel: true },
    xAxis: { type: "value", splitNumber: 3, axisLabel: { ...text(c), formatter: (v: number) => (v ? fmtScore(v) : "0") }, splitLine: { lineStyle: { color: c.grid } } },
    yAxis: { type: "category", data: shown.map((r) => r.feature), axisLabel: { ...text(c), width: 140, overflow: "truncate" }, axisTick: { show: false }, axisLine: { lineStyle: { color: c.axis } } },
    tooltip: {
      ...TOOLTIP_BOX,
      trigger: "item",
      formatter: (p: { dataIndex: number }) => {
        const r = shown[p.dataIndex];
        return r ? tooltipHtml(r.feature, [["Importance", r.importance == null ? "–" : fmtScore(r.importance)], ["p-value", r.p_value == null ? "–" : r.p_value.toPrecision(2)]]) : "";
      },
    },
    series: [{ type: "bar", data: shown.map((r) => r.importance ?? 0), barMaxWidth: 16, itemStyle: { color: c["series-1"], borderRadius: [0, 4, 4, 0] } }],
  }), [c, shown]);
  return (
    <div className="grid gap-1">
      <EChart option={option} height={Math.max(120, shown.length * 22 + 24)} aria-label={`Column importance: ${shown.map((r) => r.feature).reverse().join(", ")}`} />
      <p className="text-xs text-muted-foreground">How much the score drops when a column's values are shuffled, on rows held out from training. Near zero: the predictor hardly uses it.</p>
    </div>
  );
}

/** A series' recent history, then its forecast: the mean, and the band between the report's two quantiles. */
export function ForecastChart(props: { history: SeriesPoint[]; forecast: SeriesPoint[]; band: [number, number]; target: string }) {
  const c = useChartColors();
  const { history, forecast } = props;
  const option = useMemo((): ChartOption => {
    const times = [...history, ...forecast].map((p) => p.t.replace("T00:00:00", ""));
    const lead = history.length - 1;
    const pad = (values: (number | null | undefined)[], before: number) => [...Array(before).fill(null), ...values.map((v) => v ?? null)];
    // The mean starts at the last known value, so the two lines meet.
    const mean = [...Array(Math.max(0, lead)).fill(null), history[lead]?.y ?? null, ...forecast.map((p) => p.mean ?? null)];
    const low = pad(forecast.map((p) => p.low), history.length);
    const width = pad(forecast.map((p) => (p.high != null && p.low != null ? p.high - p.low : null)), history.length);
    const at = (i: number) => (i < history.length ? history[i] : forecast[i - history.length]);
    return {
      animationDuration: 400,
      grid: { left: 8, right: 16, top: 12, bottom: 4, containLabel: true },
      xAxis: { type: "category", data: times, boundaryGap: false, axisLabel: { ...text(c), hideOverlap: true }, axisLine: { lineStyle: { color: c.axis } }, axisTick: { show: false } },
      yAxis: { type: "value", scale: true, splitNumber: 3, axisLabel: { ...text(c), formatter: (v: number) => fmtCompact(v) }, splitLine: { lineStyle: { color: c.grid } } },
      tooltip: {
        ...TOOLTIP_BOX,
        trigger: "axis",
        axisPointer: { type: "line", lineStyle: { color: c.axis } },
        formatter: (params: { dataIndex: number }[]) => {
          const i = params[0]?.dataIndex;
          const p = i === undefined ? undefined : at(i);
          if (!p) return "";
          return i! < history.length
            ? tooltipHtml(times[i!], [[props.target, p.y == null ? "–" : fmtCompact(p.y), c.foreground]])
            : tooltipHtml(times[i!], [
                ["Forecast", p.mean == null ? "–" : fmtCompact(p.mean), c["series-1"]],
                [`${props.band[0] * 100}–${props.band[1] * 100}%`, `${fmtCompact(p.low ?? NaN)} – ${fmtCompact(p.high ?? NaN)}`, alpha(c["series-1"], 0.3)],
              ]);
        },
      },
      series: [
        { type: "line", stack: "band", data: low, symbol: "none", lineStyle: { opacity: 0 }, silent: true },
        { type: "line", stack: "band", data: width, symbol: "none", lineStyle: { opacity: 0 }, areaStyle: { color: alpha(c["series-1"], 0.18) }, silent: true },
        { type: "line", data: pad(history.map((p) => p.y), 0).concat(Array(forecast.length).fill(null)), symbol: "none", lineStyle: { color: c.foreground, width: 1.5 } },
        { type: "line", data: mean, symbol: "none", lineStyle: { color: c["series-1"], width: 2, type: "dashed" } },
      ],
    };
  }, [c, history, forecast, props.band, props.target]);
  return (
    <div className="grid gap-2">
      <ChartLegend
        items={[
          { label: props.target, color: c.foreground, mark: "line" },
          { label: "Forecast", color: c["series-1"], mark: "dashed" },
          { label: `${props.band[0] * 100}–${props.band[1] * 100}% band`, color: alpha(c["series-1"], 0.3), mark: "rect" },
        ]}
        className="flex-wrap gap-y-1"
      />
      <EChart option={option} height={240} aria-label={`${props.target}: the last ${history.length} steps, then a forecast of ${forecast.length}`} />
    </div>
  );
}
