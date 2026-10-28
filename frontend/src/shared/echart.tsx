// The charts' one way onto the page: ECharts, given an option, sized to its
// box and kept in step with the theme. ECharts is large, so the chart itself
// (echart-view.tsx) loads the first time a page shows one. It draws on a
// canvas, which cannot read the page's CSS variables, so useChartColors
// resolves them (again when the theme changes), and tooltipHtml draws a
// tooltip the way the pages' own look.
import { lazy, Suspense, useMemo, type CSSProperties } from "react";
import type { ECharts, EChartsCoreOption } from "echarts/core";
import { Skeleton } from "@/components/ui/skeleton";
import { useTheme } from "./theme";

export type ChartOption = EChartsCoreOption;
// eslint-disable-next-line @typescript-eslint/no-explicit-any -- ECharts' event payloads differ by event
export type ChartEvents = Record<string, (event: any, chart: ECharts) => void>;

export interface EChartProps {
  option: ChartOption;
  /** Handlers by ECharts event name ("datazoom", "brushEnd"...), each given the event and the chart */
  events?: ChartEvents;
  /** Called once the first option is set: for an action the chart starts in (a brush to select with) */
  onReady?(chart: ECharts): void;
  height: number;
  "aria-label": string;
  className?: string;
  style?: CSSProperties;
}

const View = lazy(() => import("./echart-view"));

export function EChart(props: EChartProps) {
  return (
    <Suspense fallback={<Skeleton className="w-full rounded-md" style={{ height: props.height }} />}>
      <View {...props} />
    </Suspense>
  );
}

const TOKENS = ["series-1", "critical", "warning", "serious", "good", "axis", "grid", "muted", "muted-foreground", "foreground", "card", "background", "border"] as const;
export type ChartColors = Record<(typeof TOKENS)[number], string>;

let probe: CanvasRenderingContext2D | null = null;

/** Any CSS color as rgba(): canvas takes oklch, but ECharts works out shades from what it can parse. */
function toRgba(css: string): string {
  probe ??= Object.assign(document.createElement("canvas"), { width: 1, height: 1 }).getContext("2d", { willReadFrequently: true });
  if (!probe || !css) return css;
  probe.clearRect(0, 0, 1, 1);
  probe.fillStyle = css;
  probe.fillRect(0, 0, 1, 1);
  const [r, g, b, a] = probe.getImageData(0, 0, 1, 1).data;
  return `rgba(${r}, ${g}, ${b}, ${Math.round((a / 255) * 100) / 100})`;
}

/** ``color`` (rgba(), as useChartColors gives them) at ``opacity``. */
export const alpha = (color: string, opacity: number) => color.replace(/,\s*[\d.]+\)$/, `, ${opacity})`);

/** The page's colors (globals.css) as the charts take them, read again when the theme changes. */
export function useChartColors(): ChartColors & { of(css: string): string } {
  const { resolved } = useTheme();
  return useMemo(() => {
    const style = getComputedStyle(document.documentElement);
    const colors = Object.fromEntries(TOKENS.map((t) => [t, toRgba(style.getPropertyValue(`--${t}`).trim())])) as ChartColors;
    /** A color as the pages write it ("var(--critical)", "#fab219") as the charts take it. */
    const of = (css: string) => {
      const name = /^var\(--([\w-]+)\)$/.exec(css)?.[1];
      return name ? ((colors as Record<string, string>)[name] ?? toRgba(style.getPropertyValue(`--${name}`).trim())) : toRgba(css);
    };
    return { ...colors, of };
    // The theme is what the computed style follows.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [resolved]);
}

const escape = (text: string) => text.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]!);

/** A row of a tooltip: what, how much, and the series' color (rows without one keep the labels lined up). */
export type TooltipRow = [label: string, value: string, color?: string];

/** A tooltip as the pages draw one: a title over rows of label and value. ECharts places it. */
export function tooltipHtml(title: string, rows: TooltipRow[]): string {
  const row = ([label, value, color]: TooltipRow) =>
    `<div class="flex items-center justify-between gap-4"><span class="flex items-center gap-2 text-muted-foreground">` +
    `<span class="size-2.5 shrink-0 rounded-[3px]" style="background:${escape(color ?? "transparent")}"></span>${escape(label)}</span>` +
    `<span class="font-mono font-medium text-foreground tabular-nums">${escape(value)}</span></div>`;
  return `<div class="grid min-w-44 gap-1.5 text-xs"><div class="font-medium text-foreground">${escape(title)}</div><div class="grid gap-1">${rows.map(row).join("")}</div></div>`;
}

/** The tooltip box itself, for an option's ``tooltip``: on the page's body, so a card's edge does not cut it. */
export const TOOLTIP_BOX = {
  appendTo: "body",
  className: "rounded-lg! border! border-border/50! bg-background! px-2.5! py-1.5! shadow-xl!",
  padding: 0,
  borderWidth: 0,
  transitionDuration: 0,
} as const;
