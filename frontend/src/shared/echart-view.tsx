// The chart itself, loaded only when a page shows one (echart.tsx): ECharts
// with just the charts and components the pages use, on a canvas the size of
// its box, which follows the box as it resizes.
import { useEffect, useLayoutEffect, useRef } from "react";
import * as echarts from "echarts/core";
import { BarChart, LineChart, ScatterChart } from "echarts/charts";
import {
  AxisPointerComponent,
  BrushComponent,
  DataZoomInsideComponent,
  DataZoomSliderComponent,
  GraphicComponent,
  GridComponent,
  TooltipComponent,
} from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";
import type { EChartProps } from "./echart";

echarts.use([
  BarChart,
  LineChart,
  ScatterChart,
  AxisPointerComponent,
  BrushComponent,
  DataZoomInsideComponent,
  DataZoomSliderComponent,
  GraphicComponent,
  GridComponent,
  TooltipComponent,
  CanvasRenderer,
]);

export default function EChartView(props: EChartProps) {
  const box = useRef<HTMLDivElement>(null);
  const chart = useRef<echarts.ECharts | null>(null);
  // The newest handlers, so the ones bound at the start call what the page passes now.
  const latest = useRef(props);
  useLayoutEffect(() => {
    latest.current = props;
  });

  useEffect(() => {
    const instance = echarts.init(box.current!, undefined, { renderer: "canvas" });
    chart.current = instance;
    for (const name of Object.keys(latest.current.events ?? {})) {
      instance.on(name, (event) => latest.current.events?.[name]?.(event, instance));
    }
    const observer = new ResizeObserver(() => instance.resize());
    observer.observe(box.current!);
    return () => {
      observer.disconnect();
      instance.dispose();
      chart.current = null;
    };
  }, []);

  const ready = useRef(false);
  useEffect(() => {
    const instance = chart.current;
    if (!instance) return;
    instance.setOption(props.option);
    if (!ready.current) {
      ready.current = true;
      latest.current.onReady?.(instance);
    }
  }, [props.option]);

  return <div ref={box} role="img" aria-label={props["aria-label"]} className={props.className} style={{ height: props.height, ...props.style }} />;
}
