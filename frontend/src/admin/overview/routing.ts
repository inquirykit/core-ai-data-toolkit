// The routing picture's layout. Requests come to the router in the middle (where they come from is its tooltip);
// a line per task leaves it to one side or the other and branches to its models' cards. A task goes to the side with fewer models so far, so the
// two stand about as tall; its name is written along its line, where it runs level, so its cards stand close. Each card holds its model's queue: the
// batch it runs in a pill on the side the line comes in, the requests waiting in a row beside it, the next nearest.
// A card's badge says where its model runs: its device, a process of its own, or "↗" and the provider or server outside
// that answers it ("⇄" when that is another toolkit). Given a height, the cards and the tasks spread apart toward it. Pure: RoutingView.tsx draws it,
// zoomed out to fit its card.

export interface Pt {
  x: number;
  y: number;
}

/** A line as points close enough to look smooth, walked by distance from its start. */
export class Path {
  readonly cum: number[];
  readonly length: number;

  constructor(readonly pts: Pt[]) {
    this.cum = [0];
    for (let i = 1; i < pts.length; i++) this.cum.push(this.cum[i - 1] + Math.hypot(pts[i].x - pts[i - 1].x, pts[i].y - pts[i - 1].y));
    this.length = this.cum[this.cum.length - 1];
  }

  /** The point ``s`` along it, held at its ends. */
  at(s: number): Pt {
    if (s <= 0) return this.pts[0];
    if (s >= this.length) return this.pts[this.pts.length - 1];
    let lo = 0;
    let hi = this.cum.length - 1;
    while (hi - lo > 1) {
      const mid = (lo + hi) >> 1;
      if (this.cum[mid] <= s) lo = mid;
      else hi = mid;
    }
    const t = (s - this.cum[lo]) / (this.cum[hi] - this.cum[lo] || 1);
    const a = this.pts[lo];
    const b = this.pts[hi];
    return { x: a.x + (b.x - a.x) * t, y: a.y + (b.y - a.y) * t };
  }

  /** Through ``waypoints``: straight between two at one height, else an S-curve that leaves and arrives level. */
  static through(waypoints: Pt[]): Path {
    const pts: Pt[] = [waypoints[0]];
    for (let i = 1; i < waypoints.length; i++) {
      const a = waypoints[i - 1];
      const b = waypoints[i];
      if (a.y === b.y) {
        pts.push(b);
        continue;
      }
      const half = (b.x - a.x) / 2;
      const steps = Math.max(8, Math.ceil(Math.hypot(b.x - a.x, b.y - a.y) / 3));
      for (let k = 1; k <= steps; k++) {
        const t = k / steps;
        const u = 1 - t;
        // A cubic from a to b with its handles level: (a.x + half, a.y) and (b.x - half, b.y).
        pts.push({
          x: u * u * u * a.x + 3 * u * u * t * (a.x + half) + 3 * u * t * t * (b.x - half) + t * t * t * b.x,
          y: u * u * u * a.y + 3 * u * u * t * a.y + 3 * u * t * t * b.y + t * t * t * b.y,
        });
      }
    }
    return new Path(pts);
  }

  static join(...paths: Path[]): Path {
    return new Path(paths.flatMap((p, i) => (i ? p.pts.slice(1) : p.pts)));
  }
}

/** How a request the log has ended: answered; refused as wrong (its input, or the server outside that answers its
 * model, said no); turned away because its model was too busy (a 503, before it ran); or failed. */
export type Ending = "ok" | "refused" | "busy" | "failed";
/** The errors that turn a request away because its model is too busy (models/cache.py) */
const BUSY = new Set(["Overloaded", "CapacityError"]);
export const ending = (status: string, error: string | null): Ending =>
  status === "ok" ? "ok" : status === "client_error" ? "refused" : error != null && BUSY.has(error) ? "busy" : "failed";

/** Where the service's model requests come from, as the request log says (models/events.py, RequestContext.source),
 * in the router's tooltip in this order: apps calling the API with a client key (/inference, /v1); the admin's key (the
 * playground, the admin's pages); workflow runs, dry runs and invokes; eval runs; a /db table's vector field embedding
 * its rows or a search reranking its hits. */
