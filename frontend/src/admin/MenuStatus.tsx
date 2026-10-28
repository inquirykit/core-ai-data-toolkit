// What the menu (shared/Shell.tsx) shows live on /admin: the loaded models
// beside Running, the traffic beside Activity & logs, the active jobs beside
// Jobs, and the server's uptime and model memory at the bottom. From the
// stats AdminApp polls, and the jobs list the Jobs section shows.
import type { ReactNode } from "react";
import { LoaderCircle } from "lucide-react";
import { Progress } from "@/components/ui/progress";
import type { ApiClient } from "../shared/api";
import { StatusDot } from "../shared/components";
import { fmtDuration, fmtGB } from "../shared/format";
import type { MenuBadges } from "../shared/Shell";
import type { Stats } from "../shared/types";
import { isActive, JOBS_LIMIT, pollInterval } from "./jobs";
import { requestsPerBucket, sparkline } from "./menu-status";

const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;

/** A small count beside a menu item, in the item's hue. */
function Count({ n, title }: { n: number; title: string }) {
  return (
    <span title={title} className="nav-count grid h-[18px] min-w-5 place-items-center rounded-full px-1.5 text-[11px] font-semibold tabular-nums">
      {n}
    </span>
  );
}

/** The requests over the stats' window, as a line with a soft fill; nothing without traffic. */
function Spark({ stats }: { stats: Stats }) {
  const path = sparkline(requestsPerBucket(stats), 40, 14);
  if (!path) return null;
  const rate = stats.totals.requests_per_min;
  return (
    <span title={`${rate.toFixed(rate < 10 ? 2 : 0)} requests per minute`} className="max-md:hidden">
      <svg viewBox="0 0 40 14" className="nav-badge h-3.5 w-10" aria-hidden="true">
        <path d={path.area} fill="currentColor" fillOpacity={0.16} />
        <path d={path.line} fill="none" stroke="currentColor" strokeWidth={1.5} strokeLinejoin="round" strokeLinecap="round" />
      </svg>
    </span>
  );
}

/** At the bottom of the menu: is the server answering, for how long, and the model memory against its budget. */
function Health({ stats, error }: { stats: Stats; error: string }) {
  const cache = stats.cache;
  const share = cache.max_memory_gb ? cache.memory_used_gb / cache.max_memory_gb : 0;
  return (
    <a
      href="/admin#/running"
      title="Loaded models and memory limits"
      className="grid gap-2 rounded-lg border bg-background/60 p-2.5 text-xs text-muted-foreground outline-none transition-colors hover:bg-background/90 focus-visible:ring-[3px] focus-visible:ring-ring/50"
    >
      <span className="flex items-center gap-2">
        <StatusDot color={error ? "var(--critical)" : "var(--good)"} live={!error} />
        <span className="font-medium text-foreground">{error ? "Not answering" : "Up"}</span>
        {!error && <span className="ml-auto tabular-nums">{fmtDuration(stats.process.uptime_s)}</span>}
      </span>
      <span className="flex items-center gap-2">
        Model memory
        <span className="ml-auto tabular-nums">
          <span className="font-medium text-foreground">{fmtGB(cache.memory_used_gb)}</span>
          {cache.max_memory_gb > 0 && ` of ${fmtGB(cache.max_memory_gb)}`}
        </span>
      </span>
      {cache.max_memory_gb > 0 && (
        <Progress
          aria-hidden="true"
          value={Math.min(100, share * 100)}
          className="h-1.5 bg-primary/10"
          indicatorClassName={share >= 1 ? "bg-critical" : share >= 0.8 ? "bg-warning" : "bg-linear-90 from-brand-1 to-brand-2"}
        />
      )}
    </a>
  );
}

/**
 * The menu's live state for Shell: ``badges`` beside its items and the health card (``status``). Nothing
 * until the dashboard is ``live`` and the first stats arrive. The jobs list is the Jobs section's own query,
 * polled fast while a job is active, and not again once it fails (jobs are switched off).
 */
export function useMenuStatus(client: ApiClient, stats: Stats | null, statsError: string, live: boolean): { badges: MenuBadges; status: ReactNode } {
  const active = client.useQuery("get", "/jobs", { params: { query: { limit: JOBS_LIMIT } } }, {
    select: (d) => d.jobs.filter(isActive).length,
    refetchInterval: (query) => (query.state.error ? false : pollInterval(query.state.data?.jobs ?? null)),
    refetchOnWindowFocus: true,
    enabled: live,
  }).data;
  if (!live || !stats) return { badges: {}, status: null };
  const loaded = stats.cache.cache_size;
  return {
    badges: {
      running: loaded > 0 && <Count n={loaded} title={`${plural(loaded, "model", "models")} loaded`} />,
      activity: <Spark stats={stats} />,
      jobs: !!active && (
        <>
          <LoaderCircle className="nav-badge size-3 animate-spin" aria-hidden="true" />
          <Count n={active} title={`${plural(active, "job", "jobs")} queued or running`} />
        </>
      ),
    },
    status: <Health stats={stats} error={statsError} />,
  };
}
