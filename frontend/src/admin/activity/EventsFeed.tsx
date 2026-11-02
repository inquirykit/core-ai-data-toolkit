// The Activity & logs section's model events of the range, as a feed; each
// kind's label and colour (EVENT_STYLE) is also the chart's and the legend's.
import { StatusLabel } from "../../shared/components";
import { fmtMs, fmtTime, statusOf } from "../../shared/format";
import type { ModelEvent, Status } from "../../shared/types";

/**
 * Each kind of model event: its label and colour (status hues, so the chart and the feed agree). Still tuples,
 * read through statusOf, as every status table is; the activity chart (ActivityChart.tsx) reads them too.
 */
export const EVENT_STYLE: Record<string, Status> = {
  load: { label: "Loaded", color: "var(--good)" },
  load_failed: { label: "Load failed", color: "var(--critical)" },
  evict: { label: "Evicted", color: "var(--warning)" },
  unload: { label: "Unloaded", color: "var(--axis)" },
  limits: { label: "Limits changed", color: "var(--series-1)" },
};
const EVICT_REASON: Record<string, string> = { max_models: "MAX_MODELS reached", max_memory_gb: "over MAX_MEMORY_GB" };

export function EventsFeed({ events }: { events: ModelEvent[] }) {
  return (
    <ul className="grid text-sm">
      {events.length ? (
        events.map((e) => {
          const { label, color } = statusOf(EVENT_STYLE, e.kind);
          const detail = e.kind === "evict" && e.detail ? (EVICT_REASON[e.detail] ?? e.detail) : e.detail;
          return (
            // Rows are keyed by id, so only the new ones play the entrance.
            <li
              key={e.id}
              className="grid grid-cols-[auto_minmax(0,1fr)] items-center gap-x-3 gap-y-1 border-b py-2.5 duration-500 animate-in fade-in-0 slide-in-from-top-1 last:border-0 sm:grid-cols-[4.5rem_7rem_minmax(0,1fr)]"
            >
              <span className="text-xs text-muted-foreground tabular-nums" title={new Date(e.ts * 1000).toLocaleString()}>
                {fmtTime(e.ts)}
              </span>
              <StatusLabel status={{ label, color }} className="font-medium" />
              <span className="min-w-0 [overflow-wrap:anywhere] max-sm:col-span-2">
                <span className="font-mono text-[13px]">{e.model_key}</span>
                <span className="text-muted-foreground">
                  {(detail ? ` · ${detail}` : "") + (e.duration_ms != null ? ` · ${fmtMs(e.duration_ms)}` : "")}
                </span>
              </span>
            </li>
          );
        })
      ) : (
        <li className="py-10 text-center text-muted-foreground">No model events yet</li>
      )}
    </ul>
  );
}
