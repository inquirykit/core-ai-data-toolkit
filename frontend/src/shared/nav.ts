// The menu on the left of /admin (Shell.tsx draws it), in groups by what is
// done there. A section is picked by the URL hash (#/models), so it can be
// linked to and the back button works. Each section is one entry here, drawn
// with its view in admin/sections.tsx, which tsc holds to this list. What is
// set up once and seldom opened sits under Settings, as its tabs, so the
// menu keeps to the pages used day to day.
import {
  Activity,
  Boxes,
  CalendarClock,
  CloudUpload,
  Cpu,
  Database,
  FlaskConical,
  History,
  KeyRound,
  LayoutDashboard,
  MessagesSquare,
  ScanSearch,
  ServerCog,
  Settings2,
  ShieldCheck,
  TrendingUp,
  Workflow,
  type LucideIcon,
} from "lucide-react";
import { parseHash } from "./browser";

/** The menu's groups, in order, under their headings; each tints its items' icons (--nav-<key> in globals.css). */
export const NAV_GROUPS = [
  { key: "monitor", label: "Monitor" },
  { key: "models", label: "Models" },
  { key: "data", label: "Data" },
  { key: "automate", label: "Automate" },
] as const;
/** A menu group, or "system": Settings, which sits apart at the bottom of the menu. */
export type NavGroup = (typeof NAV_GROUPS)[number]["key"] | "system";

export interface NavItem<K extends string = string> {
  key: K;
  label: string;
  href: string;
  /** One line under the page title */
  blurb: string;
  icon: LucideIcon;
  group: NavGroup;
  /** The section it is a tab of, instead of an entry in the menu: shown with that one's other tabs. */
  under?: string;
  /** Its own tab's name, when sections sit under it ("General" for Settings) */
  tab?: string;
}

// In menu order within each group; a section's tabs follow it, in their order.
const ITEMS = [
  { key: "overview", label: "Overview", href: "/admin#/overview", blurb: "Requests, latency and memory at a glance", icon: LayoutDashboard, group: "monitor" },
  { key: "running", label: "Running", href: "/admin#/running", blurb: "Loaded models, their traffic, the memory limits, and the background work of every kind", icon: Cpu, group: "monitor" },
  { key: "activity", label: "Activity & logs", href: "/admin#/activity", blurb: "Traffic over time to zoom into, each request with its input and output, model events and the server log", icon: Activity, group: "monitor" },
  { key: "models", label: "Models", href: "/admin#/models", blurb: "Every model by task: load it, see what it needs, and try it", icon: Boxes, group: "models" },
  { key: "playground", label: "Playground", href: "/admin#/playground", blurb: "Try models by category: chat and compare, or run a task", icon: MessagesSquare, group: "models" },
  { key: "evals", label: "Evals", href: "/admin#/evals", blurb: "Datasets, runs of models over them, scores and side-by-side comparison", icon: FlaskConical, group: "models" },
  { key: "autogluon", label: "AutoGluon", href: "/admin#/autogluon", blurb: "Predict a column of a table or forecast a series: drop in a file, pick the column, train, and serve the predictor like any model", icon: TrendingUp, group: "models" },
  { key: "data", label: "Data", href: "/admin#/data", blurb: "Workspaces: their tables, graph and rows, queried in SQL, and views and dashboards over them", icon: Database, group: "data" },
  { key: "evidence", label: "Evidence", href: "/admin#/evidence", blurb: "Web pages captured by the browser extension: signed, chained records with their files, kept by workspace, and a check of its ledger", icon: ShieldCheck, group: "data" },
  { key: "workflows", label: "Workflows", href: "/admin#/workflows", blurb: "A table's rows through a graph of models, enrichers, conditions and writes, drawn on a canvas, run by hand or on a trigger", icon: Workflow, group: "automate" },
  { key: "jobs", label: "Jobs", href: "/admin#/jobs", blurb: "Background jobs, schedules that run them on a cron, and webhooks on completion", icon: CalendarClock, group: "automate" },
  { key: "enrichers", label: "Enrichers", href: "/admin#/enrichers", blurb: "Look values up in outside services: IPs, domains, hashes and more, one editable HTTP call each", icon: ScanSearch, group: "automate" },
  { key: "settings", label: "Settings", tab: "General", href: "/admin#/settings", blurb: "Configuration and the server process", icon: Settings2, group: "system" },
  { key: "servers", label: "Model servers", under: "settings", href: "/admin#/servers", blurb: "Models on OpenAI-compatible servers (chat, embeddings, rerank, speech): Ollama, LM Studio, a server started here, or in-process, tried in order", icon: ServerCog, group: "system" },
  { key: "remotes", label: "Remotes", under: "settings", href: "/admin#/remotes", blurb: "Places outside the service that buckets' files are copied to: S3, R2, B2, a folder...", icon: CloudUpload, group: "system" },
  { key: "keys", label: "API keys", under: "settings", href: "/admin#/keys", blurb: "Client keys, one per server or machine: make them, see when each was last used, and revoke them", icon: KeyRound, group: "system" },
  { key: "audit", label: "Audit log", under: "settings", href: "/admin#/audit", blurb: "Every change made through the admin's routes, and every delete: what, with which key, from where, and how it went", icon: History, group: "system" },
] as const satisfies readonly NavItem[];

export type NavKey = (typeof ITEMS)[number]["key"];
/** An /admin section: every entry of the menu. */
export type Section = NavKey;

/** Every section, in menu order. */
export const NAV: readonly NavItem<NavKey>[] = ITEMS;

const SECTIONS = new Set<string>(NAV.map((n) => n.key));
/** The /admin section a hash names ("#/models"), else the overview. */
export function sectionFromHash(hash: string): Section {
  const name = parseHash(hash).parts[0] ?? "";
  return (SECTIONS.has(name) ? name : "overview") as Section;
}

export const navItem = (key: NavKey): NavItem<NavKey> => NAV.find((n) => n.key === key)!;

/** The menu entry a section is shown under: its own, or the one it is a tab of. */
export const menuEntry = (key: NavKey): NavKey => (navItem(key).under ?? key) as NavKey;

/** The tabs of the section a menu entry opens, that one first; none when nothing sits under it. */
export function tabsOf(entry: NavKey): NavItem<NavKey>[] {
  const under = NAV.filter((n) => n.under === entry);
  return under.length ? [navItem(entry), ...under] : [];
}
