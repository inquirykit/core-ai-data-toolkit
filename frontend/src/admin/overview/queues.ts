// The Overview's queues card: a lane per loaded model, with the batch it runs and the requests waiting for it, which
// is where the service queues them (models/cache.py, models/batching.py). Live, from GET /admin/stats: each loaded
// model's waiting, in_use and estimated_wait_ms, the models the window used but not loaded listed apart. Demo: a
// sample set of models with simulated requests, slowed down to be seen, run as the model manager does: a request is in
// its model's line the moment it is sent, the interactive before the background; when the model is free it takes the
// most urgent as one batch, one batch at a time; a model not loaded is loaded by the first request that names it, and
// unloaded once idle. Both give the same view, which QueuesCard.tsx draws as lanes; RoutingView.tsx draws the same
// queues as a routing picture.
import { fmtInt, fmtMs } from "../../shared/format";
import type { Stats } from "../../shared/types";
import type { Ending, Source } from "./routing";

/** A lane: one loaded (or loading) model. */
export interface Lane {
  key: string;
  model: string;
  task: string;
  /** The provider or server outside that answers it; null when it runs here */
  via: string | null;
  /** That server is another toolkit */
  peer: boolean;
  /** Runs here in a process of its own (a server this service started, an AutoGluon predictor) */
  own: boolean;
  loading: boolean;
  /** Requests it runs now */
  running: number;
  /** How far its batch, or its load, has got, 0 to 1; null when not known (live) */
  progress: number | null;
  /** Those waiting for it, the next first: each one's priority in the demo; live, only how many */
  waiting: { id: number; high: boolean }[] | number;
  /** How long the next request would wait; null before anything says */
  waitMs: number | null;
  /** Backing up: more is waiting than it takes in one go (demo), or the next request would wait SLOW_WAIT_MS (live) */
  backingUp: boolean;
  /** What its tooltip lists */
  tip: [string, string][];
}

export interface QueueView {
  lanes: Lane[];
  /** Models not loaded: those the window used (live), or the sample's resting ones (demo) */
  resting: string[];
  /** Loaded models past MAX_LANES, not drawn */
  hidden: number;
  /** Requests waiting, over every lane */
  waiting: number;
  /** The longest wait the next request to a model would have, over the lanes; null when none is known */
  longestWaitMs: number | null;
}

export const MAX_LANES = 12;
/** Live, a model is backing up when the next request to it would wait this long */
export const SLOW_WAIT_MS = 1000;

export const waitingCount = (lane: Lane) => (typeof lane.waiting === "number" ? lane.waiting : lane.waiting.length);

/** The lanes and the view's totals. */
function view(all: Lane[], resting: string[]): QueueView {
  const lanes = all.slice(0, MAX_LANES);
  const waits = lanes.map((lane) => lane.waitMs).filter((ms): ms is number => ms != null);
  return {
    lanes,
    resting,
    hidden: all.length - lanes.length,
    waiting: all.reduce((n, lane) => n + waitingCount(lane), 0),
    longestWaitMs: waits.length ? Math.max(...waits) : null,
  };
}

// ------------------------------------------------------------------ live
/** Where a model runs, as its tooltip says it */
const runs = (via: string | null, own: boolean, peer = false) =>
  via ? `outside, on ${via}${peer ? ", another toolkit" : ""}` : own ? "here, in a process of its own" : "here, in this service";
/** The models the window used, less those it only refused as wrong (a mistyped ID is no model to draw). */
const used = (stats: Stats | null) => (stats?.models ?? []).filter((m) => m.requests > m.client_errors);

