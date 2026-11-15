// The routing picture (routing.ts lays it out): requests come to the router in the middle, whose tooltip counts them by
// where they come from (an app, the playground, a workflow, an eval, a table), and hop along their task's line to their model's card, where they wait in its row, the next nearest the batch it runs, until it takes
// them; a card's badge says where its model runs, "↗" and a provider or server when outside. A hop is the picture
// catching up, short and eased: in the service a request is in its model's line the moment it is sent. In the demo
// the requests are the simulation's (queues.ts). Live, the cards hold each model's real
// counts from the stats, and the requests that hop are the request log's (GET /admin/requests, polled while the
// picture is shown), played a few seconds after they came: there each one has long been answered, so it goes
// straight into its model's pill, or shows where it stopped when it did not end well. Drawn on a canvas, zoomed out to
// fit, and spread toward half the window's height. Over a window (the page's 1 hour, 24 hours or 7 days) it draws no
// queue, which is only now: a line is as wide as its requests in the window, dots flow along it as many as it is busy,
// and a card gives its model's figures (requests, failed, p95).
import { useEffect, useMemo, useRef, useState, type PointerEvent } from "react";
import { createPortal } from "react-dom";
import { useApi } from "../../shared/api";
import { tooltipHtml, useChartColors } from "../../shared/echart";
import { fmtCompact, fmtInt, fmtMs, fmtPct } from "../../shared/format";
import {
  ending, HUES, layoutRouting, NATURAL, sourceOf, SOURCES, type Box, type Ending, type Path, type Placed, type Pt, type RouteStation,
  type RoutingLayout, type Source,
} from "./routing";
import { waitingCount, type Figures, type Lane, type QueueView, type Simulation } from "./queues";

/** How long a request takes to hop from the router to its model's card */
export const HOP_MS = 900;
/** How often the request log is read, live */
const POLL_MS = 2000;
/** The height it spreads toward: this share of the window, within these */
const FILL = { share: 0.5, least: 320, most: 600 };
const fillHeight = () => Math.round(Math.max(FILL.least, Math.min(FILL.most, window.innerHeight * FILL.share)));
/** The lines' hues, in the palette's fixed order (globals.css) */
const HUE_VARS = Array.from({ length: HUES }, (_, i) => `var(--series-${i + 1})`);
const TAU = Math.PI * 2;
/** How long a dot of a window's flow takes along its line */
const FLOW_MS = 2600;

/** A request drawn on its way, live: from the request log, played at ``born`` (performance.now()). */
interface Flight {
  id: number;
  station: string;
  born: number;
  end: Ending;
}

/** Live: the requests on their way, and when those of the last minute came (performance.now(), the oldest first) and
 * from where. */
interface Live {
  flights: Flight[];
  came: { at: number; source: Source }[];
}

export interface RoutingSource {
  mode: "live" | "window" | "demo";
  models: Placed[];
  view: QueueView;
  /** Live: the service's clock as the stats last said (Unix seconds), which the first read of the log counts by */
  until?: number;
  /** Over the window: each model's figures, its requests by source, and what it is called after "the last" ("hour",
   * "24 hours") and in short ("24 h") */
  window?: { figures: Map<string, Figures>; sources: Record<string, number>; name: string; short: string };
  /** The demo, which the picture moves on every frame while ``running`` */
  sim: Simulation;
  running: boolean;
}

type Colors = ReturnType<typeof useChartColors> & { hues: string[] };

