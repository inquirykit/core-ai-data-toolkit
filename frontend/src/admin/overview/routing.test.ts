import { describe, expect, it } from "vitest";
import { SAMPLE } from "./queues";
import { badgeOf, ending, HUES, layoutRouting, NATURAL, sourceOf, SOURCES, SPREAD } from "./routing";

describe("layoutRouting", () => {
  const map = layoutRouting(SAMPLE, 1200);

  it("puts the router in the middle and takes each model's line from it to the side of its card facing it", () => {
    const { router } = map;
    expect(router.x + router.w / 2).toBeCloseTo(map.width / 2, 0);
    for (const s of map.stations) {
      expect(s.route.at(0)).toEqual({ x: map.width / 2, y: router.y + router.h / 2 });
      const near = s.card.x > map.width / 2 ? s.card.x : s.card.x + s.card.w;
      expect(s.route.at(s.route.length)).toEqual({ x: near, y: s.card.y + s.card.h / 2 });
    }
  });

  it("keeps the models in their order, on two sides about as tall", () => {
    expect(map.stations.map((s) => s.id)).toEqual(SAMPLE.map((m) => m.id));
    const right = map.stations.filter((s) => s.card.x > map.width / 2).length;
    expect(Math.abs(right - (map.stations.length - right))).toBeLessThanOrEqual(2);
  });

  it("stands a card's waiting requests in a row beside its pill, the next nearest, inside the card", () => {
    for (const s of map.stations) {
      const first = s.slotAt(0);
      const last = s.slotAt(s.slots - 1);
      const pill = s.pill.x + s.pill.w / 2;
      expect(Math.abs(first.x - pill)).toBeLessThan(Math.abs(last.x - pill));
      for (const p of [first, last]) expect(p.x > s.card.x && p.x < s.card.x + s.card.w && p.y > s.card.y && p.y < s.card.y + s.card.h).toBe(true);
      // Under the name row and its badge (6 to 19 px down), a gap between them.
      expect(s.pill.y - s.card.y).toBeGreaterThanOrEqual(25);
      expect(s.pill.y + s.pill.h).toBeLessThanOrEqual(s.card.y + s.card.h - 6);
    }
  });

  it("keeps its size in a wider card, in the middle, and is laid out no narrower than NATURAL", () => {
    expect(layoutRouting(SAMPLE, 400).width).toBe(NATURAL);
    const narrow = layoutRouting(SAMPLE, NATURAL);
    expect(map.stations.map((s) => Math.round(s.route.length))).toEqual(narrow.stations.map((s) => Math.round(s.route.length)));
    // A hop is short: from the router to any card of nine models.
    expect(Math.max(...layoutRouting(SAMPLE.slice(0, 9), 1200).stations.map((s) => s.route.length))).toBeLessThan(420);
  });

  it("spreads its cards and tasks apart toward a height, at most SPREAD times, never past it", () => {
    const nine = SAMPLE.slice(0, 9);
    const short = layoutRouting(nine, 1200);
    const tall = layoutRouting(nine, 1200, 700);
    expect(tall.height).toBeGreaterThan(short.height);
    expect(tall.height).toBeLessThanOrEqual(700);
    const gaps = (m: typeof map) => m.stations.slice(1).map((s, i) => s.card.y - m.stations[i].card.y).filter((d) => d > 0);
    expect(Math.min(...gaps(tall))).toBeGreaterThan(Math.min(...gaps(short)));
    for (const s of tall.stations) expect(s.card.y >= 0 && s.card.y + s.card.h <= tall.height).toBe(true);
    // Two models are spread at most SPREAD times, not across all of the room.
    const two = layoutRouting(SAMPLE.slice(0, 2), 1200, 900);
    const [a, b] = two.stations.map((s) => s.card.y);
    expect(b - a).toBeLessThanOrEqual(Math.round(52 * SPREAD));
    expect(two.height).toBeLessThan(400);
    expect(layoutRouting(SAMPLE, 1200, 0).height).toBe(map.height);
  });

  it("badges each card with where its model runs: outside on its provider or another toolkit, in its own process, or its device", () => {
    expect([
      badgeOf({ id: "a", task: "t", via: "ollama", device: "mps" }),
      badgeOf({ id: "p", task: "t", via: "gpu-box", peer: true }),
      badgeOf({ id: "b", task: "t", own: true, device: "mps" }),
      badgeOf({ id: "c", task: "t", device: "cuda:0" }),
      badgeOf({ id: "d", task: "t" }),
    ]).toEqual([
      { text: "↗ ollama", away: true }, { text: "⇄ gpu-box", away: true }, { text: "own process", away: false },
      { text: "cuda:0", away: false }, null,
    ]);
    expect(map.stations.filter((s) => s.badge?.away).map((s) => s.id)).toEqual(SAMPLE.filter((m) => m.via).map((m) => m.id));
  });

  it("writes each task's name just over its line where it runs level, in a column on each side, apart", () => {
    for (const line of map.lines) {
      const { x, y, room, align } = line.label;
      const right = x > map.width / 2;
      expect(align).toBe(right ? "left" : "right");
      // Level under the whole name: the line's way from the router is at one height there.
      const level = line.edge.pts.filter((p) => Math.abs(p.y - (y + 8)) < 0.5).map((p) => p.x);
      const [from, to] = right ? [x, x + Math.min(room, line.edge.pts[line.edge.pts.length - 1].x - x)] : [x - Math.min(room, x - line.edge.pts[line.edge.pts.length - 1].x), x];
      expect(Math.min(...level)).toBeLessThanOrEqual(from);
      expect(Math.max(...level)).toBeGreaterThanOrEqual(to);
      expect(room).toBeGreaterThan(100);
    }
    for (const side of [true, false]) {
      const ys = map.lines.filter((l) => l.label.x > map.width / 2 === side).map((l) => l.label.y).sort((a, b) => a - b);
      ys.slice(1).forEach((y, i) => expect(y - ys[i]).toBeGreaterThan(30));
      expect(new Set(map.lines.filter((l) => l.label.x > map.width / 2 === side).map((l) => l.label.x)).size).toBe(1);
    }
  });

  it("gives each task a hue by its place on its side, the left half way round, so a twin is far away", () => {
    for (const side of [0, 1]) {
      const lines = map.lines.filter((l) => (l.label.x > map.width / 2 ? 1 : 0) === side).sort((a, b) => a.label.y - b.label.y);
      expect(lines.map((l) => l.hue)).toEqual(lines.map((_, k) => (k + (side ? 0 : HUES / 2)) % HUES));
    }
    for (const s of map.stations) expect(s.hue).toBe(map.lines[s.line].hue);
  });
});

describe("ending", () => {
  it("tells a request answered, refused as wrong, turned away by a busy model, or failed", () => {
    const cases: [string, string | null][] = [
      ["ok", null], ["client_error", "ValueError"], ["error", "Overloaded"], ["error", "CapacityError"],
      ["error", "NoBackend"], ["error", "ReadTimeout"],
    ];
    expect(cases.map(([status, error]) => ending(status, error))).toEqual(["ok", "refused", "busy", "busy", "failed", "failed"]);
  });
});

describe("sources", () => {
  it("names where requests come from, and counts a row the log names none for as an app's", () => {
    expect(SOURCES.map((s) => s.key)).toEqual(["api", "admin", "workflow", "eval", "data"]);
    expect([sourceOf("eval"), sourceOf(null), sourceOf("weird")]).toEqual(["eval", "api", "api"]);
  });
});