/** The service's loaded models as lanes, by task and then model, an order that holds from one poll to the next. */
export function liveView(stats: Stats | null): QueueView {
  const loaded = Object.values(stats?.cache.model_details ?? {}).sort(
    (a, b) => a.task.localeCompare(b.task) || a.model_id.localeCompare(b.model_id),
  );
  const lanes = loaded.map((m): Lane => {
    const running = Math.max(0, m.in_use - m.waiting);
    const waitMs = m.estimated_wait_ms ?? null;
    const via = m.upstream ?? null;
    const own = !via && m.kind === "server";
    const peer = !!m.peer;
    return {
      key: `${m.model_id}|${m.task}`, model: m.model_id, task: m.task, via, peer, own, loading: false, running, progress: null,
      waiting: m.waiting, waitMs, backingUp: (waitMs ?? 0) >= SLOW_WAIT_MS,
      tip: [
        ["Runs", runs(via, own, peer)],
        ...(m.backend ? [["Backend", m.backend] as [string, string]] : []),
        ...(via ? [] : [["Device", m.device] as [string, string]]),
        // A model that batches answers those running in one pass, however many.
        ["Running", m.batch_size ? `${fmtInt(running)} in one pass of up to ${fmtInt(m.batch_size)}` : `${fmtInt(running)} of ${fmtInt(m.max_concurrency)} at once`],
        ["Waiting", fmtInt(m.waiting)],
        [m.batch_size ? "Average pass" : "Average request", fmtMs(m.avg_request_ms)],
        ["Next request waits", fmtMs(waitMs)],
      ],
    };
  });
  const ids = new Set(loaded.map((m) => m.model_id));
  const resting = [...new Set(used(stats).map((m) => m.model_id).filter((id) => !ids.has(id)))].sort();
  return view(lanes, resting);
}

export interface RouteModel {
  id: string;
  task: string;
  loaded: boolean;
  /** The provider or server outside that answers it, once loaded */
  via: string | null;
  /** That server is another toolkit */
  peer: boolean;
  /** Runs here in a process of its own, once loaded */
  own: boolean;
  /** The device it runs on here, once loaded */
  device: string | null;
}

/** The models the routing picture draws, MAX_LANES in all, by task and then model, and how many more there are: now,
 * the loaded ones and then those the window used most; over the window (``byUse``), those it used most, a loaded one it
 * did not use last. */
export function liveModels(stats: Stats | null, byUse = false): { models: RouteModel[]; hidden: number } {
  const all = new Map<string, RouteModel & { requests: number }>();
  for (const m of used(stats)) {
    const seen = all.get(m.model_id);
    all.set(m.model_id, { id: m.model_id, task: seen?.task ?? m.task, loaded: false, via: null, peer: false, own: false, device: null, requests: (seen?.requests ?? 0) + m.requests });
  }
  for (const m of Object.values(stats?.cache.model_details ?? {})) {
    const via = m.upstream ?? null;
    const own = !via && m.kind === "server";
    const requests = all.get(m.model_id)?.requests ?? 0;
    all.set(m.model_id, { id: m.model_id, task: m.task, loaded: true, via, peer: !!m.peer, own, device: m.device, requests });
  }
  const ranked = [...all.values()].sort((a, b) =>
    byUse ? b.requests - a.requests || Number(b.loaded) - Number(a.loaded) : Number(b.loaded) - Number(a.loaded) || b.requests - a.requests);
  const models = ranked
    .slice(0, MAX_LANES)
    .map(({ requests: _, ...m }) => m)
    .sort((a, b) => a.task.localeCompare(b.task) || a.id.localeCompare(b.id));
  return { models, hidden: ranked.length - models.length };
}

/** A model's figures over the window: its requests, how many failed (refused or not), its p95 and its cold starts */
export interface Figures {
  requests: number;
  failed: number;
  p95: number | null;
  cold: number;
}

/** Each model's figures over the stats' window, by model ID (a model used for two tasks adds them up). */
export function windowFigures(stats: Stats | null): Map<string, Figures> {
  const by = new Map<string, Figures>();
  for (const m of stats?.models ?? []) {
    const seen = by.get(m.model_id) ?? { requests: 0, failed: 0, p95: null, cold: 0 };
    by.set(m.model_id, {
      requests: seen.requests + m.requests, failed: seen.failed + m.errors + m.client_errors,
      p95: Math.max(seen.p95 ?? 0, m.p95_ms ?? 0) || null, cold: seen.cold + m.cold_starts,
    });
  }
  return by;
}

// ------------------------------------------------------------------ demo
export interface Station {
  id: string;
  task: string;
  loaded: boolean;
  /** Its share of the traffic */
  weight: number;
  /** The provider or server outside that answers it */
  via?: string;
  /** That server is another toolkit */
  peer?: boolean;
  /** Runs here in a process of its own */
  own?: boolean;
}

