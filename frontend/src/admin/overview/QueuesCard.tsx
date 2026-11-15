// The Overview's queues card (queues.ts), in two pictures: Routing (RoutingView.tsx), requests going from the router in
// the middle along their task's line to the model that serves them, easy to follow for anyone; and Lanes, a row per
// loaded model with the batch it runs and its line of waiting requests, a dot each, the next first. A model backing up
// turns amber in both. Now, from the stats the dashboard polls (and, for Routing, the request log); over the page's
// window (1 hour, 24 hours, 7 days), Routing alone, as the window's traffic rather than a queue; or, with Demo,
// simulated over a sample set. The lanes are rows of the page: they reflow to the card's width, and a dot moves only
// when something happens to its request (it arrives, an interactive one goes ahead of the background ones, its model
// takes it).
import { useCallback, useEffect, useMemo, useReducer, useState } from "react";
import { Pause, Play } from "lucide-react";
import { AnimatePresence, MotionConfig, motion } from "motion/react";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import { useTaskLabels } from "../../shared/categories";
import { Segmented } from "../../shared/components";
import { fmtCompact, fmtInt, fmtMs, fmtPct } from "../../shared/format";
import type { Stats, StatsWindow } from "../../shared/types";
import { liveModels, liveView, SAMPLE, Simulation, SLOW_WAIT_MS, waitingCount, windowFigures, type Lane, type QueueView } from "./queues";
import { RoutingView } from "./RoutingView";

type Mode = "live" | "window" | "demo";
/** The page's window as its toggle names it, after "the last", and in short */
const WINDOW_NAMES: Record<StatsWindow, { toggle: string; name: string; short: string }> = {
  "1h": { toggle: "1 hour", name: "hour", short: "1 h" },
  "24h": { toggle: "24 hours", name: "24 hours", short: "24 h" },
  "7d": { toggle: "7 days", name: "7 days", short: "7 d" },
};
type Picture = "routing" | "lanes";
const PICTURES: [Picture, string][] = [
  ["routing", "Routing"],
  ["lanes", "Lanes"],
];
/** How often the demo moves on */
const TICK_MS = 100;
/** The room a waiting request's dot takes */
const DOT_ROOM = 12;
/** Task, model, running, waiting, count, by the card's own width: on a narrow card the task goes under the model */
const GRID = "grid grid-cols-[minmax(0,1fr)_5rem_minmax(0,1fr)_2.5rem] items-center gap-x-3 @3xl:grid-cols-[8rem_minmax(0,15rem)_5.5rem_minmax(0,1fr)_2.5rem]";

