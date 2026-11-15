// /admin#/overview: the warnings, the tiles, the models' queues, and requests
// and latency over the time window, as charts and as a table; and its shape,
// shimmering, until the first figures arrive.
import type { ReactNode } from "react";
import { CircleAlert, HardDrive, Info, Layers, Timer, Zap, type LucideIcon } from "lucide-react";
import { motion } from "motion/react";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Card, CardAction, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { Progress } from "@/components/ui/progress";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";
import { CollapsibleCard } from "../../shared/components";
import { fmtCompact, fmtGB, fmtInt, fmtMs, fmtPct } from "../../shared/format";
import type { AdminConfig, Stats } from "../../shared/types";
import { CHART_H, ChartLegend, LatencyChart, RequestsChart, TimelineTable } from "../charts";
import { Warnings } from "../SettingsSection";
import { QueuesCard } from "./QueuesCard";

/** Two cards side by side on a wide screen: the charts, the activity. */
const GRID_2 = "grid gap-5 lg:grid-cols-2";
/** A chart's card, kept low so the queues have the room */
const CHART_CARD = "gap-1 py-3";
/** A chart card's header: its title and what it shows on one line, the legend at the end */
const CHART_HEADER = "flex flex-wrap items-baseline gap-x-2 gap-y-0.5 px-4";

export function OverviewSection({ config, stats }: { config: AdminConfig; stats: Stats | null }) {
  return (
    <>
      <Warnings warnings={config.warnings} />
      {stats && !stats.monitoring.enabled && (
        <Alert role="note">
          <Info aria-hidden="true" />
          <AlertDescription>
            <p>
              Monitoring is off (<code>MONITORING_RETENTION_DAYS</code> is 0), so only the live state is shown.
            </p>
          </AlertDescription>
        </Alert>
      )}
      {!stats && <OverviewSkeleton />}
      {stats && (
        <>
          <Tiles stats={stats} />
          <QueuesCard stats={stats} />
          <section className={GRID_2}>
            <Card className={CHART_CARD}>
              <CardHeader className={CHART_HEADER}>
                <CardTitle className="text-sm">Requests</CardTitle>
                <CardDescription className="text-xs">per interval, OK and failed</CardDescription>
                <CardAction className="ml-auto self-center">
                  <ChartLegend
                    items={[
                      ["OK", "var(--series-1)"],
                      ["Failed", "var(--critical)"],
                    ]}
                  />
                </CardAction>
              </CardHeader>
              <CardContent className="px-3">
                <RequestsChart stats={stats} />
              </CardContent>
            </Card>
            <Card className={CHART_CARD}>
              <CardHeader className={CHART_HEADER}>
                <CardTitle className="text-sm">Latency p95</CardTitle>
                <CardDescription className="text-xs">per interval, including model loads</CardDescription>
              </CardHeader>
              <CardContent className="px-3">
                <LatencyChart stats={stats} />
              </CardContent>
            </Card>
          </section>
          <CollapsibleCard title="Requests over time as a table">
            <div className="overflow-hidden rounded-lg border">
              <TimelineTable stats={stats} />
            </div>
          </CollapsibleCard>
        </>
      )}
    </>
  );
}

// ------------------------------------------------------------------ tiles
// One column, then two (the memory card across both), then all five in a row.
const TILE_GRID = "stagger grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-5";
const MEMORY_SPAN = "sm:col-span-2 xl:col-span-1";

/** A figure that slides in afresh each time it changes. */
function TileValue({ value }: { value: string }) {
  return (
    <CardTitle className="text-lg leading-tight font-semibold tabular-nums">
      <motion.span
        key={value}
        className="inline-block"
        initial={{ opacity: 0, y: 8, filter: "blur(4px)" }}
        animate={{ opacity: 1, y: 0, filter: "blur(0px)" }}
        transition={{ duration: 0.35, ease: [0.22, 1, 0.36, 1] }}
      >
        {value}
      </motion.span>
    </CardTitle>
  );
}

