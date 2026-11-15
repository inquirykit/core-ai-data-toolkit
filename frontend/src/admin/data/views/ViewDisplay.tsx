// A view's answer drawn as its display says (views.ts works out what to draw):
// a table (the shared DataGrid, cells as DataRows.tsx draws them), a number,
// a line, bar or pie chart (shared/echart.tsx, colored by the categorical
// palette in fixed order the graph's tables use, each theme's own steps), the
// graph the Graph query draws (GraphView.tsx), or a bucket's files with their
// downloads, images as thumbnails fetched with the page's key. A chart can be
// read as a table too, and a display that names fields the answer lacks says
// so, then shows the answer as a table, rather than draw nothing.
import { lazy, Suspense, useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ChartBar, ChartLine, ChartPie, Download, FileIcon, Files, Hash, Network, Table2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";
import { useAction } from "../../../shared/action";
import { useApi } from "../../../shared/api";
import { downloadResponse } from "../../../shared/browser";
import { Notice, Segmented } from "../../../shared/components";
import { EChart, TOOLTIP_BOX, tooltipHtml, useChartColors, type ChartOption, type TooltipRow } from "../../../shared/echart";
import { fmtAgo, fmtBytes, fmtCompact, fmtInt, fmtPct } from "../../../shared/format";
import { DataGrid, type GridColumn } from "../../../shared/grid";
import { useTheme } from "../../../shared/theme";
import { ChartLegend, type LegendItem } from "../../charts";
import { type Workspace } from "../data";
import { Cell } from "../DataRows";
import { graphFromRows, tableColor } from "../graph/graph";
import {
  bucketOf,
  chartData,
  figureOf,
  fmtFigure,
  labelOf,
  missingFields,
  pieSlices,
  type ChartDisplay,
  type RowsDisplay,
  type ViewDisplay,
  type ViewResult,
  type ViewSpec,
} from "./views";

const GraphView = lazy(() => import("../graph/GraphView"));

type AnyRow = Record<string, unknown>;

/** The answer as its display draws it. ``height`` is a chart's; ``compact`` leaves out the chart's Table switch. */
export function ResultView(props: {
  workspace: Workspace;
  spec: Pick<ViewSpec, "source" | "display">;
  result: ViewResult;
  height?: number;
  compact?: boolean;
}) {
  const display: ViewDisplay = props.spec.display ?? { type: "table" };
  if (display.type !== "graph" && display.type !== "files") return <RowsView display={display} result={props.result} height={props.height} compact={props.compact} />;
  const rows = props.result.rows as AnyRow[];
  if (!rows.length) return <p className="py-6 text-center text-sm text-muted-foreground">No rows</p>;
  return display.type === "graph" ? (
    <ResultGraph workspace={props.workspace} rows={rows} />
  ) : (
    <FileGallery workspace={props.workspace.name} bucket={bucketOf(props.spec)} rows={rows} />
  );
}

/** Rows as a table, a figure or a chart: a view's answer, or a chart a playground reply writes (playground/blocks.ts). */
export function RowsView(props: { display: RowsDisplay; result: Pick<ViewResult, "columns" | "rows">; height?: number; compact?: boolean }) {
  const { display } = props;
  const rows = props.result.rows as AnyRow[];
  const missing = missingFields(display, props.result);
  const [asTable, setAsTable] = useState(false);
  const chart = display.type === "line" || display.type === "bar" || display.type === "pie";
  if (missing.length) {
    return (
      <div className="grid gap-3">
        <Notice
          notice={{
            kind: "error",
            text: `The ${display.type} display names ${missing.join(", ")}, which the answer has no column of. It has ${props.result.columns.join(", ") || "none"}.`,
          }}
        />
        <ResultTable result={props.result} />
      </div>
    );
  }
  if (!rows.length && display.type !== "table") return <p className="py-6 text-center text-sm text-muted-foreground">No rows</p>;
  return (
    <div className="grid min-w-0 gap-2">
      {chart && !props.compact && (
        <Segmented<"chart" | "table">
          label="Show the answer as"
          size="sm"
          options={[
            ["chart", "Chart"],
            ["table", "Table"],
          ]}
          value={asTable ? "table" : "chart"}
          onChange={(v) => setAsTable(v === "table")}
        />
      )}
      {chart && asTable ? (
        <ResultTable result={props.result} />
      ) : display.type === "number" ? (
        <Figure value={figureOf(rows, display.field)} label={display.label ?? display.field} unit={display.unit} />
      ) : display.type === "line" || display.type === "bar" ? (
        <SeriesChart display={display} rows={rows} height={props.height ?? 280} />
      ) : display.type === "pie" ? (
        <PieChart rows={rows} label={display.label} value={display.value} height={props.height ?? 280} />
      ) : (
        <ResultTable result={props.result} columns={display.columns ?? undefined} />
      )}
    </div>
  );
}

const ICONS = { table: Table2, number: Hash, line: ChartLine, bar: ChartBar, pie: ChartPie, graph: Network, files: Files } as const;

/** A view's icon, by its display. */
export function ViewIcon({ type, className }: { type: ViewDisplay["type"]; className?: string }) {
  const Icon = ICONS[type] ?? Table2;
  return <Icon className={className} aria-hidden="true" />;
}

/** The rows in a grid: ``columns`` in their order, or every column as first seen. */
export function ResultTable({ result, columns }: { result: Pick<ViewResult, "columns" | "rows">; columns?: string[] }) {
  const shown = columns?.length ? columns : result.columns;
  const gridColumns: GridColumn<AnyRow>[] = shown.map((c, i) => ({
    key: c,
    label: c,
    headClassName: cn("h-9 px-3 font-mono text-xs font-medium", i === 0 && "sticky left-0 z-10 bg-muted"),
    className: cn("max-w-80 px-3 py-1.5", i === 0 && "sticky left-0 bg-card"),
    render: (row) => <Cell value={row[c]} />,
  }));
  return (
    <div className="max-h-[60vh] overflow-auto rounded-lg border">
      <DataGrid columns={gridColumns} rows={result.rows as AnyRow[]} rowKey={(_, i) => i} headerClassName="sticky top-0 z-10 bg-muted/90 backdrop-blur" empty="No rows" />
    </div>
  );
}

/** One figure, large, with what it is. */
function Figure({ value, label, unit }: { value: unknown; label: string; unit?: string | null }) {
  return (
    <div className="grid content-center gap-1 py-4">
      <div className="text-4xl font-semibold tracking-tight tabular-nums" title={labelOf(value)}>
        {fmtFigure(value)}
        {unit && <span className="ml-1.5 text-lg font-normal text-muted-foreground">{unit}</span>}
      </div>
      <div className="text-sm text-muted-foreground">{label}</div>
    </div>
  );
}

/** A series' color: its slot of the palette in this theme, gray for "Other". */
function useSlotColor(): (slot: number) => string {
  const { resolved } = useTheme();
  return (slot: number) => tableColor(slot, resolved === "dark");
}

const fmtValue = (v: number | null) => (v === null ? "–" : v.toLocaleString(undefined, { maximumFractionDigits: 2 }));

/** Lines or bars over the x values; a legend for two series or more. */
function SeriesChart({ display, rows, height }: { display: ChartDisplay; rows: AnyRow[]; height: number }) {
  const c = useChartColors();
  const colorOf = useSlotColor();
  const data = useMemo(() => chartData(rows, display), [rows, display]);
  const line = display.type === "line";
  const stacked = display.type === "bar" && Boolean(display.stacked);
  const option = useMemo((): ChartOption => {
    const text = { color: c["muted-foreground"], fontSize: 11.5, fontFamily: c.font };
    const last = data.series.length - 1;
    return {
      animationDuration: 400,
      grid: { left: 8, right: 12, top: 12, bottom: 4, containLabel: true },
      xAxis: {
        type: "category",
        data: data.categories,
        axisLine: { lineStyle: { color: c.axis } },
        axisTick: { show: false },
        axisLabel: { ...text, hideOverlap: true },
      },
      yAxis: { type: "value", splitNumber: 3, axisLabel: { ...text, formatter: (v: number) => fmtCompact(v) }, splitLine: { lineStyle: { color: c.grid } } },
      tooltip: {
        ...TOOLTIP_BOX,
        trigger: "axis",
        axisPointer: line ? { type: "line", lineStyle: { color: c.axis } } : { type: "shadow", shadowStyle: { color: c.foreground, opacity: 0.05 } },
        formatter: (params: { dataIndex: number }[]) => {
          const i = params[0]?.dataIndex;
          if (i === undefined) return "";
          const lines: TooltipRow[] = data.series.map((s) => [s.name, fmtValue(s.values[i]), colorOf(s.slot)]);
          return tooltipHtml(data.categories[i], lines);
        },
      },
      series: data.series.map((s, i) =>
        line
          ? {
              type: "line",
              name: s.name,
              data: s.values,
              lineStyle: { color: colorOf(s.slot), width: 2, cap: "round" },
              itemStyle: { color: colorOf(s.slot) },
              symbol: "circle",
              // A point with no neighbour would not show as a line: it gets a dot.
              symbolSize: (_: unknown, p: { dataIndex: number }) => (s.values[p.dataIndex] !== null && s.values[p.dataIndex - 1] == null && s.values[p.dataIndex + 1] == null ? 8 : 0),
            }
          : {
              type: "bar",
              name: s.name,
              data: s.values,
              stack: stacked ? "all" : undefined,
              barMaxWidth: 32,
              barGap: "8%",
              // Rounded where the bar ends, square at the baseline; a thin surface line keeps fills apart.
              itemStyle: { color: colorOf(s.slot), borderColor: c.card, borderWidth: 1, borderRadius: !stacked || i === last ? [4, 4, 0, 0] : 0 },
            },
      ),
    };
  }, [c, data, line, stacked, colorOf]);
  const legend: LegendItem[] = data.series.map((s) => ({ label: s.name, color: colorOf(s.slot), mark: line ? "line" : "rect" }));
  return (
    <div className="grid min-w-0 gap-2">
      {legend.length > 1 && <ChartLegend items={legend} className="flex-wrap gap-y-1" />}
      <EChart className="min-w-0" option={option} height={height} aria-label={`${display.type === "line" ? "Lines" : "Bars"} of ${display.y.join(", ")} by ${display.x}`} />
      {data.folded > 0 && <p className="text-xs text-muted-foreground">{fmtInt(data.folded)} series past the seventh are summed as Other.</p>}
    </div>
  );
}

/** A slice per label, largest first; its share in the tooltip. */
function PieChart({ rows, label, value, height }: { rows: AnyRow[]; label: string; value: string; height: number }) {
  const c = useChartColors();
  const colorOf = useSlotColor();
  const slices = useMemo(() => pieSlices(rows, label, value), [rows, label, value]);
  const total = slices.reduce((sum, s) => sum + s.value, 0);
  const option = useMemo(
    (): ChartOption => ({
      animationDuration: 400,
      tooltip: {
        ...TOOLTIP_BOX,
        trigger: "item",
        formatter: (p: { dataIndex: number }) => {
          const s = slices[p.dataIndex];
          return s ? tooltipHtml(s.name, [[value, fmtValue(s.value), colorOf(s.slot)], ["Share", total ? fmtPct(s.value / total) : "–"]]) : "";
        },
      },
      series: [
        {
          type: "pie",
          radius: ["48%", "78%"],
          avoidLabelOverlap: true,
          label: { show: false },
          itemStyle: { borderColor: c.card, borderWidth: 2, borderRadius: 4 },
          data: slices.map((s) => ({ name: s.name, value: s.value, itemStyle: { color: colorOf(s.slot) } })),
        },
      ],
    }),
    [c, slices, total, value, colorOf],
  );
  if (!slices.length) return <p className="py-6 text-center text-sm text-muted-foreground">No positive {value} to share out</p>;
  return (
    <div className="grid min-w-0 gap-2">
      <ChartLegend items={slices.map((s) => [s.name, colorOf(s.slot)] as [string, string])} className="flex-wrap gap-y-1" />
      <EChart className="min-w-0" option={option} height={height} aria-label={`${value} by ${label}: ${slices.map((s) => `${s.name} ${fmtValue(s.value)}`).join(", ")}`} />
    </div>
  );
}

/** The rows and edges of the answer, drawn as the Graph query draws them. */
function ResultGraph({ workspace, rows }: { workspace: Workspace; rows: AnyRow[] }) {
  const graphTables = useMemo(() => workspace.tables.filter((t) => t.graph).map((t) => t.name), [workspace]);
  const drawn = useMemo(() => graphFromRows(rows, graphTables), [rows, graphTables]);
  if (!drawn.nodes.length) {
    return <p className="py-6 text-center text-sm text-muted-foreground">No relationships to draw: the SQL returns none (rows of _edges, with from_table, from_id, to_table and to_id).</p>;
  }
  return (
    <Suspense fallback={<Skeleton className="h-[62vh] rounded-lg" />}>
      <GraphView workspace={workspace} data={drawn} />
    </Suspense>
  );
}

// -------------------------------------------------------------------- files
/** A row of a bucket as the gallery reads it. */
interface FileRow {
  id: string;
  name?: string;
  size?: number;
  content_type?: string;
  created?: number;
}

/** Images up to this size get a thumbnail, fetched as the download is. */
const THUMB_BYTES = 2 * 1024 * 1024;
const isImage = (f: FileRow) => typeof f.content_type === "string" && f.content_type.startsWith("image/") && (f.size ?? Infinity) <= THUMB_BYTES;

/** A bucket's files: a thumbnail for each small image, its name, type, size and time, and its download. */
function FileGallery({ workspace, bucket, rows }: { workspace: string; bucket: string | null; rows: AnyRow[] }) {
  const client = useApi();
  const files = rows.filter((r): r is AnyRow & FileRow => typeof r.id === "string");
  const download = useAction(
    async (file: FileRow) =>
      downloadResponse(
        await client.raw("get", "/db/{workspace}/{bucket}/_files/{file_id}", { params: { path: { workspace, bucket: bucket!, file_id: file.id } } }),
        file.name ?? file.id,
      ),
    { failed: (file) => `Could not download ${file.name ?? file.id}` },
  );
  if (!bucket) return <Notice notice={{ kind: "error", text: "Name the bucket these files are in: the display's bucket, or a rows or search source's table." }} />;
  const columns: GridColumn<FileRow>[] = [
    {
      key: "thumb",
      label: <span className="sr-only">Preview</span>,
      headClassName: "w-0",
      render: (f) => (isImage(f) ? <Thumbnail workspace={workspace} bucket={bucket} file={f} /> : <FileIcon className="size-5 text-muted-foreground" aria-hidden="true" />),
    },
    { key: "name", label: "Name", className: "max-w-80 truncate font-medium", title: (f) => f.name, render: (f) => f.name ?? f.id },
    { key: "type", label: "Type", className: "max-w-48 truncate font-mono text-xs text-muted-foreground", render: (f) => f.content_type ?? "–" },
    { key: "size", label: "Size", align: "right", render: (f) => (typeof f.size === "number" ? fmtBytes(f.size) : "–") },
    { key: "created", label: "Uploaded", className: "text-muted-foreground", render: (f) => (typeof f.created === "number" ? fmtAgo(f.created) : "–") },
    {
      key: "download",
      label: <span className="sr-only">Download</span>,
      headClassName: "w-0",
      className: "text-right",
      render: (f) => (
        <Button type="button" variant="outline" size="icon-sm" aria-label={`Download ${f.name ?? f.id}`} title="Download" disabled={download.running?.[0].id === f.id} onClick={() => void download.run(f)}>
          <Download aria-hidden="true" />
        </Button>
      ),
    },
  ];
  return (
    <div className="max-h-[60vh] overflow-auto rounded-lg border">
      <DataGrid columns={columns} rows={files} rowKey={(f) => f.id} headerClassName="sticky top-0 z-10 bg-muted/90 backdrop-blur" empty="No files" />
    </div>
  );
}

/** An image file's thumbnail: its bytes fetched with the page's key, shown from a blob URL, let go when it leaves. */
function Thumbnail({ workspace, bucket, file }: { workspace: string; bucket: string; file: FileRow }) {
  const client = useApi();
  const bytes = useQuery({
    queryKey: ["db", "thumbnail", workspace, bucket, file.id],
    queryFn: async () => (await client.raw("get", "/db/{workspace}/{bucket}/_files/{file_id}", { params: { path: { workspace, bucket, file_id: file.id } } })).blob(),
    staleTime: Infinity,
  });
  const [url, setUrl] = useState<string | null>(null);
  useEffect(() => {
    if (!bytes.data) return;
    const made = URL.createObjectURL(bytes.data);
    setUrl(made);
    return () => URL.revokeObjectURL(made);
  }, [bytes.data]);
  if (!url) return <Skeleton className="size-10 rounded-md" />;
  return <img src={url} alt={file.name ?? ""} className="size-10 rounded-md border object-cover" loading="lazy" />;
}
