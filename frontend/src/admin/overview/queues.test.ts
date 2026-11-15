import { describe, expect, it } from "vitest";
import type { Stats } from "../../shared/types";
import { IDLE_MS, liveModels, liveView, MAX_LANES, SAMPLE, Simulation, SLOW_WAIT_MS, timing, waitingCount, windowFigures, type Station } from "./queues";

type Loaded = { model_id: string; task: string; in_use: number; waiting: number; estimated_wait_ms?: number | null; batch_size?: number | null };
const loaded = (m: Loaded) => ({ device: "cpu", max_concurrency: 1, avg_request_ms: 20, estimated_wait_ms: null, ...m });
type Used = string | { model_id: string; requests: number; client_errors: number };
const stats = (details: Loaded[], used: Used[] = []) =>
  ({
    cache: { model_details: Object.fromEntries(details.map((d) => [d.model_id, loaded(d)])) },
    models: used.map((m) => ({ task: "embedding", ...(typeof m === "string" ? { model_id: m, requests: 3, client_errors: 0 } : m) })),
  }) as unknown as Stats;

describe("liveModels", () => {
  it("draws the loaded models, then those used most, MAX_LANES in all, by task and model, and counts the rest", () => {
    const busy = Array.from({ length: MAX_LANES }, (_, i) => ({ model_id: `used${String(i).padStart(2, "0")}`, requests: 100 - i, client_errors: 0 }));
    const { models, hidden } = liveModels(stats([{ model_id: "zz-loaded", task: "embedding", in_use: 0, waiting: 0 }], busy));
    expect(models).toHaveLength(MAX_LANES);
    expect(models.map((m) => m.id)).toEqual([...busy.slice(0, MAX_LANES - 1).map((m) => m.model_id), "zz-loaded"]);
    expect(models.find((m) => m.id === "zz-loaded")?.loaded).toBe(true);
    expect(hidden).toBe(1);
  });

  it("over a window, draws the models used most, a loaded one the window did not use after them", () => {
    const busy = Array.from({ length: MAX_LANES }, (_, i) => ({ model_id: `used${String(i).padStart(2, "0")}`, requests: 100 - i, client_errors: 0 }));
    const { models, hidden } = liveModels(stats([{ model_id: "zz-loaded", task: "embedding", in_use: 0, waiting: 0 }], busy), true);
    expect(models.map((m) => m.id)).toEqual(busy.map((m) => m.model_id));
    expect(hidden).toBe(1);
  });

  it("gives each model's figures over the window, its tasks added up", () => {
    const s = {
      cache: { model_details: {} },
      models: [
        { model_id: "a", task: "embedding", requests: 10, errors: 1, client_errors: 2, cold_starts: 1, p95_ms: 30 },
        { model_id: "a", task: "rerank", requests: 5, errors: 0, client_errors: 0, cold_starts: 0, p95_ms: 80 },
        { model_id: "b", task: "embedding", requests: 4, errors: 0, client_errors: 0, cold_starts: 0, p95_ms: null },
      ],
    } as unknown as Stats;
    const figures = windowFigures(s);
    expect(figures.get("a")).toEqual({ requests: 15, failed: 3, p95: 80, cold: 1 });
    expect(figures.get("b")).toEqual({ requests: 4, failed: 0, p95: null, cold: 0 });
  });

  it("leaves out a model whose every request was refused as wrong, a mistyped ID", () => {
    const used = [{ model_id: "typo", requests: 4, client_errors: 4 }, { model_id: "real", requests: 4, client_errors: 1 }];
    expect(liveModels(stats([], used)).models.map((m) => m.id)).toEqual(["real"]);
    expect(liveView(stats([], used)).resting).toEqual(["real"]);
  });
});

describe("liveView", () => {
  it("shows each loaded model's queue, by task and then model, the models used but not loaded apart", () => {
    const view = liveView(stats(
      [
        { model_id: "zeta", task: "text-classification", in_use: 5, waiting: 3 },
        { model_id: "gte", task: "embedding", in_use: 0, waiting: 0 },
        { model_id: "alpha", task: "text-classification", in_use: 1, waiting: 0 },
      ],
      ["gte", "qwen", "qwen"],
    ));
    expect(view.lanes.map((l) => [l.model, l.running, waitingCount(l)])).toEqual([["gte", 0, 0], ["alpha", 1, 0], ["zeta", 2, 3]]);
    expect([view.resting, view.waiting, view.hidden]).toEqual([["qwen"], 3, 0]);
  });

  it("marks a model backing up when the next request would wait a second or more", () => {
    const view = liveView(stats([
      { model_id: "slow", task: "rerank", in_use: 9, waiting: 8, estimated_wait_ms: SLOW_WAIT_MS },
      { model_id: "fast", task: "rerank", in_use: 2, waiting: 1, estimated_wait_ms: 40 },
    ]));
    expect(view.lanes.map((l) => [l.model, l.backingUp])).toEqual([["fast", false], ["slow", true]]);
    expect(view.longestWaitMs).toBe(SLOW_WAIT_MS);
  });

  it("says a model that batches runs those running in one pass", () => {
    const view = liveView(stats([
      { model_id: "bert", task: "text-classification", in_use: 16, waiting: 0, estimated_wait_ms: 80, batch_size: 32 },
      { model_id: "llm", task: "chat", in_use: 2, waiting: 1, estimated_wait_ms: 40 },
    ]));
    const tip = (model: string) => Object.fromEntries(view.lanes.find((l) => l.model === model)!.tip);
    expect([tip("bert").Running, tip("bert")["Average pass"]]).toEqual(["16 in one pass of up to 32", "20 ms"]);
    expect([tip("llm").Running, tip("llm")["Average request"]]).toEqual(["1 of 1 at once", "20 ms"]);
    expect(view.lanes.find((l) => l.model === "bert")!.backingUp).toBe(false);
  });

  it("draws at most MAX_LANES and counts the rest", () => {
    const many = Array.from({ length: MAX_LANES + 2 }, (_, i) => ({ model_id: `m${String(i).padStart(2, "0")}`, task: "embedding", in_use: 1, waiting: 1 }));
    const view = liveView(stats(many));
    expect([view.lanes.length, view.hidden, view.waiting]).toEqual([MAX_LANES, 2, MAX_LANES + 2]);
  });
});