export function RoutingView({ source, label }: { source: RoutingSource; label: (task: string) => string }) {
  const box = useRef<HTMLDivElement>(null);
  const canvas = useRef<HTMLCanvasElement>(null);
  const tip = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(0);
  const [height, setHeight] = useState(fillHeight);
  const chart = useChartColors();
  const colors = useMemo<Colors>(
    () => ({ ...chart, hues: HUE_VARS.map(chart.of) }),
    [chart],
  );
  const ids = source.models.map((m) => [m.task, m.id, m.via, m.own, m.device].join(":")).join("|");
  // Zoomed out to fit the card's width, and laid out to spread toward the height at that zoom.
  const scale = width ? Math.min(1, width / NATURAL) : 1;
  // eslint-disable-next-line react-hooks/exhaustive-deps -- a new layout only for new models or a new size
  const map = useMemo(() => (width ? layoutRouting(source.models, width / scale, height / scale) : null), [ids, width, height, scale]);
  // What the loop draws, kept current without starting it again.
  const latest = useRef({ source, label, colors, map, scale });
  latest.current = { source, label, colors, map, scale };
  const pointer = useRef<{ x: number; y: number; clientX: number; clientY: number } | null>(null);
  const flights = useLiveFlights(source.mode === "live", source.until);

  useEffect(() => {
    const el = box.current!;
    const observer = new ResizeObserver(() => setWidth(Math.floor(el.clientWidth)));
    observer.observe(el);
    const resized = () => setHeight(fillHeight());
    window.addEventListener("resize", resized);
    return () => {
      observer.disconnect();
      window.removeEventListener("resize", resized);
    };
  }, []);

  useEffect(() => {
    const el = canvas.current;
    if (!el) return;
    const shown = new Map<number, Pt>();
    let frame = 0;
    let last = performance.now();
    const loop = (t: number) => {
      const dt = Math.min(100, t - last);
      last = t;
      const { source, label, colors, map, scale } = latest.current;
      if (source.mode === "demo" && source.running) source.sim.step(dt);
      const ctx = el.getContext("2d");
      if (map && ctx) {
        const k = (window.devicePixelRatio || 1) * scale;
        if (el.width !== Math.round(map.width * k) || el.height !== Math.round(map.height * k)) {
          el.width = Math.round(map.width * k);
          el.height = Math.round(map.height * k);
        }
        ctx.setTransform(k, 0, 0, k, 0, 0);
        const p = pointer.current;
        const overRouter = !!p && inside(map.router, p.x, p.y);
        const under = p && !overRouter ? map.stations.find((s) => inside(s.card, p.x, p.y)) ?? null : null;
        // The demo's lanes as they are this frame, for the cards and the tooltip.
        const now = source.mode === "demo" ? { ...source, view: source.sim.view() } : source;
        const counts = draw(ctx, map, now, flights.current, shown, colors, label, dt, t, under, overRouter);
        showTip(tip.current!, overRouter ? "router" : under, p, now, label, counts);
      }
      frame = requestAnimationFrame(loop);
    };
    frame = requestAnimationFrame(loop);
    return () => cancelAnimationFrame(frame);
  }, [flights]);

  const point = (e: PointerEvent) => {
    const r = canvas.current!.getBoundingClientRect();
    const k = latest.current.scale;
    pointer.current = { x: (e.clientX - r.left) / k, y: (e.clientY - r.top) / k, clientX: e.clientX, clientY: e.clientY };
  };

  return (
    <div ref={box} className="w-full">
      <canvas
        ref={canvas}
        role="img"
        aria-label="Requests coming to the router in the middle, counted by where they come from, and going along their task's line to the model that runs them, where they wait in its line"
        className="mx-auto block touch-pan-y"
        style={map ? { width: map.width * scale, height: map.height * scale } : { width: "100%", height }}
        onPointerMove={point}
        onPointerDown={point}
        onPointerLeave={() => (pointer.current = null)}
      />
      {createPortal(
        <div ref={tip} hidden className="pointer-events-none fixed z-50 max-w-md rounded-lg border border-border/50 bg-background px-2.5 py-1.5 shadow-xl" />,
        document.body,
      )}
    </div>
  );
}

/** Live: the request log's new requests, read every POLL_MS while the page is seen, each played as a flight a few
 * seconds after it came. The log is read by its rows' ids, newest first, so the browser's clock does not matter. The
 * first read, and the first after the page was hidden, only note where the log is, and count the last minute's by
 * the service's clock (``until``); the rest are all counted, those of models not drawn too. */
