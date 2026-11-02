// /admin#/overview: the warnings, the tiles, and requests and latency over
// the time window, as charts and as a table; and its shape, shimmering,
// until the first figures arrive.
import type { ReactNode } from "react";
import { CircleAlert, HardDrive, Info, Layers, Timer, Zap, type LucideIcon } from "lucide-react";
import { motion } from "motion/react";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Card, CardAction, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { Progress } from "@/components/ui/progress";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";
import { CollapsibleCard } from "../shared/components";
import { fmtCompact, fmtGB, fmtInt, fmtMs, fmtPct } from "../shared/format";
import type { AdminConfig, Stats } from "../shared/types";
import { ChartLegend, LatencyChart, RequestsChart, TimelineTable } from "./charts";
import { Warnings } from "./SettingsSection";

/** Two cards side by side on a wide screen: the charts, the activity. */
const GRID_2 = "grid gap-5 lg:grid-cols-2";

export function OverviewSection({ config, stats }: { config: AdminConfig; stats: Stats | null }) {
  return (
    <>
      <Warnings warnings={config.warnings} />
      {stats && !stats.monitoring.enabled && (
        <Alert role="note">
          <Info aria-hidden="true" />
          <AlertDescription>
            <p>
              Monitoring is off (<code>ARCADEDB_PATH</code> is empty or <code>MONITORING_RETENTION_DAYS</code> is 0), so only the live state is shown.
            </p>
          </AlertDescription>
        </Alert>
      )}
      {!stats && <OverviewSkeleton />}
      {stats && (
        <>
          <Tiles stats={stats} />
          <section className={GRID_2}>
            <Card className="gap-4">
              <CardHeader>
                <CardTitle>Requests</CardTitle>
                <CardDescription>per interval, OK and failed</CardDescription>
                <CardAction className="self-center">
                  <ChartLegend
                    items={[
                      ["OK", "var(--series-1)"],
                      ["Failed", "var(--critical)"],
                    ]}
                  />
                </CardAction>
              </CardHeader>
              <CardContent className="px-4">
                <RequestsChart stats={stats} />
              </CardContent>
            </Card>
            <Card className="gap-4">
              <CardHeader>
                <CardTitle>Latency p95</CardTitle>
                <CardDescription>per interval, including model loads</CardDescription>
              </CardHeader>
              <CardContent className="px-4">
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
const TILE_GRID = "stagger grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-5";
const MEMORY_SPAN = "sm:col-span-2 xl:col-span-1";

/** A figure that slides in afresh each time it changes. */
function TileValue({ value }: { value: string }) {
  return (
    <CardTitle className="text-2xl font-semibold tabular-nums @[200px]/card:text-3xl">
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

/** A summary card (shadcn "section card"): label and icon, the figure, a line under it. */
function Tile(props: { label: string; icon: LucideIcon; value: string; className?: string; children: ReactNode }) {
  return (
    <Card
      className={cn(
        "@container/card gap-3 bg-linear-to-t from-muted/40 to-card py-5 dark:from-primary/5 transition-[translate,box-shadow] duration-300 ease-out hover:-translate-y-0.5 hover:shadow-md",
        props.className,
      )}
    >
      <CardHeader className="px-5">
        <CardDescription>{props.label}</CardDescription>
        <TileValue value={props.value} />
        <CardAction>
          <span className="grid size-8 place-items-center rounded-md border bg-background/60 text-muted-foreground">
            <props.icon className="size-4" aria-hidden="true" />
          </span>
        </CardAction>
      </CardHeader>
      <CardFooter className="mt-auto flex-col items-stretch gap-2.5 px-5 text-xs text-muted-foreground">{props.children}</CardFooter>
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
      <Tile label="Model memory" icon={HardDrive} value={fmtGB(cache.memory_used_gb)} className={MEMORY_SPAN}>
        <div>
          {cache.max_memory_gb > 0 ? `of ${fmtGB(cache.max_memory_gb)} budget` : "no budget"}
          {cache.free_memory_gb != null ? ` · ${fmtGB(cache.free_memory_gb)} free` : ""}
          {share >= 1 ? " · over limit" : ""}
        </div>
        <Progress
          aria-hidden="true"
          value={Math.min(100, share * 100)}
          className="h-1.5 bg-primary/10"
          indicatorClassName={share >= 1 ? "bg-critical" : share >= 0.8 ? "bg-warning" : "bg-series-1"}
        />
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
          <Card className={cn("gap-3 py-5", i === 4 && MEMORY_SPAN)} key={i}>
            <CardHeader className="px-5">
              <Skeleton className="h-4 w-20" />
              <Skeleton className="mt-1 h-7 w-24" />
              <CardAction>
                <Skeleton className="size-8" />
              </CardAction>
            </CardHeader>
            <CardFooter className="px-5">
              <Skeleton className="h-3 w-28" />
            </CardFooter>
          </Card>
        ))}
      </section>
      <section className={GRID_2} aria-hidden="true">
        {[0, 1].map((i) => (
          <Card className="gap-4" key={i}>
            <CardHeader>
              <Skeleton className="h-4 w-24" />
              <Skeleton className="h-3.5 w-44" />
            </CardHeader>
            <CardContent className="px-4">
              <Skeleton className="h-[200px]" />
            </CardContent>
          </Card>
        ))}
      </section>
    </>
  );
}
