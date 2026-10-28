// The Activity & logs section of /admin: a range toolbar, the zoomable chart
// (ActivityChart.tsx, with its legend in ActivityLegend.tsx), and tabs with
// the requests (panels.tsx), per-model figures (ModelsTable.tsx) and model
// events of that range, plus the server's log (LogView.tsx). A request opens
// its input and output (TraceDialog.tsx, at #/activity/<trace id>); searching
// the requests' words lists the traces that hold them instead of the request
// metadata. It polls on its own, since what it asks for depends on the range.

import { useMemo, useState, type ReactNode } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronLeft, ChevronRight, Eraser, Info, Radio, ZoomOut } from "lucide-react";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import type { ApiClient } from "../shared/api";
import { useDebounced, useHash } from "../shared/browser";
import { Segmented, SelectInput, StatusDot } from "../shared/components";
import { fmtInt, fmtMs, fmtPct } from "../shared/format";
import { errorText, poll } from "../shared/query";
import type { LogLine, Logs, ModelEvent, Timeline, TraceCapture } from "../shared/types";
import {
  CONTEXT_SPAN_S,
  DEFAULT_SPAN,
  LOG_LEVELS,
  PRESETS,
  bucketsFor,
  filterLogs,
  fmtRange,
  fromRequestRow,
  fromTrace,
  mergeLogs,
  panRange,
  presetOf,
  presetView,
  rangeOf,
  searchesTraces,
  traceFromHash,
  viewOf,
  zoomRange,
  type LogLevel,
  type Range,
  type RequestItem,
  type View,
} from "./activity";
import { ActivityChart } from "./ActivityChart";
import { ActivityLegend } from "./ActivityLegend";
import { useWidth } from "./charts";
import { LEVEL_COLOR, LogView } from "./LogView";
import { ModelsTable } from "./ModelsTable";
import { EventsFeed, RecentRequests } from "./panels";
import { TraceDialog } from "./TraceDialog";

// How long typing in a text filter pauses before it is sent.
const TYPING_PAUSE_MS = 300;
const REFRESH_MS = 5000;
/** How many requests of the range the table lists. */
const REQUEST_LIMIT = 200;
const LOG_LIMIT = 2000;
/** The chart's side margins (ActivityChart M.l + M.r), to size the plot. */
const CHART_MARGINS = 64;

type StatusFilter = "" | "ok" | "client_error" | "error";
const STATUS_OPTIONS: [StatusFilter, string][] = [
  ["", "All"],
  ["ok", "OK"],
  ["client_error", "Client errors"],
  ["error", "Server errors"],
];

const now = () => Date.now() / 1000;

interface Fetched {
  /** The range, resolution and filters it was loaded for (the section's ``key``) */
  key: string;
  /** When it was loaded, in Unix seconds: where a live range ends */
  at: number;
  data: Timeline | null;
  context: Timeline | null;
  events: ModelEvent[];
  requests: RequestItem[];
  /** Whether the requests came from the traces (a words or tag search) */
  searched: boolean;
}

/** What shows before the first load. */
const NO_LINES: LogLine[] = [];
const NOTHING: Fetched = { key: "", at: 0, data: null, context: null, events: [], requests: [], searched: false };

/** The server log so far: each load adds the lines after the last one. */
interface FetchedLogs {
  lines: LogLine[];
  info: Pick<Logs, "capacity" | "level">;
}

/**
 * The requests of a range: their metadata, or, when words or a tag are
 * searched, the traces that match (only they have the words).
 */
async function fetchRequests(
  client: ApiClient,
  r: Range,
  status: StatusFilter,
  model: string,
  words: string,
  tag: string,
  signal: AbortSignal,
): Promise<[RequestItem[], boolean]> {
  // An empty filter is left out of the query.
  if (searchesTraces(words, tag)) {
    const body = await client.get("/admin/traces", {
      params: { query: { since: r.since, until: r.until, limit: REQUEST_LIMIT, status: status || undefined, model: model || undefined, q: words || undefined, tag: tag || undefined } },
      signal,
    });
    return [body.traces.map(fromTrace), true];
  }
  const body = await client.get("/admin/requests", {
    params: { query: { since: r.since, until: r.until, limit: REQUEST_LIMIT, status: status || undefined, model: model || undefined } },
    signal,
  });
  return [body.requests.map(fromRequestRow), false];
}

