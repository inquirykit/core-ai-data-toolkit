// /admin#/settings: the warnings about exposing the service (on the Overview
// too), and the configuration and the process, folded on the Overview.
import { TriangleAlert } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { CollapsibleCard, Facts } from "../shared/components";
import { fmtDuration, fmtGB } from "../shared/format";
import type { AdminConfig, Stats } from "../shared/types";

export function SettingsSection({ config, stats }: { config: AdminConfig; stats: Stats | null }) {
  return (
    <>
      <Warnings warnings={config.warnings} />
      <ConfigAndProcess config={config} stats={stats} open />
    </>
  );
}

/** Settings and figures as the rows of a boxed Facts; a list is joined. */
function KeyValues({ entries }: { entries: [string, unknown][] }) {
  return <Facts boxed mono rows={entries.map(([k, v]) => [k, Array.isArray(v) ? (v.length ? v.join(", ") : "(empty)") : String(v)])} />;
}

function processEntries(stats: Stats): [string, unknown][] {
  const p = stats.process;
  const entries: [string, unknown][] = [
    ["device", p.device],
    ["pid", p.pid],
    ["python", p.python],
    ["uptime", fmtDuration(p.uptime_s)],
  ];
  if (p.cpu_percent != null) entries.push(["cpu", `${p.cpu_percent.toFixed(0)}% of 1 core · ${p.cpu_count} cores`]);
  if (p.rss_gb != null) entries.push(["process memory", fmtGB(p.rss_gb)]);
  if (p.host_memory) {
    entries.push(["host memory", `${fmtGB(p.host_memory.available_gb)} free of ${fmtGB(p.host_memory.total_gb)}`]);
  }
  for (const g of p.gpus) entries.push([`gpu ${g.index}`, `${g.name} · ${fmtGB(g.allocated_gb)} used of ${fmtGB(g.total_gb)}`]);
  const m = stats.monitoring;
  entries.push([
    "monitoring",
    m.enabled ? `on · kept ${m.retention_days} days${m.dropped ? ` · ${m.dropped} rows dropped` : ""}` : "off",
  ]);
  return entries;
}

function ConfigAndProcess({ config, stats, open }: { config: AdminConfig; stats: Stats | null; open?: boolean }) {
  return (
    <CollapsibleCard title="Configuration and process" defaultOpen={open}>
      <div className="grid items-start gap-5 lg:grid-cols-2">
        <KeyValues entries={Object.entries(config.settings)} />
        <div>{stats && <KeyValues entries={processEntries(stats)} />}</div>
      </div>
    </CollapsibleCard>
  );
}

export function Warnings({ warnings }: { warnings: string[] }) {
  if (!warnings.length) return null;
  return (
    <Alert variant="warning" role="note">
      <TriangleAlert aria-hidden="true" />
      <AlertTitle>Check before exposing this service</AlertTitle>
      <AlertDescription>
        <ul className="grid list-disc gap-0.5 pl-4">
          {warnings.map((w) => (
            <li key={w}>{w}</li>
          ))}
        </ul>
      </AlertDescription>
    </Alert>
  );
}