function useLiveFlights(live: boolean, until: number | undefined) {
  const client = useApi();
  const state = useRef<Live>({ flights: [], came: [] });
  const clock = useRef(until);
  clock.current = until;
  useEffect(() => {
    state.current = { flights: [], came: [] };
    if (!live) return;
    let lastId: number | null = null;
    let stopped = false;
    const read = async () => {
      if (document.hidden) {
        lastId = null;
        return;
      }
      try {
        const reply = await client.get("/admin/requests", { params: { query: { limit: lastId == null ? 500 : 200 } } });
        if (stopped || !reply.requests.length) return;
        const now = performance.now();
        const s = state.current;
        if (lastId == null) {
          const at = clock.current ?? reply.requests[0].ts;
          s.flights = [];
          s.came = reply.requests
            .filter((r) => r.ts > at - 60)
            .map((r) => ({ at: now - Math.max(0, at - r.ts) * 1000, source: sourceOf(r.source) }))
            .sort((a, b) => a.at - b.at);
          lastId = reply.requests[0].id;
          return;
        }
        const rows = reply.requests.filter((r) => r.id > lastId!).sort((a, b) => a.id - b.id);
        if (!rows.length) return;
        lastId = rows[rows.length - 1].id;
        // Played over the next poll's time, as they were spaced, squeezed in if they came over longer.
        const first = rows[0].ts;
        const spread = Math.max(1, (rows[rows.length - 1].ts - first) * 1000);
        for (const r of rows) {
          const at = now + (r.ts - first) * 1000 * Math.min(1, (POLL_MS * 0.9) / spread);
          s.flights.push({ id: r.id, station: r.model_id, born: at, end: ending(r.status, r.error) });
          s.came.push({ at, source: sourceOf(r.source) });
        }
      } catch {
        /* the request log is off or the service went away: nothing to play */
      }
    };
    read();
    const timer = setInterval(read, POLL_MS);
    return () => {
      stopped = true;
      clearInterval(timer);
    };
  }, [live, client]);
  return state;
}

const inside = (b: { x: number; y: number; w: number; h: number }, x: number, y: number) => x >= b.x && x <= b.x + b.w && y >= b.y && y <= b.y + b.h;
const ease = (t: number) => (t < 0.5 ? 2 * t * t : 1 - (-2 * t + 2) ** 2 / 2);

function showTip(
  el: HTMLDivElement, under: RouteStation | "router" | null, p: { clientX: number; clientY: number } | null, source: RoutingSource,
  label: (task: string) => string, counts: Map<Source, number>,
) {
  if (!under || !p) {
    el.hidden = true;
    return;
  }
  const html = under === "router" ? routerTip(source, counts) : source.window ? windowTip(under, source.window, label) : nowTip(under, source.view, label);
  if (el.innerHTML !== html) el.innerHTML = html;
  el.hidden = false;
  const x = p.clientX + 14 + el.offsetWidth > window.innerWidth ? p.clientX - 14 - el.offsetWidth : p.clientX + 14;
  const y = p.clientY + 14 + el.offsetHeight > window.innerHeight ? p.clientY - 14 - el.offsetHeight : p.clientY + 14;
  el.style.left = `${Math.max(4, x)}px`;
  el.style.top = `${Math.max(4, y)}px`;
}

/** The router's tooltip: where the requests of the last minute (or the window) came from. */
function routerTip(source: RoutingSource, counts: Map<Source, number>): string {
  const when = source.window ? source.window.name : "minute";
  return tooltipHtml(`Router · the last ${when}`, SOURCES.map((s): [string, string] => [s.label, fmtInt(counts.get(s.key) ?? 0)]));
}