const sample = (task: string, id: string, loaded: boolean, weight: number, where: Partial<Station> = {}): Station =>
  ({ id, task, loaded, weight, ...where });

/** The demo's models: one for every kind of task the service has, by IDs the model files serve (models/families), run
 * in the service, in a process of its own (AutoGluon's), or outside: by a provider, or by another toolkit. */
export const SAMPLE: Station[] = [
  sample("text-classification", "AnkitAI/Sensible-ModernBERT-Sentiment-Analysis", true, 5),
  sample("embedding", "ibm-granite/granite-embedding-small-english-r2", true, 6),
  // Answered by another toolkit, on a machine with a bigger GPU.
  sample("embedding", "BAAI/bge-m3", true, 2, { via: "gpu-box", peer: true }),
  sample("rerank", "Alibaba-NLP/gte-reranker-modernbert-base", true, 3),
  sample("zero-shot-classification", "MoritzLaurer/deberta-v3-base-zeroshot-v2.0", true, 2),
  sample("zero-shot-classification", "facebook/bart-large-mnli", false, 1),
  sample("speech-to-text", "UsefulSensors/moonshine-tiny", false, 1),
  sample("text-generation", "LiquidAI/LFM2.5-1.2B-Instruct", true, 1),
  // Answered outside: by Ollama on another machine, and by a provider's API.
  sample("text-generation", "qwen3:8b", true, 1, { via: "ollama" }),
  sample("text-generation", "gpt-4.1-mini", true, 1, { via: "openai" }),
  sample("question-answering", "deepset/tinyroberta-squad2", true, 2),
  sample("vision-language", "apple/FastVLM-1.5B", true, 1),
  sample("decision", "vega-0.8b", true, 1),
  sample("tabular-prediction", "autogluon:churn", true, 2, { own: true }),
  sample("timeseries-forecast", "autogluon:daily-sales", true, 1, { own: true }),
  sample("custom", "custom:echo", true, 1),
];

/** Requests per second, on average, before the bursts */
const RATE = 3;
/** The share of requests that name a model not loaded, which loads it */
const COLD_SHARE = 0.02;
/** A model the demo loaded is unloaded after this long idle (the cache's idle unload) */
export const IDLE_MS = 22000;
const MAX_WAITING = 300;
/** The shares of requests refused as wrong, and of those that fail */
const REFUSED_SHARE = 0.03;
const FAILED_SHARE = 0.02;
/** A model turns a new request away when this many batches wait for it already (as MODEL_WAIT_TIMEOUT's 503 does) */
const BUSY_BATCHES = 4;
/** How long a request that did not end well is kept, for the picture to show where it stopped */
const ENDED_MS = 4000;

/** How long a model takes, slowed down to be seen: base + per request of the batch, the largest batch, and a load. */
const TIMING: Record<string, { base: number; per: number; batch: number; load: number }> = {
  embedding: { base: 900, per: 60, batch: 16, load: 2400 },
  "text-classification": { base: 1100, per: 90, batch: 8, load: 2600 },
  rerank: { base: 1300, per: 120, batch: 8, load: 2800 },
  "zero-shot-classification": { base: 1500, per: 160, batch: 8, load: 3000 },
  "speech-to-text": { base: 2200, per: 500, batch: 4, load: 3400 },
  "text-generation": { base: 2600, per: 400, batch: 4, load: 3800 },
  "question-answering": { base: 1200, per: 120, batch: 8, load: 2600 },
  "vision-language": { base: 2400, per: 600, batch: 2, load: 3800 },
  decision: { base: 1800, per: 300, batch: 4, load: 3400 },
  "tabular-prediction": { base: 700, per: 40, batch: 16, load: 3000 },
  "timeseries-forecast": { base: 1400, per: 150, batch: 8, load: 3200 },
  custom: { base: 500, per: 20, batch: 16, load: 1500 },
};
const DEFAULT_TIMING = { base: 1300, per: 110, batch: 8, load: 2800 };
export const timing = (task: string) => TIMING[task] ?? DEFAULT_TIMING;

