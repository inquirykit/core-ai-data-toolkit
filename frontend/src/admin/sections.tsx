// What each /admin section draws, by its menu entry (shared/nav.ts): a menu
// entry without a view here, or a view the menu does not list, fails tsc.
// AdminApp draws the one the hash names, with what it loads for them all.
import { lazy, Suspense, type ReactNode } from "react";
import { Card } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import type { ApiClient } from "../shared/api";
import { ModelBrowserPanel } from "../shared/model-browser/ModelBrowser";
import type { Section } from "../shared/nav";
import type { AdminConfig, Stats } from "../shared/types";
import { ActivitySection } from "./activity/ActivitySection";
import { AuditSection } from "./audit/AuditSection";
import { DataSection } from "./data/DataSection";
import { EnrichersSection } from "./enrichers/EnrichersSection";
import { EvalsSection } from "./evals/EvalsSection";
import { EvidenceSection } from "./evidence/EvidenceSection";
import { JobsSection } from "./jobs/JobsSection";
import type { CatalogInfo } from "./catalog";
import { OverviewSection } from "./OverviewSection";
import { RunningSection } from "./RunningSection";
import { ServersSection } from "./servers/ServersSection";
import { SettingsSection } from "./SettingsSection";

/** What the dashboard loads for every section. */
export interface Dashboard {
  client: ApiClient;
  config: AdminConfig;
  /** GET /admin/stats over the time window, polled; null until it first arrives */
  stats: Stats | null;
  catalog: CatalogInfo;
  /** Ask for the stats again now, after a change they show */
  refresh(): void;
}

export interface SectionView {
  render(d: Dashboard): ReactNode;
  /** It shows figures over the time window, so the header offers it (Activity & logs has a range of its own). */
  windowed?: boolean;
}

// Hub is switched off (shared/nav.ts): loaded only if it is ever shown, so it costs the page nothing meanwhile.
const HubSection = lazy(() => import("./hub/HubSection").then((m) => ({ default: m.HubSection })));

/** The loaded models' keys, which change when one is loaded or unloaded. */
const loadedKeys = (stats: Stats | null) => (stats ? Object.keys(stats.cache.model_details).sort().join("|") : "");

export const SECTIONS: Record<Section, SectionView> = {
  overview: { windowed: true, render: (d) => <OverviewSection config={d.config} stats={d.stats} /> },
  models: {
    render: (d) => (
      <Card className="h-[calc(100svh-8rem)] min-h-[520px] gap-0 overflow-hidden py-0">
        <ModelBrowserPanel client={d.client} load={{ onChange: d.refresh }} refreshKey={loadedKeys(d.stats)} />
      </Card>
    ),
  },
  hub: {
    render: (d) => (
      <Suspense fallback={<Skeleton className="h-64 rounded-xl" />}>
        <HubSection client={d.client} />
      </Suspense>
    ),
  },
  running: { windowed: true, render: (d) => <RunningSection stats={d.stats} client={d.client} catalog={d.catalog} refresh={d.refresh} /> },
  servers: { render: (d) => <ServersSection client={d.client} /> },
  activity: { render: (d) => <ActivitySection client={d.client} monitoring={d.stats ? d.stats.monitoring.enabled : null} /> },
  audit: { render: (d) => <AuditSection client={d.client} /> },
  evals: { render: (d) => <EvalsSection client={d.client} catalog={d.catalog} /> },
  jobs: { render: (d) => <JobsSection client={d.client} /> },
  data: { render: (d) => <DataSection client={d.client} /> },
  enrichers: { render: (d) => <EnrichersSection client={d.client} /> },
  evidence: { render: (d) => <EvidenceSection client={d.client} /> },
  settings: { render: (d) => <SettingsSection config={d.config} stats={d.stats} /> },
};