/** A card's tooltip now: its task, then its lane's (where it runs, its queue), or that it is not loaded. */
function nowTip(s: RouteStation, view: QueueView, label: (task: string) => string): string {
  const lane = view.lanes.find((l) => l.model === s.id);
  return tooltipHtml(s.id, [["Task", label(s.task)], ...(lane ? lane.tip : [["State", "not loaded: the first request loads it"] as [string, string]])]);
}

/** A card's tooltip over the window: its task, where it runs as far as known, and its figures. */
function windowTip(s: RouteStation, window: NonNullable<RoutingSource["window"]>, label: (task: string) => string): string {
  const f = window.figures.get(s.id);
  const rows: [string, string][] = [["Task", label(s.task)]];
  if (s.badge) rows.push(["Runs", s.badge.text]);
  if (!f?.requests) return tooltipHtml(s.id, [...rows, ["Requests", `none in the last ${window.name}`]]);
  return tooltipHtml(s.id, [
    ...rows, ["Requests", fmtInt(f.requests)], ["Failed", `${fmtInt(f.failed)} (${fmtPct(f.failed / f.requests)})`],
    ["p95", fmtMs(f.p95)], ["Cold starts", fmtInt(f.cold)],
  ]);
}

/** ``color`` faded to ``alpha`` over ``base``, as an opaque color, so faded lines that overlap show no seam. */
function fade(color: string, base: string, alpha: number): string {
  if (alpha >= 0.995) return color;
  const [r, g, b] = (color.match(/[\d.]+/g) ?? []).map(Number);
  const [r0, g0, b0] = (base.match(/[\d.]+/g) ?? []).map(Number);
  const at = (v: number, v0: number) => Math.round(v0 + (v - v0) * alpha);
  return `rgb(${at(r, r0)}, ${at(g, g0)}, ${at(b, b0)})`;
}

/** ``text`` cut to ``room`` px, with an ellipsis. */
function fit(ctx: CanvasRenderingContext2D, text: string, room: number): string {
  if (ctx.measureText(text).width <= room) return text;
  let lo = 0;
  let hi = text.length;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (ctx.measureText(text.slice(0, mid) + "…").width <= room) lo = mid;
    else hi = mid - 1;
  }
  return text.slice(0, lo) + "…";
}

function dot(ctx: CanvasRenderingContext2D, c: Colors, x: number, y: number, r: number, color: string, hollow: boolean) {
  ctx.beginPath();
  ctx.arc(x, y, r, 0, TAU);
  ctx.fillStyle = hollow ? c.card : color;
  ctx.fill();
  ctx.lineWidth = hollow ? 1.5 : 1.25;
  ctx.strokeStyle = hollow ? color : c.card;
  ctx.stroke();
}