/** A request: interactive (a client's) or background (a workflow's, an eval run's), taken after the interactive. */
export interface Item {
  id: number;
  high: boolean;
  /** When it was sent */
  born: number;
  /** Where it came from */
  source: Source;
}

/** A request that did not end well, sent to ``station`` (its index) at ``born`` */
export interface Ended {
  id: number;
  station: number;
  born: number;
  source: Source;
  end: Exclude<Ending, "ok">;
}

interface Batch {
  items: Item[];
  start: number;
  ms: number;
}

export interface StationState {
  loaded: boolean;
  /** When its load began, while loading */
  loading: number | null;
  /** Loaded by the demo, so unloaded again once idle */
  warm: boolean;
  queue: Item[];
  /** The batch it runs: one at a time, as a model's batcher does */
  running: Batch | null;
  lastActive: number;
  done: number;
  last: { n: number; ms: number } | null;
}

export class Simulation {
  now = 0;
  /** Whether it makes up requests of its own (the tests send theirs) */
  traffic = true;
  readonly stations: StationState[];
  /** When requests were sent in the last minute, and from where, the oldest first */
  readonly sent: { at: number; source: Source }[] = [];
  /** The requests of the last few seconds that did not end well: refused, turned away, or failed */
  ended: Ended[] = [];
  private nextId = 1;
  private nextBurst = 4000;
  private scheduled: { at: number; station: number; source: Source }[] = [];

  constructor(readonly models: Station[], private random: () => number = Math.random) {
    this.stations = models.map((m) => ({
      loaded: m.loaded, loading: null, warm: false, queue: [], running: null, lastActive: 0, done: 0, last: null,
    }));
  }

  /** Run ``ms`` at once, in steps: to fill the demo before it is first drawn. */
  advance(ms: number) {
    for (let t = 0; t < ms; t += 50) this.step(50);
  }

  step(dt: number) {
    dt = Math.min(dt, 100);
    this.now += dt;
    if (this.ended.length && this.ended[0].born < this.now - ENDED_MS) this.ended = this.ended.filter((e) => e.born >= this.now - ENDED_MS);
    this.spawn(dt);
    this.stations.forEach((st, i) => this.serve(st, i));
  }

  /** Send a request to ``station``: in its model's line at once, after the interactive ones there if it is one. */
  send(station: number, high = this.random() < 0.75, source: Source = high ? "api" : "workflow"): Item {
    const item = { id: this.nextId++, high, born: this.now, source };
    this.noteSent(source);
    const { queue } = this.stations[station];
    const after = high ? queue.findIndex((other) => !other.high) : -1;
    if (after < 0) queue.push(item);
    else queue.splice(after, 0, item);
    return item;
  }

  /** Where a request goes: a loaded model by its share of the traffic; now and then one not loaded, which loads it. */
  private pick(cold: boolean): number {
    const all = this.stations.map((_, i) => i);
    const ready = all.filter((i) => this.stations[i].loaded);
    const resting = all.filter((i) => !this.stations[i].loaded);
    const pool = cold && resting.length ? resting : ready.length ? ready : resting;
    let r = this.random() * pool.reduce((sum, i) => sum + this.models[i].weight, 0);
    for (const i of pool) if ((r -= this.models[i].weight) < 0) return i;
    return pool[pool.length - 1];
  }

  private spawn(dt: number) {
    if (!this.traffic || this.stations.reduce((n, st) => n + st.queue.length, 0) >= MAX_WAITING) return;
    const rate = RATE * (1 + 0.6 * Math.sin(this.now / 7000));
    // One at a time: an app's or the playground's, or in the background a table's embedding or an eval's.
    if (this.random() < (rate * dt) / 1000) {
      const station = this.pick(this.random() < COLD_SHARE);
      const high = this.random() < 0.75;
      this.arrive(station, high, high ? (this.random() < 0.8 ? "api" : "admin") : this.random() < 0.6 ? "data" : "eval");
    }
    // Now and then a burst: a workflow or an eval run sending a page of background requests to one model at once.
    if (this.now >= this.nextBurst) {
      const station = this.pick(false);
      const n = 8 + Math.floor(this.random() * 18);
      const source = this.random() < 0.6 ? "workflow" : "eval";
      for (let k = 0; k < n; k++) this.scheduled.push({ at: this.now + k * 40, station, source });
      this.nextBurst = this.now + 6000 + this.random() * 7000;
    }
    while (this.scheduled.length && this.scheduled[0].at <= this.now) {
      const { station, source } = this.scheduled.shift()!;
      this.arrive(station, false, source);
    }
  }

