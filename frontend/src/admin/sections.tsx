// What each /admin section draws, by its menu entry (shared/nav.ts): a menu
// entry without a view here, or a view the menu does not list, fails tsc.
// AdminApp draws the one the hash names, with what it loads for them all.
import type { ReactNode } from "react";
import { Card } from "@/components/ui/card";
import { parseHash } from "../shared/browser";
import { modelFromParams } from "../shared/model-browser/catalog";
import { ModelBrowserPanel } from "../shared/model-browser/ModelBrowser";
import type { Section } from "../shared/nav";
import type { AdminConfig, Stats } from "../shared/types";
import { ActivitySection } from "./activity/ActivitySection";
import { AuditSection } from "./audit/AuditSection";
import { AutoGluonSection } from "./autogluon/AutoGluonSection";
import { DataSection } from "./data/DataSection";
import { EnrichersSection } from "./enrichers/EnrichersSection";
import { EvalsSection } from "./evals/EvalsSection";
import { EvidenceSection } from "./evidence/EvidenceSection";
import { JobsSection } from "./jobs/JobsSection";
import { KeysSection } from "./keys/KeysSection";
import { PlaygroundSection } from "./playground/PlaygroundSection";
import type { CatalogInfo } from "./catalog";
import { OverviewSection } from "./overview/OverviewSection";
import { RemotesSection } from "./remotes/RemotesSection";
import { RunningSection } from "./RunningSection";
import { ServersSection } from "./servers/ServersSection";
import { SettingsSection } from "./SettingsSection";
import { WorkflowsSection } from "./workflows/WorkflowsSection";

/** What the dashboard loads for every section. */
export interface Dashboard {
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
  /** It fills the page's height to its foot: the playground's chat keeps its input at the bottom. */
  fill?: boolean;
}

/** The loaded models' keys, which change when one is loaded or unloaded. */
const loadedKeys = (stats: Stats | null) => (stats ? Object.keys(stats.cache.model_details).sort().join("|") : "");

export const SECTIONS: Record<Section, SectionView> = {
  overview: { windowed: true, render: (d) => <OverviewSection config={d.config} stats={d.stats} /> },
  models: {
    render: (d) => {
      // #/models?model=<id>&task=<task> opens on that model's details (a trained model's job links there).
      const open = modelFromParams(parseHash(location.hash).params);
      return (
        <Card className="h-[calc(100svh-8rem)] min-h-[520px] gap-0 overflow-hidden py-0">
          <ModelBrowserPanel key={open ? `${open.id}\n${open.task}` : ""} startOn={open} load={{ onChange: d.refresh }} refreshKey={loadedKeys(d.stats)} />
        </Card>
      );
    },
  },
  running: { windowed: true, render: (d) => <RunningSection stats={d.stats} catalog={d.catalog} refresh={d.refresh} /> },
  servers: { render: () => <ServersSection /> },
  playground: { fill: true, render: () => <PlaygroundSection /> },
  activity: { render: (d) => <ActivitySection monitoring={d.stats ? d.stats.monitoring.enabled : null} /> },
  audit: { render: () => <AuditSection /> },
  evals: { render: (d) => <EvalsSection catalog={d.catalog} /> },
  autogluon: { render: () => <AutoGluonSection /> },
  jobs: { render: () => <JobsSection /> },
  workflows: { render: () => <WorkflowsSection /> },
  data: { render: () => <DataSection /> },
  remotes: { render: () => <RemotesSection /> },
  enrichers: { render: () => <EnrichersSection /> },
  evidence: { render: () => <EvidenceSection /> },
  keys: { render: () => <KeysSection /> },
  settings: { render: (d) => <SettingsSection config={d.config} stats={d.stats} /> },
};