function draw(
  ctx: CanvasRenderingContext2D, map: RoutingLayout, source: RoutingSource, { flights: live, came }: Live, shown: Map<number, Pt>,
  c: Colors, label: (task: string) => string, dt: number, now: number, under: RouteStation | null, overRouter: boolean,
): Map<Source, number> {
  ctx.clearRect(0, 0, map.width, map.height);
  ctx.lineCap = "round";
  ctx.lineJoin = "round";
  ctx.textBaseline = "middle";
  const lanes = new Map<string, Lane>(source.view.lanes.map((l) => [l.model, l]));
  const hue = (s: RouteStation) => c.hues[s.hue];
  const on = (s: RouteStation) => lanes.has(s.id);

  // Over a window: a station's requests in it, and its task's; a line is as wide as its share of the busiest task's.
  const win = source.window;
  const used = (s: RouteStation) => win?.figures.get(s.id)?.requests ?? 0;
  const taskUse = map.lines.map((_, li) => map.stations.filter((s) => s.line === li).reduce((n, s) => n + used(s), 0));
  const busiest = Math.max(1, ...taskUse);
  const width = (n: number) => 1.5 + 4.5 * Math.sqrt(n / busiest);
  const lit = (s: RouteStation) => (win ? used(s) > 0 : on(s));
  const stroke = (path: Path, color: string, w: number) => {
    ctx.strokeStyle = color;
    ctx.lineWidth = w;
    ctx.beginPath();
    path.pts.forEach((p, i) => (i ? ctx.lineTo(p.x, p.y) : ctx.moveTo(p.x, p.y)));
    ctx.stroke();
  };
  // The lines: to each card, the faded (a model not loaded now, or not used in the window) first so a lit one runs
  // over them; over a window, each task's trunk first, as wide as all its requests.
  if (win) map.lines.forEach((line, li) => stroke(line.edge, fade(c.hues[line.hue], c.card, taskUse[li] ? 1 : 0.3), width(taskUse[li])));
  for (const s of [...map.stations].sort((a, b) => Number(lit(a)) - Number(lit(b)))) {
    stroke(s.route, fade(hue(s), c.card, lit(s) ? 1 : 0.3), win ? width(used(s)) : 2);
  }
  // The requests: those on their way go under the router and the cards, so they come out of the one and into the
  // other; those in a card, waiting in its row or going into its pill, over it.
  const t = source.mode === "demo" ? source.sim.now : now;
  const over: (() => void)[] = [];
  if (win) {
    // A flow, not requests: as many dots along a line as it is busy, at one pace.
    for (const s of map.stations) {
      const n = used(s);
      if (!n) continue;
      const k = Math.min(4, Math.ceil(4 * Math.sqrt(n / busiest)));
      for (let i = 0; i < k; i++) {
        const at = s.route.at((((now / FLOW_MS) + i / k) % 1) * s.route.length);
        dot(ctx, c, at.x, at.y, 3, hue(s), false);
      }
    }
  } else if (source.mode === "demo") {
    const seen = new Set<number>();
    source.sim.stations.forEach((st, i) => {
      const s = map.stations.find((x) => x.id === source.sim.models[i].id);
      if (!s) return;
      let k = 0;
      for (const item of st.queue) {
        seen.add(item.id);
        const age = t - item.born;
        if (age < HOP_MS) {
          const at = s.route.at(ease(age / HOP_MS) * s.route.length);
          shown.set(item.id, at);
          dot(ctx, c, at.x, at.y, 4, hue(s), !item.high);
          continue;
        }
        // In its place in the row, moving up as those ahead are taken; those past the room are counted.
        const place = k++;
        if (place >= s.slots) continue;
        const to = s.slotAt(place);
        const from = shown.get(item.id) ?? to;
        const at = { x: from.x + (to.x - from.x) * Math.min(1, dt / 90), y: from.y + (to.y - from.y) * Math.min(1, dt / 90) };
        shown.set(item.id, at);
        over.push(() => dot(ctx, c, at.x, at.y, 3.5, hue(s), !item.high));
      }
      // Taken: still on its way, it carries on to the card; there, it goes into the pill.
      for (const item of st.running?.items ?? []) {
        seen.add(item.id);
        const age = t - item.born;
        const pill = { x: s.pill.x + s.pill.w / 2, y: s.pill.y + s.pill.h / 2 };
        if (age < HOP_MS) {
          const at = s.route.at(ease(age / HOP_MS) * s.route.length);
          shown.set(item.id, at);
          dot(ctx, c, at.x, at.y, 4, hue(s), !item.high);
          continue;
        }
        const from = shown.get(item.id) ?? s.route.at(s.route.length);
        if (Math.hypot(from.x - pill.x, from.y - pill.y) < 2) continue;
        const at = { x: from.x + (pill.x - from.x) * Math.min(1, dt / 70), y: from.y + (pill.y - from.y) * Math.min(1, dt / 70) };
        shown.set(item.id, at);
        over.push(() => dot(ctx, c, at.x, at.y, 3.5, hue(s), !item.high));
      }
    });
    for (const id of shown.keys()) if (!seen.has(id)) shown.delete(id);
    // Those that did not end well, as the live ones do.
    for (const e of source.sim.ended) {
      const s = map.stations.find((x) => x.id === source.sim.models[e.station].id);
      if (s) flight(ctx, c, s, e.end, t - e.born, hue(s), over);
    }
  } else {
    for (let i = live.length - 1; i >= 0; i--) {
      const f = live[i];
      const s = map.stations.find((x) => x.id === f.station);
      if (!s || !flight(ctx, c, s, f.end, now - f.born, hue(s), over)) live.splice(i, 1);
    }
  }

  // The router, with how many came in the last minute, or the window; from where, its tooltip says.
  while (came.length && came[0].at <= now - 60_000) came.shift();
  const counts = new Map<Source, number>();
  if (win) {
    for (const [key, n] of Object.entries(win.sources)) counts.set(sourceOf(key), (counts.get(sourceOf(key)) ?? 0) + n);
  } else {
    for (const e of source.mode === "demo" ? source.sim.sent : came) {
      if (e.at <= t && e.at > t - 60_000) counts.set(e.source, (counts.get(e.source) ?? 0) + 1);
    }
  }
  const total = [...counts.values()].reduce((a, b) => a + b, 0);
  drawRouter(ctx, c, map.router, win ? `${fmtCompact(total)} in ${win.short}` : `${fmtInt(total)} a minute`, overRouter);

  // Each task's name along its line, with how many of its requests wait or run; cut to its room.
  map.lines.forEach((line, li) => {
    const open = source.view.lanes.filter((l) => l.task === line.task).reduce((n, l) => n + waitingCount(l) + l.running, 0);
    const count = win ? fmtCompact(taskUse[li]) : fmtInt(open);
    ctx.font = `600 9.5px ${c.font}`;
    const cw = ctx.measureText(count).width;
    ctx.font = `500 10px ${c.font}`;
    const name = fit(ctx, label(line.task), line.label.room - cw - 5);
    const nw = ctx.measureText(name).width;
    const left = line.label.align === "left" ? line.label.x : line.label.x - nw - 5 - cw;
    // Over a window, clear of its line however wide it is.
    const y = line.label.y - (win ? width(taskUse[li]) / 2 - 1 : 0);
    text(ctx, c, name, left, y, "left", "500 10px", c.foreground);
    text(ctx, c, count, left + nw + 5, y, "left", "600 9.5px", c["muted-foreground"]);
  });

  // The cards, each with its model's queue, and the requests in them.
  for (const s of map.stations) drawCard(ctx, s, lanes.get(s.id) ?? null, source, c, hue(s), t, s === under);

  for (const drawOver of over) drawOver();
  return counts;
}

