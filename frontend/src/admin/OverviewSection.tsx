// /admin#/overview: the warnings, the tiles, and requests and latency over
// the time window, as charts and as a table.
import { Info } from "lucide-react";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import type { AdminConfig, Stats } from "../shared/types";
import { ChartLegend, LatencyChart, RequestsChart, TimelineTable } from "./charts";
import { CollapsibleCard, GRID_2, OverviewSkeleton, Tiles, Warnings } from "./panels";

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
