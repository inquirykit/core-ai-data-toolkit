// The "Server log" tab of Activity & logs: the lines, following the newest
// unless the reader scrolls up, each level in its colour.
import { useEffect, useRef } from "react";
import { StatusDot } from "../shared/components";
import { fmtTime } from "../shared/format";
import type { LogLine } from "../shared/types";

/** Each level's colour: the dot of its lines and of the level filter's options. */
export const LEVEL_COLOR: Record<string, string> = {
  DEBUG: "var(--axis)",
  INFO: "var(--muted-foreground)",
  WARNING: "var(--warning)",
  ERROR: "var(--critical)",
  CRITICAL: "var(--critical)",
};

export function LogView(props: { lines: LogLine[]; follow: boolean; onScrollAway(): void }) {
  const ref = useRef<HTMLOListElement>(null);
  useEffect(() => {
    const el = ref.current;
    if (el && props.follow) el.scrollTop = el.scrollHeight;
  }, [props.lines, props.follow]);

  const onScroll = () => {
    const el = ref.current;
    if (!el || !props.follow) return;
    if (el.scrollHeight - el.scrollTop - el.clientHeight > 40) props.onScrollAway();
  };

  if (!props.lines.length) {
    return <p className="py-16 text-center text-sm text-muted-foreground">No log lines match</p>;
  }
  return (
    <ol
      ref={ref}
      onScroll={onScroll}
      className="max-h-[600px] overflow-y-auto bg-muted/20 px-4 py-2 font-mono text-[12.5px] leading-5"
      aria-label="Server log"
    >
      {props.lines.map((l) => (
        <li
          key={l.id}
          className="grid grid-cols-[4.75rem_minmax(0,1fr)] gap-x-3 border-b border-border/40 py-1 last:border-0 sm:grid-cols-[4.75rem_5.25rem_minmax(0,1fr)]"
        >
          <span className="text-muted-foreground tabular-nums" title={new Date(l.ts * 1000).toLocaleString()}>
            {fmtTime(l.ts)}
          </span>
          <span className="flex items-center gap-1.5 text-xs font-medium" style={{ color: l.levelno >= 30 ? LEVEL_COLOR[l.level] : undefined }}>
            <StatusDot color={LEVEL_COLOR[l.level] ?? "var(--axis)"} />
            {l.level}
          </span>
          <span className="min-w-0 whitespace-pre-wrap [overflow-wrap:anywhere] max-sm:col-span-2">
            <span className="text-muted-foreground">{l.logger} </span>
            {l.message}
          </span>
        </li>
      ))}
    </ol>
  );
}