/** The router: a card with its name and how many came (``count``: in the last minute, or the window). */
function drawRouter(ctx: CanvasRenderingContext2D, c: Colors, r: Box, count: string, hovered: boolean) {
  ctx.save();
  ctx.shadowColor = "rgba(0, 0, 0, 0.12)";
  ctx.shadowBlur = 14;
  ctx.shadowOffsetY = 3;
  ctx.beginPath();
  ctx.roundRect(r.x, r.y, r.w, r.h, 12);
  ctx.fillStyle = c.card;
  ctx.fill();
  ctx.restore();
  ctx.lineWidth = hovered ? 1.5 : 1;
  ctx.strokeStyle = hovered ? c.foreground : c.axis;
  ctx.stroke();
  text(ctx, c, "Router", r.x + r.w / 2, r.y + 17, "center", "600 11.5px", c.foreground);
  text(ctx, c, count, r.x + r.w / 2, r.y + 32, "center", "10px", c["muted-foreground"]);
}

/** A request ``age`` ms after it came, going to the model of ``s``: it hops to the card and into its pill; one that did
 * not end well shows at the card in its ending's color: refused (grey), turned away (amber) or failed (red). False once
 * it is done. */
function flight(ctx: CanvasRenderingContext2D, c: Colors, s: RouteStation, end: Ending, age: number, hue: string, over: (() => void)[]): boolean {
  if (age > HOP_MS + 600) return false;
  if (age < 0) return true;
  const color = end === "ok" ? hue : end === "refused" ? c["muted-foreground"] : end === "busy" ? c.warning : c.critical;
  if (age < HOP_MS) {
    const at = s.route.at(ease(age / HOP_MS) * s.route.length);
    dot(ctx, c, at.x, at.y, 4, color, false);
  } else if (end !== "ok") {
    const at = s.route.at(s.route.length);
    over.push(() => {
      ctx.globalAlpha = 1 - (age - HOP_MS) / 600;
      dot(ctx, c, at.x, at.y, 4 + (age - HOP_MS) / 120, color, false);
      ctx.globalAlpha = 1;
    });
  }
  return true;
}

