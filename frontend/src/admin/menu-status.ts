// The figures behind the menu's live state (MenuStatus.tsx): kept apart from
// the view so they can be tested.
import type { Stats } from "../shared/types";

/** Requests in each bucket of the stats' timeline, failed ones included. */
export const requestsPerBucket = (stats: Pick<Stats, "timeline">): number[] =>
  stats.timeline.map((p) => p.ok + p.error + p.client_error);

/**
 * A sparkline of ``values`` in a ``width`` × ``height`` box: its line, and the area under it to fill.
 * The largest value touches the top, 0 the bottom. Null when there is nothing to draw (all 0, or one value).
 */
export function sparkline(values: number[], width: number, height: number): { line: string; area: string } | null {
  const max = Math.max(0, ...values);
  if (values.length < 2 || max === 0) return null;
  const pad = 1; // so the stroke is not cut at the edges
  const line = values
    .map((v, i) => {
      const x = (i / (values.length - 1)) * width;
      const y = height - pad - (v / max) * (height - 2 * pad);
      return `${i ? "L" : "M"}${x.toFixed(1)} ${y.toFixed(1)}`;
    })
    .join(" ");
  return { line, area: `${line} L${width} ${height} L0 ${height} Z` };
}