export function QueuesCard({ stats }: { stats: Stats }) {
  const [mode, setMode] = useState<Mode>(() => (Object.keys(stats.cache.model_details).length ? "live" : "demo"));
  const [picture, setPicture] = useState<Picture>("routing");
  const [paused, setPaused] = useState(false);
  const label = useTaskLabels();
  const names = WINDOW_NAMES[stats.window];
  const modes: [Mode, string][] = [["live", "Now"], ["window", names.toggle], ["demo", "Demo"]];
  // Over the window only the routing picture: the lanes are a queue, which is only now.
  const shown: Picture = mode === "window" ? "routing" : picture;
  const live = useMemo(() => liveView(stats), [stats]);
  const { models, hidden } = useMemo(() => liveModels(stats, mode === "window"), [stats, mode]);
  const windowed = useMemo(
    () => ({ figures: windowFigures(stats), sources: stats.totals.sources, name: names.name, short: names.short }),
    [stats, names],
  );
  // The demo's models run on this service's device, as its own would.
  const device = stats.process.device;
  const sample = useMemo(() => SAMPLE.map((m) => ({ ...m, device: m.via || m.own ? null : device })), [device]);
  const routed = mode === "demo" ? sample : models;
  // The routing picture moves the demo on itself, every frame; the lanes, every TICK_MS.
  const { sim, view: demo } = useDemo(mode === "demo" && !paused, shown === "lanes");
  const view = mode === "demo" ? demo : live;
  const wait = view.longestWaitMs ? ` · next waits up to ${fmtMs(view.longestWaitMs)}` : "";
  const t = stats.totals;
  const empty = mode === "live" ? (shown === "lanes" ? !view.lanes.length : !models.length) : mode === "window" && !t.requests;
  const describe =
    mode === "demo" ? "Sample models with simulated requests, slowed down to be seen"
      : mode === "window" ? `This service's requests over the last ${names.name}, by task and model`
        : shown === "routing" ? "This service's requests going to the models that serve them, a few seconds behind"
          : "The requests waiting for each loaded model, and those it runs, as the overview refreshes";
  return (
    <Card className="gap-4">
      <CardHeader>
        <CardTitle>Queues</CardTitle>
        <CardDescription>{describe}</CardDescription>
        <p className="text-sm tabular-nums text-muted-foreground">
          {mode === "window" ? (
            <>
              <span className="font-medium text-foreground">{fmtCompact(t.requests)}</span> requests
              {t.errors + t.client_errors ? ` · ${fmtPct((t.errors + t.client_errors) / t.requests)} failed` : ""}
            </>
          ) : (
            <>
              <span className="font-medium text-foreground">{fmtInt(view.waiting)}</span> waiting{wait}
            </>
          )}
        </p>
        <CardAction className="flex flex-wrap items-center justify-end gap-2">
          {mode !== "window" && <Segmented<Picture> label="Picture" size="sm" options={PICTURES} value={picture} onChange={setPicture} />}
          <Segmented<Mode> label="Now, over the window, or a demo" size="sm" options={modes} value={mode} onChange={setMode} />
          {mode === "demo" && (
            <Button variant="ghost" size="icon-sm" onClick={() => setPaused((p) => !p)} aria-label={paused ? "Play" : "Pause"}>
              {paused ? <Play aria-hidden="true" /> : <Pause aria-hidden="true" />}
            </Button>
          )}
        </CardAction>
      </CardHeader>
      <CardContent className="px-4">
        {empty ? (
          <div className="flex flex-col items-start gap-2 py-6 text-sm text-muted-foreground">
            {mode === "window" ? `No requests in the last ${names.name}.` : "No model is loaded now: a model's queue shows here once it is."}
            <Button variant="outline" size="sm" onClick={() => setMode("demo")}>
              See the demo
            </Button>
          </div>
        ) : shown === "routing" ? (
          <>
            <RoutingView
              source={{
                mode, models: routed, view, until: stats.until, window: mode === "window" ? windowed : undefined, sim,
                running: mode === "demo" && !paused,
              }}
              label={label}
            />
            {mode !== "demo" && hidden > 0 && (
              <p className="mt-2 px-3 text-xs text-muted-foreground">
                {`${fmtInt(hidden)} more ${hidden === 1 ? "model" : "models"}, the least used, not drawn`}
              </p>
            )}
          </>
        ) : (
          <MotionConfig reducedMotion="user">
            <Lanes view={view} mode={mode} label={label} />
          </MotionConfig>
        )}
        {!empty && <Legend mode={mode} picture={shown} away={routed.some((m) => m.via)} peer={routed.some((m) => m.peer)} />}
      </CardContent>
    </Card>
  );
}

/** The demo, and its view: moved on every TICK_MS by ``stepping`` (the routing picture moves it on itself), and drawn
 * again as often, or every second when the routing picture does the moving, for the counts over it. */
function useDemo(running: boolean, stepping: boolean): { sim: Simulation; view: QueueView } {
  const sim = useMemo(() => {
    const s = new Simulation(SAMPLE);
    s.advance(9000);
    return s;
  }, []);
  const [, tick] = useReducer((n: number) => n + 1, 0);
  useEffect(() => {
    if (!running) return;
    const timer = setInterval(
      () => {
        if (document.hidden) return;
        if (stepping) sim.step(TICK_MS);
        tick();
      },
      stepping ? TICK_MS : 1000,
    );
    return () => clearInterval(timer);
  }, [running, stepping, sim]);
  return { sim, view: sim.view() };
}