/** A loaded model and a resting one, its traffic only what a test sends. */
function quiet(task = "embedding") {
  const models: Station[] = [
    { id: "on", task, loaded: true, weight: 1 },
    { id: "off", task, loaded: false, weight: 1 },
  ];
  const sim = new Simulation(models, () => 0.5);
  sim.traffic = false;
  return sim;
}

/** Its batch busy until a test frees it, so requests wait. */
const hold = (sim: Simulation) => (sim.stations[0].running = { items: [], start: 0, ms: Infinity });
const free = (sim: Simulation) => (sim.stations[0].running = null);

describe("Simulation", () => {
  it("puts a request in its model's line the moment it is sent, and runs it at once when the model is free", () => {
    const sim = quiet();
    const item = sim.send(0, true);
    expect(sim.stations[0].queue).toEqual([item]);
    sim.step(16);
    expect(sim.stations[0].running?.items).toEqual([item]);
    sim.advance(timing("embedding").base + timing("embedding").per + 100);
    expect([sim.stations[0].running, sim.stations[0].done]).toEqual([null, 1]);
  });

  it("takes those waiting together as one batch, one batch at a time, up to the largest", () => {
    const sim = quiet("text-generation");
    hold(sim);
    const items = Array.from({ length: 6 }, () => sim.send(0, true));
    sim.step(16);
    free(sim);
    sim.step(16);
    const { batch } = timing("text-generation");
    expect(sim.stations[0].running?.items).toEqual(items.slice(0, batch));
    expect(sim.stations[0].queue).toEqual(items.slice(batch));
  });

  it("puts an interactive request ahead of the background ones waiting", () => {
    const sim = quiet();
    hold(sim);
    const background = [sim.send(0, false), sim.send(0, false)];
    const urgent = sim.send(0, true);
    expect(sim.stations[0].queue).toEqual([urgent, ...background]);
  });

  it("loads a model the first request names, and unloads it once idle", () => {
    const sim = quiet();
    sim.send(1, true);
    sim.step(16);
    expect([sim.stations[1].loading != null, sim.stations[1].loaded]).toEqual([true, false]);
    sim.advance(timing("embedding").load + 100);
    expect(sim.stations[1].loaded).toBe(true);
    sim.advance(IDLE_MS + 3000);
    expect([sim.stations[1].loaded, sim.stations[1].done]).toEqual([false, 1]);
    // The one loaded from the start stays.
    expect(sim.stations[0].loaded).toBe(true);
  });

  it("shows its models as lanes: those waiting in order with their priority, backing up past one batch", () => {
    const sim = quiet();
    hold(sim);
    const { batch } = timing("embedding");
    const items = [sim.send(0, true), ...Array.from({ length: batch }, () => sim.send(0, false))];
    const view = sim.view();
    expect(view.lanes.map((l) => l.model)).toEqual(["on"]);
    expect(view.resting).toEqual(["off"]);
    expect(view.lanes[0].waiting).toEqual(items.map(({ id, high }) => ({ id, high })));
    expect([view.lanes[0].backingUp, view.waiting]).toEqual([true, batch + 1]);
  });

  it("runs the sample on its own, its models' lines filling and emptying", () => {
    const sim = new Simulation(SAMPLE);
    sim.advance(60_000);
    const done = sim.stations.reduce((n, st) => n + st.done, 0);
    expect(done).toBeGreaterThan(50);
    expect(sim.view().waiting).toBeLessThan(300);
  });

  it("now and then refuses a request as wrong, fails one, or turns one away from a model too far behind", () => {
    // A small seeded generator, so the run is the same each time.
    let seed = 7;
    const random = () => ((seed = (seed * 1103515245 + 12345) % 2 ** 31) / 2 ** 31);
    const sim = new Simulation(SAMPLE, random);
    const seen = new Map<string, number>();
    for (let t = 0; t < 180_000; t += 50) {
      sim.step(50);
      for (const e of sim.ended) seen.set(`${e.id}`, ["refused", "busy", "failed"].indexOf(e.end));
    }
    expect([...new Set(seen.values())].sort()).toEqual([0, 1, 2]);
    // None of them waits in a queue: they are shown where they stopped, then let go.
    const queued = new Set(sim.stations.flatMap((st) => st.queue.map((item) => item.id)));
    expect(sim.ended.every((e) => !queued.has(e.id) && e.born >= sim.now - 4000)).toBe(true);
  });

  it("sends from every source: apps and the playground now and then, tables and evals behind, workflows in bursts", () => {
    let seed = 11;
    const random = () => ((seed = (seed * 1103515245 + 12345) % 2 ** 31) / 2 ** 31);
    const sim = new Simulation(SAMPLE, random);
    const seen = new Set<string>();
    for (let t = 0; t < 120_000; t += 50) {
      sim.step(50);
      for (const e of sim.sent) seen.add(e.source);
    }
    expect([...seen].sort()).toEqual(["admin", "api", "data", "eval", "workflow"]);
  });
});