function text(ctx: CanvasRenderingContext2D, c: Colors, value: string, x: number, y: number, align: CanvasTextAlign, font: string, color: string) {
  ctx.font = `${font} ${c.font}`;
  ctx.textAlign = align;
  ctx.fillStyle = color;
  ctx.fillText(value, x, y);
}

/** A model's card: its name; the batch it runs in a pill, with how far it has got; its waiting requests in a row
 * (live, only how many); amber when it backs up, faded and dashed when not loaded. */
function drawCard(ctx: CanvasRenderingContext2D, s: RouteStation, lane: Lane | null, source: RoutingSource, c: Colors, hue: string, t: number, hovered: boolean) {
  const { x, y, w, h } = s.card;
  // Over a window there is no queue: faded when the window used it not, never dashed (not loaded is only now).
  const win = source.window;
  const figures = win?.figures.get(s.id);
  ctx.globalAlpha = (win ? figures?.requests : lane) ? 1 : 0.5;
  ctx.beginPath();
  ctx.roundRect(x, y, w, h, 8);
  ctx.fillStyle = !win && lane?.backingUp ? fade(c.warning, c.card, 0.14) : c.card;
  ctx.fill();
  if (!win && !lane) ctx.setLineDash([3, 3]);
  ctx.lineWidth = hovered ? 1.5 : 1;
  ctx.strokeStyle = hovered ? c.foreground : !win && lane?.backingUp ? c.warning : c.axis;
  ctx.stroke();
  ctx.setLineDash([]);
  // Its name, and its badge: where it runs. One outside ("↗ ollama") is filled with a tint of its line's hue; the
  // pills are outlined, the badges filled.
  let room = w - 20;
  if (s.badge) {
    const font = s.badge.away ? "600 9.5px" : "9.5px";
    ctx.font = `${font} ${c.font}`;
    const label = fit(ctx, s.badge.text, BADGE_MAX);
    const bw = Math.ceil(ctx.measureText(label).width) + 12;
    // Its own row over the pill's, a gap between them.
    const b = { x: x + w - 7 - bw, y: y + 6, w: bw, h: 13 };
    ctx.beginPath();
    ctx.roundRect(b.x, b.y, b.w, b.h, 4);
    ctx.fillStyle = s.badge.away ? fade(hue, c.card, 0.25) : c.muted;
    ctx.fill();
    text(ctx, c, label, b.x + b.w / 2, b.y + b.h / 2 + 0.5, "center", font, s.badge.away ? c.foreground : c["muted-foreground"]);
    room -= bw + 8;
  }
  ctx.font = `500 11px ${c.font}`;
  text(ctx, c, fit(ctx, s.id, room), x + 10, y + 13, "left", "500 11px", c.foreground);
  if (win) {
    windowFiguresRow(ctx, c, s, figures, win.name);
    ctx.globalAlpha = 1;
    return;
  }
  // The pill: what it runs, or that it loads.
  const p = s.pill;
  ctx.beginPath();
  ctx.roundRect(p.x, p.y, p.w, p.h, 4);
  ctx.fillStyle = c.card;
  ctx.fill();
  ctx.lineWidth = 1;
  ctx.strokeStyle = fade(hue, c.card, lane ? 0.8 : 0.4);
  ctx.stroke();
  const busy = lane ? (lane.loading ? "···" : lane.running ? `×${lane.running}` : "idle") : "–";
  text(ctx, c, busy, p.x + p.w / 2, p.y + p.h / 2 + 0.5, "center", "600 9.5px", lane?.running || lane?.loading ? c.foreground : c["muted-foreground"]);
  if (lane?.progress != null) {
    ctx.fillStyle = hue;
    ctx.fillRect(p.x + 3, p.y + p.h - 2, (p.w - 6) * Math.min(1, lane.progress), 1.5);
  }
  // Beside it: those waiting live (only how many), or what it is doing when it waits on nothing.
  const after = s.slotAt(0);
  const right = s.pill.x < x + w / 2;
  if (!lane) {
    text(ctx, c, "not loaded", after.x - (right ? 4 : -4), after.y, right ? "left" : "right", "10px", c["muted-foreground"]);
  } else if (lane.loading && !waitingCount(lane)) {
    text(ctx, c, "loading", after.x - (right ? 4 : -4), after.y, right ? "left" : "right", "10px", c["muted-foreground"]);
  } else if (source.mode === "live") {
    const n = waitingCount(lane);
    for (let k = 0; k < Math.min(n, s.slots); k++) {
      const at = s.slotAt(k);
      dot(ctx, c, at.x, at.y, 3.5, fade(hue, c.card, 0.8), false);
    }
    if (n > s.slots) more(ctx, c, s, n - s.slots, right);
  } else {
    // The demo's are drawn one by one (draw); here, how many are past the room.
    const queue = source.sim.stations[source.sim.models.findIndex((m) => m.id === s.id)]?.queue ?? [];
    const placed = queue.filter((item) => t - item.born >= HOP_MS).length;
    if (placed > s.slots) more(ctx, c, s, placed - s.slots, right);
  }
  ctx.globalAlpha = 1;
}