function Legend({ mode, picture, away, peer }: { mode: Mode; picture: Picture; away: boolean; peer: boolean }) {
  const outside = away && (
    <span className="flex items-center gap-1.5">
      <span className="rounded-[3px] bg-series-1/25 px-1 text-[10px] leading-3.5 font-semibold text-foreground" aria-hidden="true">
        ↗
      </span>
      answered outside, by the provider or server named{peer ? "; ⇄ by another toolkit" : ""}
    </span>
  );
  if (mode === "window") {
    return (
      <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1.5 px-3 text-xs text-muted-foreground">
        <span className="flex items-center gap-1.5">
          <span className="flex w-4 shrink-0 flex-col gap-0.5" aria-hidden="true">
            <span className="h-px bg-foreground/60" />
            <span className="h-1 rounded-full bg-foreground/60" />
          </span>
          a line as wide as its requests in the window
        </span>
        <Key className="bg-series-1">its dots: as many as it is busy, not requests</Key>
        <span>a model's card: its requests, the share that failed (when any did), its p95</span>
        {outside}
      </div>
    );
  }
  return (
    <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1.5 px-3 text-xs text-muted-foreground">
      {mode === "demo" ? (
        <>
          <Key className="bg-foreground/75">interactive request</Key>
          <Key className="border-[1.5px] border-foreground/60">background request, taken after the interactive ones</Key>
        </>
      ) : (
        <Key className={picture === "routing" ? "bg-series-1" : "bg-muted-foreground"}>
          {picture === "routing" ? "a request, in its task's color" : "a request"}
        </Key>
      )}
      {picture === "routing" && (
        <>
          <Key className="bg-critical">failed, where it stopped</Key>
          <Key className="bg-warning">turned away: its model too busy</Key>
          <Key className="bg-muted-foreground">refused as wrong</Key>
        </>
      )}
      <Key className="rounded-[3px] bg-warning/40 ring-1 ring-warning">
        {mode === "demo" ? "backing up: more waiting than one batch takes" : `backing up: the next request would wait ${fmtMs(SLOW_WAIT_MS)} or more`}
      </Key>
      {picture === "routing" && <Key className="rounded-[3px] border border-dashed border-foreground/60">model not loaded: the first request loads it</Key>}
      {picture === "routing" && outside}
    </div>
  );
}