export function ActivitySection(props: { client: ApiClient; monitoring: boolean | null }) {
  const { client } = props;
  const [view, setView] = useState<View>(presetView(DEFAULT_SPAN));
  const [presetSpan, setPresetSpan] = useState(DEFAULT_SPAN);
  const [clock, setClock] = useState(now);
  const [status, setStatus] = useState<StatusFilter>("");
  const [modelInput, setModelInput] = useState("");
  const [wordsInput, setWordsInput] = useState("");
  const [tagInput, setTagInput] = useState("");
  // The filters as sent: what was typed, once typing has paused (a load is five requests).
  const model = useDebounced(modelInput.trim(), TYPING_PAUSE_MS);
  const words = useDebounced(wordsInput.trim(), TYPING_PAUSE_MS);
  const tag = useDebounced(tagInput.trim(), TYPING_PAUSE_MS);
  const open = useHash(traceFromHash);
  const [logLevel, setLogLevel] = useState<LogLevel>("INFO");
  const [logSearch, setLogSearch] = useState("");
  const [follow, setFollow] = useState(true);
  const [clearedBefore, setClearedBefore] = useState(0);
  const [tab, setTab] = useState("requests");
  const [chartRef, chartWidth] = useWidth<HTMLDivElement>();
  const buckets = bucketsFor(Math.max(0, chartWidth - CHART_MARGINS));
  const live = view.until == null;

  // Whether requests leave traces, and for how long: said once, in the footnote and the detail.
  const traces = client.useQuery("get", "/admin/traces", { params: { query: { limit: 1 } } });
  const capture = traces.data?.capture ?? null;

  // --------------------------------------------------------------- fetching
  // After a pause (a drag changes the range many times a second, typing a
  // filter many times a minute) when the range, resolution or filters change,
  // and every few seconds, or when the tab comes back.
  const key = `${view.span}|${view.until}|${buckets}|${status}|${model}|${words}|${tag}`;
  const settledKey = useDebounced(key, 150);
  const activity = useQuery({
    queryKey: ["admin", "activity", settledKey],
    queryFn: async ({ signal }): Promise<Fetched> => {
      const at = now();
      const r = rangeOf(view, at);
      // The navigator shows at least a week, and always contains the range.
      const contextSpan = Math.max(CONTEXT_SPAN_S, at - r.since);
      const [data, events, [requests, searched], context] = await Promise.all([
        client.get("/admin/timeline", { params: { query: { since: r.since, until: r.until, buckets } }, signal }),
        client.get("/admin/events", { params: { query: { since: r.since, until: r.until, limit: 500 } }, signal }),
        fetchRequests(client, r, status, model, words, tag, signal),
        client.get("/admin/timeline", { params: { query: { since: at - contextSpan, until: at, buckets: 120 } }, signal }),
      ]);
      return { key, at, data, context, events: events.events, requests, searched };
    },
    ...poll(REFRESH_MS),
  });
  const fetched = activity.data ?? NOTHING;
  const activityError = errorText(activity.error);
  // The log, whatever the range: each poll asks for the lines after the last one the cache holds.
  const queries = useQueryClient();
  const logsLoad = useQuery({
    queryKey: ["admin", "logs"],
    queryFn: async ({ signal, queryKey }): Promise<FetchedLogs> => {
      const lines = queries.getQueryData<FetchedLogs>(queryKey)?.lines ?? [];
      const log = await client.get("/admin/logs", { params: { query: { after_id: lines.length ? lines[lines.length - 1].id : 0, limit: LOG_LIMIT } }, signal });
      return { lines: mergeLogs(lines, log.lines, log.capacity), info: { capacity: log.capacity, level: log.level } };
    },
    ...poll(REFRESH_MS),
  });
  const logs = logsLoad.data?.lines ?? NO_LINES;
  const logsInfo = logsLoad.data?.info ?? null;
  // A load moves a live range's end to when it was made; so does picking a range.
  const range = useMemo(() => rangeOf(view, Math.max(clock, fetched.at)), [view, clock, fetched.at]);

  // ------------------------------------------------------------ the range
  const show = (r: Range) => {
    const at = now();
    setClock(at);
    setView(viewOf(r, at));
  };
  const choosePreset = (span: number) => {
    setPresetSpan(span);
    setClock(now());
    setView(presetView(span));
  };
  const reset = () => choosePreset(presetSpan);

  const { data, context, events, requests, searched } = fetched;
  const totals = data?.totals;
  const failed = totals ? totals.client_errors + totals.errors : 0;
  const visibleLogs = useMemo(
    () => filterLogs(logs.filter((l) => l.id > clearedBefore), logLevel, logSearch),
    [logs, clearedBefore, logLevel, logSearch],
  );

  return (
    <>
      {props.monitoring === false && (
        <Alert role="note">
          <Info aria-hidden="true" />
          <AlertDescription>
            <p>
              Monitoring is off (<code>ARCADEDB_PATH</code> is empty or <code>MONITORING_RETENTION_DAYS</code> is 0), so there is no request history to chart. The server log
              still shows.
            </p>
          </AlertDescription>
        </Alert>
      )}
      {capture && !capture.enabled && (
        <Alert role="note">
          <Info aria-hidden="true" />
          <AlertDescription>
            <p>
              Inputs and outputs are not kept: <code>TRACE_RETENTION_DAYS</code> is 0. Requests still list, but open to nothing, and
              searching their words finds only what was kept before.
            </p>
          </AlertDescription>
        </Alert>
      )}

      {/* The range: presets first, then nudges, then where we are. */}
      <div className="flex flex-wrap items-center gap-2" role="group" aria-label="Time range">
        <Segmented<string>
          label="Preset range"
          size="sm"
          options={PRESETS.map(([key, label]) => [key, label])}
          value={presetOf(view) ?? "custom"}
          onChange={(key) => choosePreset(PRESETS.find((p) => p[0] === key)![2])}
        />
        <div className="flex items-center gap-1">
          <Button type="button" variant="outline" size="icon-sm" aria-label="Earlier" onClick={() => show(panRange(range, -view.span / 2, now()))}>
            <ChevronLeft aria-hidden="true" />
          </Button>
          <Button
            type="button"
            variant="outline"
            size="icon-sm"
            aria-label="Later"
            disabled={live}
            onClick={() => show(panRange(range, view.span / 2, now()))}
          >
            <ChevronRight aria-hidden="true" />
          </Button>
          <Button type="button" variant="outline" size="icon-sm" aria-label="Zoom out" onClick={() => show(zoomRange(range, 2, 0.5, now()))}>
            <ZoomOut aria-hidden="true" />
          </Button>
          <Button type="button" variant={live ? "secondary" : "outline"} size="sm" aria-pressed={live} onClick={() => choosePreset(view.span)}>
            {live ? <StatusDot color="var(--good)" live /> : <Radio aria-hidden="true" />}
            {live ? "Live" : "Go live"}
          </Button>
        </div>
        <span className="text-xs text-muted-foreground tabular-nums max-sm:w-full sm:ml-auto" aria-live="polite">
          {fmtRange(range)}
        </span>
      </div>

      <Card className="gap-4" ref={chartRef}>
        {/* The legend goes beside the title on a wide screen and under it on a phone, where it wraps. */}
        <CardHeader className="sm:grid-cols-[minmax(0,1fr)_auto]">
          <CardTitle>Requests and latency</CardTitle>
          <CardDescription className="tabular-nums">
            {totals
              ? `${fmtInt(totals.requests)} requests · ${fmtInt(failed)} failed${totals.requests ? ` (${fmtPct(failed / totals.requests)})` : ""} · p50 ${fmtMs(totals.p50_ms)} · p95 ${fmtMs(totals.p95_ms)}`
              : activityError
                ? `Could not load: ${activityError}`
                : "Loading…"}
          </CardDescription>
          <div className="min-w-0 pt-1 sm:col-start-2 sm:row-span-2 sm:row-start-1 sm:self-center sm:pt-0">
            <ActivityLegend className="sm:justify-end" />
          </div>
        </CardHeader>
        <CardContent className="px-4">
          {/* Faded while a new range or filter loads; a poll leaves it as it is. */}
          <ActivityChart
            range={range}
            data={data}
            events={events}
            context={context}
            loading={activity.isFetching && fetched.key !== key}
            onRange={show}
            onReset={reset}
          />
        </CardContent>
        <CardFooter className="text-xs text-muted-foreground">
          Drag across the chart to zoom into a range, scroll to zoom, shift+drag or drag the strip below to pan, double-click to go back to the
          preset. Arrow keys pan and +/− zoom when the chart has focus.
        </CardFooter>
      </Card>

      <Card className="gap-0 overflow-hidden py-0">
        <Tabs value={tab} onValueChange={setTab} className="gap-0">
          <div className="flex flex-wrap items-center gap-3 border-b px-4 py-3">
            <TabsList className="max-w-full justify-start overflow-x-auto">
              <TabsTrigger value="requests">
                Requests <Count n={requests.length} more={requests.length >= REQUEST_LIMIT} />
              </TabsTrigger>
              <TabsTrigger value="models">
                By model <Count n={data?.models.length ?? 0} />
              </TabsTrigger>
              <TabsTrigger value="events">
                Model events <Count n={events.length} more={events.length >= 500} />
              </TabsTrigger>
              <TabsTrigger value="logs">
                Server log <Count n={visibleLogs.length} />
              </TabsTrigger>
            </TabsList>
            {tab === "requests" && (
              <div className="flex flex-wrap items-center gap-2 sm:ml-auto">
                <Segmented<StatusFilter> label="Status" size="sm" options={STATUS_OPTIONS} value={status} onChange={setStatus} />
                <Input
                  type="search"
                  aria-label="Filter by model or task"
                  placeholder="Model or task…"
                  className="h-8 w-44 text-sm"
                  value={modelInput}
                  onChange={(e) => setModelInput(e.target.value)}
                />
                <Input
                  type="search"
                  aria-label="Search inputs and outputs"
                  placeholder="Words in inputs and outputs…"
                  className="h-8 w-56 text-sm"
                  value={wordsInput}
                  onChange={(e) => setWordsInput(e.target.value)}
                />
                <Input
                  type="search"
                  aria-label="Filter by tag"
                  placeholder="Tag, e.g. playground"
                  className="h-8 w-40 text-sm"
                  value={tagInput}
                  onChange={(e) => setTagInput(e.target.value)}
                />
              </div>
            )}
            {tab === "logs" && (
              <div className="flex flex-wrap items-center gap-2 sm:ml-auto">
                <SelectInput
                  size="sm"
                  className="w-32"
                  aria-label="Lowest level shown"
                  value={logLevel}
                  onChange={(v) => setLogLevel(v as LogLevel)}
                  options={LOG_LEVELS.map((level) => ({
                    value: level,
                    label: (
                      <>
                        <StatusDot color={LEVEL_COLOR[level]} /> {level === "DEBUG" ? "All levels" : `${level} and up`}
                      </>
                    ),
                  }))}
                />
                <Input
                  type="search"
                  aria-label="Search the log"
                  placeholder="Search…"
                  className="h-8 w-52 text-sm"
                  value={logSearch}
                  onChange={(e) => setLogSearch(e.target.value)}
                />
                <label className="flex items-center gap-2 text-sm">
                  <Switch checked={follow} onCheckedChange={setFollow} aria-label="Follow new lines" />
                  Follow
                </label>
                <Button type="button" variant="ghost" size="sm" onClick={() => setClearedBefore(logs.length ? logs[logs.length - 1].id : 0)}>
                  <Eraser aria-hidden="true" />
                  Clear
                </Button>
              </div>
            )}
          </div>

          <TabsContent value="requests">
            <div className="max-h-[600px] overflow-y-auto">
              <RecentRequests requests={requests} withPreview={searched} onOpen={(id) => (location.hash = `#/activity/${encodeURIComponent(id)}`)} />
            </div>
            <Footnote>{requestsNote(requests.length, searched, capture)}</Footnote>
          </TabsContent>

          <TabsContent value="models">
            <div className="max-h-[600px] overflow-y-auto">
              <ModelsTable data={data} />
            </div>
          </TabsContent>

          <TabsContent value="events">
            <div className="max-h-[600px] overflow-y-auto px-4">
              <EventsFeed events={events} />
            </div>
          </TabsContent>

          <TabsContent value="logs">
            <LogView lines={visibleLogs} follow={follow} onScrollAway={() => setFollow(false)} />
            <Footnote>
              {logsInfo
                ? `The newest ${fmtInt(logsInfo.capacity)} lines of this server process since it started. Its LOG_LEVEL is ${logsInfo.level}: nothing below that is logged.`
                : "Loading…"}
            </Footnote>
          </TabsContent>
        </Tabs>
      </Card>

      <TraceDialog client={client} id={open} capture={capture} onClose={() => (location.hash = "#/activity")} />
    </>
  );
}