/** A card's figures over the window, on the row the pill takes now, from the side its line comes in: its requests,
 * the share that failed (in red, when any did) and its p95. */
function windowFiguresRow(ctx: CanvasRenderingContext2D, c: Colors, s: RouteStation, f: Figures | undefined, name: string) {
  const y = s.pill.y + s.pill.h / 2 + 0.5;
  const parts: [string, string][] = f?.requests
    ? [
      [`${fmtCompact(f.requests)} requests`, c.foreground],
      ...(f.failed ? [[` · ${fmtPct(f.failed / f.requests)} failed`, c.critical] as [string, string]] : []),
      [` · p95 ${fmtMs(f.p95)}`, c["muted-foreground"]],
    ]
    : [[`none in the last ${name}`, c["muted-foreground"]]];
  ctx.font = `10px ${c.font}`;
  const widths = parts.map(([words]) => ctx.measureText(words).width);
  const right = s.pill.x > s.card.x + s.card.w / 2;
  let x = right ? s.card.x + s.card.w - 10 - widths.reduce((a, b) => a + b, 0) : s.card.x + 10;
  parts.forEach(([words, color], i) => {
    text(ctx, c, words, x, y, "left", "10px", color);
    x += widths[i];
  });
}

/** The widest a card's badge's words are; a longer name is cut */
const BADGE_MAX = 84;

/** "+N" after the last waiting request drawn in a card. */
function more(ctx: CanvasRenderingContext2D, c: Colors, s: RouteStation, n: number, right: boolean) {
  const last = s.slotAt(s.slots - 1);
  text(ctx, c, `+${n}`, last.x + (right ? 8 : -8), last.y + 0.5, right ? "left" : "right", "600 9.5px", c["muted-foreground"]);
}