function Lanes({ view, mode, label }: { view: QueueView; mode: Mode; label: (task: string) => string }) {
  // How many dots fit in the waiting column, from its width, measured again each time the column is drawn.
  const [room, setRoom] = useState(12);
  const column = useCallback((el: HTMLDivElement | null) => {
    if (!el) return;
    const observer = new ResizeObserver(() => setRoom(Math.max(3, Math.floor((el.clientWidth - 34) / DOT_ROOM))));
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  return (
    <div className="@container text-sm">
      <div className={cn(GRID, "px-3 pb-1.5 text-[11px] font-medium uppercase tracking-wide text-muted-foreground")} aria-hidden="true">
        <span className="hidden @3xl:block">Task</span>
        <span>Model</span>
        <span>Running</span>
        <div ref={column}>Waiting</div>
        <span className="text-right">#</span>
      </div>
      <div role="list" aria-label="Model queues">
        {view.lanes.map((lane, i) => {
          const task = label(lane.task);
          const first = i === 0 || view.lanes[i - 1].task !== lane.task;
          return (
            <motion.div
              key={lane.key}
              layout="position"
              role="listitem"
              aria-label={`${lane.model}: ${lane.loading ? "loading" : `${lane.running} running`}, ${waitingCount(lane)} waiting${lane.backingUp ? ", backing up" : ""}`}
              className={cn(GRID, "rounded-md px-3 py-2 transition-colors duration-300", lane.backingUp && "bg-warning/12 ring-1 ring-warning/50 ring-inset")}
            >
              <span className="hidden truncate text-muted-foreground @3xl:block">{first ? task : ""}</span>
              <Tooltip>
                <TooltipTrigger asChild>
                  <span className="min-w-0">
                    <span className="block truncate">
                      {lane.model}
                      {lane.via && <span className="text-muted-foreground"> {lane.peer ? "⇄" : "→"} {lane.via}</span>}
                      {lane.own && <span className="text-muted-foreground"> · own process</span>}
                    </span>
                    <span className="block truncate text-xs text-muted-foreground @3xl:hidden">{task}</span>
                  </span>
                </TooltipTrigger>
                <TooltipContent side="bottom" align="start">
                  <div className="grid gap-1 text-xs">
                    <div className="font-medium">{lane.model}</div>
                    {lane.tip.map(([what, value]) => (
                      <div key={what} className="flex justify-between gap-6">
                        <span className="opacity-70">{what}</span>
                        <span className="font-mono tabular-nums">{value}</span>
                      </div>
                    ))}
                  </div>
                </TooltipContent>
              </Tooltip>
              <Running lane={lane} />
              <Waiting lane={lane} room={room} known={mode === "demo"} />
              <span className="text-right font-mono tabular-nums">{fmtInt(waitingCount(lane))}</span>
            </motion.div>
          );
        })}
      </div>
      {(view.resting.length > 0 || view.hidden > 0) && (
        <p className="mt-2 truncate px-3 text-xs text-muted-foreground">
          {view.resting.length > 0 && <>Not loaded: {view.resting.join(" · ")}</>}
          {view.hidden > 0 && <> · {view.hidden} more loaded, not shown</>}
        </p>
      )}
    </div>
  );
}

function Key({ className, children }: { className: string; children: string }) {
  return (
    <span className="flex items-center gap-1.5">
      <span className={cn("size-2 shrink-0 rounded-full", className)} aria-hidden="true" />
      {children}
    </span>
  );
}

/** What a model runs: its batch, with how far it has got; its load; or nothing. */
function Running({ lane }: { lane: Lane }) {
  if (!lane.loading && !lane.running) return <span className="text-xs text-muted-foreground">idle</span>;
  const text = lane.loading ? "loading" : `×${lane.running}`;
  return (
    <span className="relative inline-flex h-6 w-fit min-w-12 items-center justify-center overflow-hidden rounded-md border px-2 font-mono text-xs tabular-nums">
      <motion.span key={text} initial={{ scale: 1.3, opacity: 0.3 }} animate={{ scale: 1, opacity: 1 }} transition={{ duration: 0.2 }}>
        {text}
      </motion.span>
      {lane.progress != null && (
        <span
          className="absolute inset-x-0 bottom-0 h-0.5 origin-left bg-foreground/40"
          style={{ transform: `scaleX(${Math.min(1, lane.progress)})` }}
        />
      )}
    </span>
  );
}

/** The requests waiting, a dot each, the next first; those past the room counted. Known (demo): filled if interactive,
 * hollow if background; live, only how many. */
function Waiting({ lane, room, known }: { lane: Lane; room: number; known: boolean }) {
  const all = typeof lane.waiting === "number" ? Array.from({ length: lane.waiting }, (_, id) => ({ id, high: true })) : lane.waiting;
  const shown = all.slice(0, room);
  return (
    <div className="flex h-6 min-w-0 items-center gap-1">
      <AnimatePresence initial={false} mode="popLayout">
        {shown.map((item) => (
          <motion.span
            key={item.id}
            layout="position"
            initial={{ opacity: 0, scale: 0.3 }}
            animate={{ opacity: 1, scale: 1 }}
            // Taken: it slides toward the running batch as it goes.
            exit={{ opacity: 0, x: -14 }}
            transition={{ duration: 0.22 }}
            className={cn(
              "size-2 shrink-0 rounded-full",
              !known ? "bg-muted-foreground" : item.high ? "bg-foreground/75" : "border-[1.5px] border-foreground/60",
            )}
          />
        ))}
      </AnimatePresence>
      {all.length > shown.length && <span className="ml-1 font-mono text-xs text-muted-foreground">+{all.length - shown.length}</span>}
    </div>
  );
}