  private noteSent(source: Source) {
    this.sent.push({ at: this.now, source });
    while (this.sent[0].at < this.now - 60_000) this.sent.shift();
  }

  /** A request of the demo's own traffic: now and then refused as wrong, or failed; turned away by a model with too many
   * batches waiting already; else sent. */
  private arrive(station: number, high: boolean, source: Source) {
    const r = this.random();
    const busy = this.stations[station].queue.length >= BUSY_BATCHES * timing(this.models[station].task).batch;
    const end = r < REFUSED_SHARE ? "refused" : busy ? "busy" : r < REFUSED_SHARE + FAILED_SHARE ? "failed" : null;
    if (!end) return void this.send(station, high, source);
    this.noteSent(source);
    this.ended.push({ id: this.nextId++, station, born: this.now, source, end });
  }

  private serve(st: StationState, i: number) {
    const t = timing(this.models[i].task);
    if (st.queue.length || st.running) st.lastActive = this.now;
    if (!st.loaded) {
      if (st.queue.length && st.loading == null) st.loading = this.now;
      if (st.loading != null && this.now - st.loading >= t.load) {
        st.loaded = true;
        st.warm = true;
        st.loading = null;
      }
    }
    if (st.running && this.now - st.running.start >= st.running.ms) {
      st.done += st.running.items.length;
      st.last = { n: st.running.items.length, ms: st.running.ms };
      st.running = null;
    }
    if (st.loaded && !st.running && st.queue.length) {
      const items = st.queue.splice(0, t.batch);
      st.running = { items, start: this.now, ms: t.base + t.per * items.length };
    }
    if (st.warm && !st.queue.length && !st.running && this.now - st.lastActive > IDLE_MS) {
      st.loaded = false;
      st.warm = false;
    }
  }

  /** The demo as lanes: the models loaded or loading, the rest listed as resting. */
  view(): QueueView {
    const lanes: Lane[] = [];
    const resting: string[] = [];
    this.stations.forEach((st, i) => {
      const m = this.models[i];
      if (!st.loaded && st.loading == null) {
        resting.push(m.id);
        return;
      }
      const t = timing(m.task);
      const left = st.loading != null ? t.load - (this.now - st.loading) : st.running ? st.running.ms - (this.now - st.running.start) : 0;
      // The next request waits for what runs (or the load), then a full batch for each batch's worth before it.
      const waitMs = Math.max(0, left) + Math.floor(st.queue.length / t.batch) * (t.base + t.per * t.batch);
      const high = st.queue.filter((item) => item.high).length;
      lanes.push({
        key: m.id, model: m.id, task: m.task, via: m.via ?? null, peer: !!m.peer, own: !!m.own, loading: st.loading != null,
        running: st.running?.items.length ?? 0,
        progress: st.loading != null ? (this.now - st.loading) / t.load : st.running ? (this.now - st.running.start) / st.running.ms : null,
        waiting: st.queue.map((item) => ({ id: item.id, high: item.high })),
        waitMs, backingUp: st.queue.length > t.batch,
        tip: [
          ["Runs", runs(m.via ?? null, !!m.own, !!m.peer)],
          ["State", st.loading != null ? "loading" : "loaded"],
          ["Running", st.running ? `a batch of ${st.running.items.length}` : "nothing"],
          ["Waiting", `${fmtInt(high)} interactive · ${fmtInt(st.queue.length - high)} background`],
          ["Largest batch", fmtInt(t.batch)],
          ["Last batch", st.last ? `${st.last.n} in ${fmtMs(st.last.ms)}` : "–"],
          ["Next request waits", fmtMs(waitMs)],
        ],
      });
    });
    return view(lanes, resting);
  }
}