// ------------------------------------------------------------------ pieces
/** Under the requests table: what the rows are, how many, and how long their inputs and outputs stay. */
function requestsNote(count: number, searched: boolean, capture: TraceCapture | null): string {
  const kept = capture?.enabled
    ? `Click a request for its input and output, kept ${capture.retention_days} day${capture.retention_days === 1 ? "" : "s"}; strings are cut at ${fmtInt(capture.max_chars)} characters and media kept as size and hash.`
    : "Click a request for its input and output, when they were kept.";
  const dropped = capture?.dropped ? ` ${fmtInt(capture.dropped)} were dropped when the queue was full.` : "";
  const cap = count >= REQUEST_LIMIT ? ` The newest ${REQUEST_LIMIT} of the range: zoom in or filter to see the rest.` : "";
  const scope = searched ? "Matched among the kept inputs and outputs, so only requests that still have them." : kept;
  return scope + dropped + cap;
}

function Count({ n, more }: { n: number; more?: boolean }) {
  return (
    <span className="rounded-sm bg-muted-foreground/10 px-1.5 text-xs text-muted-foreground tabular-nums">
      {fmtInt(n)}
      {more ? "+" : ""}
    </span>
  );
}

function Footnote({ children }: { children: ReactNode }) {
  return <p className="border-t px-4 py-2.5 text-xs text-muted-foreground">{children}</p>;
}