/** A summary card (shadcn "section card"): label and icon, the figure (a meter beside it, if any), a line under it. */
function Tile(props: { label: string; icon: LucideIcon; value: string; meter?: ReactNode; className?: string; children: ReactNode }) {
  return (
    <Card
      className={cn(
        "@container/card gap-0.5 bg-linear-to-t from-muted/40 to-card py-2 dark:from-primary/5 transition-[translate,box-shadow] duration-300 ease-out hover:-translate-y-0.5 hover:shadow-md",
        props.className,
      )}
    >
      <CardHeader className="gap-0.5 px-3">
        <CardDescription className="text-xs">{props.label}</CardDescription>
        <div className="flex items-center gap-2">
          <TileValue value={props.value} />
          {props.meter}
        </div>
        <CardAction>
          <span className="grid size-6 place-items-center rounded-md border bg-background/60 text-muted-foreground">
            <props.icon className="size-3" aria-hidden="true" />
          </span>
        </CardAction>
      </CardHeader>
      <CardFooter className="mt-auto px-3 text-xs text-muted-foreground">{props.children}</CardFooter>
    </Card>
  );
}

function Tiles({ stats }: { stats: Stats }) {
  const t = stats.totals;
  const cache = stats.cache;
  const share = cache.max_memory_gb ? cache.memory_used_gb / cache.max_memory_gb : 0;
  const tiles = [
    {
      label: "Requests",
      icon: Zap,
      value: fmtCompact(t.requests),
      sub: `${t.requests_per_min.toFixed(t.requests_per_min < 10 ? 2 : 0)} per minute`,
    },
    {
      label: "Failed",
      icon: CircleAlert,
      value: fmtCompact(t.client_errors + t.errors),
      sub: t.requests ? `${fmtInt(t.errors)} server (${fmtPct(t.error_rate)}) · ${fmtInt(t.client_errors)} client` : "no requests",
    },
    { label: "Latency p95", icon: Timer, value: fmtMs(t.p95_ms), sub: `p50 ${fmtMs(t.p50_ms)}` },
    { label: "Loaded models", icon: Layers, value: `${cache.cache_size} / ${cache.max_models}`, sub: `on ${stats.process.device}` },
  ] satisfies { label: string; icon: LucideIcon; value: string; sub: string }[];
  return (
    <section className={TILE_GRID} aria-label="Summary">
      {tiles.map((tile) => (
        <Tile key={tile.label} label={tile.label} icon={tile.icon} value={tile.value}>
          {tile.sub}
        </Tile>
      ))}
      <Tile
        label="Model memory"
        icon={HardDrive}
        value={fmtGB(cache.memory_used_gb)}
        className={MEMORY_SPAN}
        meter={
          <Progress
            aria-hidden="true"
            value={Math.min(100, share * 100)}
            className="h-1 flex-1 bg-primary/10"
            indicatorClassName={share >= 1 ? "bg-critical" : share >= 0.8 ? "bg-warning" : "bg-series-1"}
          />
        }
      >
        {cache.max_memory_gb > 0 ? `of ${fmtGB(cache.max_memory_gb)}` : "no budget"}
        {cache.free_memory_gb != null ? ` · ${fmtGB(cache.free_memory_gb)} free` : ""}
        {share >= 1 ? " · over limit" : ""}
      </Tile>
    </section>
  );
}

/** The overview's shape, shimmering, until the first figures arrive. */
function OverviewSkeleton() {
  return (
    <>
      <section className={TILE_GRID} aria-hidden="true">
        {[0, 1, 2, 3, 4].map((i) => (
          <Card className={cn("gap-0.5 py-2", i === 4 && MEMORY_SPAN)} key={i}>
            <CardHeader className="gap-0.5 px-3">
              <Skeleton className="h-3.5 w-16" />
              <Skeleton className="mt-0.5 h-5 w-20" />
              <CardAction>
                <Skeleton className="size-6" />
              </CardAction>
            </CardHeader>
            <CardFooter className="px-3">
              <Skeleton className="h-3 w-28" />
            </CardFooter>
          </Card>
        ))}
      </section>
      <section className={GRID_2} aria-hidden="true">
        {[0, 1].map((i) => (
          <Card className={CHART_CARD} key={i}>
            <CardHeader className={CHART_HEADER}>
              <Skeleton className="h-3.5 w-20" />
              <Skeleton className="h-3 w-40" />
            </CardHeader>
            <CardContent className="px-3">
              <Skeleton style={{ height: CHART_H }} />
            </CardContent>
          </Card>
        ))}
      </section>
    </>
  );
}