export const SOURCES = [
  { key: "api", label: "Apps (API)" },
  { key: "admin", label: "Playground" },
  { key: "workflow", label: "Workflows" },
  { key: "eval", label: "Evals" },
  { key: "data", label: "Data" },
] as const;
export type Source = (typeof SOURCES)[number]["key"];
/** A request log row's source as a router row: one the log has none for (or an older row) is an app's */
export const sourceOf = (value: string | null | undefined): Source => SOURCES.find((s) => s.key === value)?.key ?? "api";

export interface Box {
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface RouteLine {
  task: string;
  /** Its hue, of HUES: by its place on its side, so a hue used twice is far from its twin */
  hue: number;
  /** From the router to where it branches to its models */
  edge: Path;
  /** Where its name is written: along its line, just over it, from ``x`` on (on the right) or up to ``x`` (on the
   * left), in ``room`` px: from just past the router's fan to where it branches, or to its card when it has one */
  label: { x: number; y: number; room: number; align: "left" | "right" };
}

export interface RouteStation {
  id: string;
  task: string;
  /** What its badge says: where it runs */
  badge: Badge | null;
  /** Its task's line */
  line: number;
  /** Its task's hue */
  hue: number;
  card: Box;
  /** A request's way, from the middle of the router to the card's side facing it */
  route: Path;
  /** The pill of the batch it runs, on that side */
  pill: Box;
  /** Where the k-th waiting request stands in the card, the next nearest the pill, and how many are drawn */
  slotAt: (k: number) => Pt;
  slots: number;
}

/** Where a model runs, as its card's badge says: on a device here, in a process of its own here, or outside, on the
 * provider, server or other toolkit named (``away``) */
export interface Badge {
  text: string;
  away: boolean;
}

/** A model to lay out: the server outside that answers it (``via``, another toolkit when ``peer``), or whether it runs
 * here in a process of its own, or on which device, as far as known */
export interface Placed {
  id: string;
  task: string;
  via?: string | null;
  peer?: boolean;
  own?: boolean;
  device?: string | null;
}

export const badgeOf = (m: Placed): Badge | null =>
  m.via ? { text: `${m.peer ? "⇄" : "↗"} ${m.via}`, away: true } : m.own ? { text: "own process", away: false } : m.device ? { text: m.device, away: false } : null;

export interface RoutingLayout {
  width: number;
  height: number;
  router: Box;
  lines: RouteLine[];
  stations: RouteStation[];
}

const PAD = 16;
const ROW = 52;
const GROUP_GAP = 12;
/** The router: its name, and how many came in the last minute */
const ROUTER = { w: 136, h: 48 };
/** A line's way from the router: across to its height, level (where its name is written), then across a branch to its
 * card */
const FAN = 70;
const LEVEL = 120;
const BRANCH = 44;
export const CARD = { w: 224, h: 46 };
const PILL = { w: 40, h: 15 };
const SLOT_GAP = 11;
/** Its size: the router, and on each side the lines and the cards */
export const NATURAL = PAD * 2 + ROUTER.w + 2 * (FAN + LEVEL + BRANCH + CARD.w);
/** The lines' hues, a task each; with more tasks, each side starts them over, the left half way round */
export const HUES = 6;
/** The most the cards and the tasks are spread apart, to fill a taller picture */
export const SPREAD = 1.4;

const span = (dy: number, least: number, most: number) => Math.round(Math.max(least, Math.min(most, dy * 0.9)));

/** ``models`` (by task, in the order given) laid out ``width`` wide, or NATURAL if that is wider; spread apart toward
 * ``height``, at most SPREAD times, so a few models are not stretched across empty room. */
export function layoutRouting(models: Placed[], width: number, height = 0): RoutingLayout {
  const w = Math.max(width, NATURAL);
  const tasks = [...new Set(models.map((m) => m.task))];
  const byTask = tasks.map((task) => models.filter((m) => m.task === task));
  // Each task on the side with fewer models so far (the right first), the tasks with the most models first, so the
  // two stand about as tall; on each side, in the order given.
  const sides: number[][] = [[], []];
  const count = [0, 0];
  [...byTask.keys()].sort((a, b) => byTask[b].length - byTask[a].length).forEach((li) => {
    const side = count[1] <= count[0] ? 1 : 0;
    sides[side].push(li);
    count[side] += byTask[li].length;
  });
  sides.forEach((ids) => ids.sort((a, b) => a - b));
  // A side's height: its cards and the gaps between its tasks, which spread.
  const loose = (ids: number[]) => ids.reduce((h, li, k) => h + byTask[li].length * ROW + (k ? GROUP_GAP : 0), 0);
  const room = height - PAD * 2;
  const spread = Math.max(1, Math.min(SPREAD, ...sides.filter((ids) => ids.length).map((ids) => room / loose(ids))));
  const row = Math.round(ROW * spread);
  const gap = Math.round(GROUP_GAP * spread);
  const tall = (ids: number[]) => ids.reduce((h, li, k) => h + byTask[li].length * row + (k ? gap : 0), 0);
  const inner = Math.max(tall(sides[0]), tall(sides[1]), ROUTER.h + 28);
  const cy = Math.round(PAD + inner / 2);
  const router: Box = { x: Math.round(w / 2 - ROUTER.w / 2), y: cy - ROUTER.h / 2, w: ROUTER.w, h: ROUTER.h };
  const middle: Pt = { x: Math.round(w / 2), y: cy };
  const lines: RouteLine[] = new Array(tasks.length);
  const placed: RouteStation[][] = new Array(tasks.length);
  sides.forEach((ids, side) => {
    const dir = side ? 1 : -1;
    const exit: Pt = { x: side ? router.x + router.w : router.x, y: cy };
    const near = exit.x + dir * (FAN + LEVEL + BRANCH);
    const cardX = side ? near : near - CARD.w;
    let y = cy - tall(ids) / 2;
    const rows = ids.map((li, k) => {
      y += k ? gap : 0;
      return byTask[li].map(() => Math.round((y += row) - row / 2));
    });
    const lineY = rows.map((ys) => Math.round((ys[0] + ys[ys.length - 1]) / 2));
    const fan = span(Math.max(0, ...lineY.map((ty) => Math.abs(ty - cy))), 30, FAN);
    const jx = exit.x + dir * (FAN + LEVEL);
    // The names start in a column just past the widest fan.
    const column = exit.x + dir * (FAN + 6);
    ids.forEach((li, k) => {
      const ys = rows[k];
      const ty = lineY[k];
      const junction: Pt = { x: jx, y: ty };
      const edge = Path.through([middle, exit, { x: exit.x + dir * fan, y: ty }, junction]);
      const hue = (k + (side ? 0 : HUES / 2)) % HUES;
      const until = (ys.length > 1 ? jx : near) - dir * 8;
      const label = { x: column, y: ty - 8, room: Math.abs(until - column), align: side ? ("left" as const) : ("right" as const) };
      lines[li] = { task: tasks[li], hue, edge, label };
      const across = Math.min(span(Math.max(...ys.map((yy) => Math.abs(yy - ty))), 0, BRANCH), BRANCH - 12);
      placed[li] = byTask[li].map((m, si) => {
        const at: Pt = { x: near, y: ys[si] };
        const card: Box = { x: cardX, y: at.y - CARD.h / 2, w: CARD.w, h: CARD.h };
        // The pill on the side the line comes in, the waiting row beside it, its next nearest.
        const pill: Box = { x: side ? card.x + 8 : card.x + card.w - 8 - PILL.w, y: card.y + card.h - 6 - PILL.h, w: PILL.w, h: PILL.h };
        const first = side ? pill.x + pill.w + 10 : pill.x - 10;
        const slots = Math.floor((card.w - PILL.w - 40) / SLOT_GAP);
        return {
          id: m.id, task: m.task, badge: badgeOf(m), line: li, hue, card, pill, slots,
          route: Path.join(edge, Path.through([junction, { x: jx + dir * across, y: at.y }, at])),
          slotAt: (q: number) => ({ x: first + dir * q * SLOT_GAP, y: pill.y + pill.h / 2 }),
        };
      });
    });
  });
  return { width: w, height: PAD * 2 + inner, router, lines, stations: placed.flat() };
}
