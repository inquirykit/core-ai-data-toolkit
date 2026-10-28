// The menu on the left of /admin and /playground (Shell.tsx draws it), in
// groups. /admin's sections are picked by the URL hash (#/models), so they
// can be linked to and the back button works; the playground is its own page.
// Each page is one entry here; /admin draws a section with its view in
// admin/sections.tsx, which tsc holds to this list.
import {
  Activity,
  Boxes,
  CalendarClock,
  Cpu,
  Database,
  FlaskConical,
  HardDriveDownload,
  LayoutDashboard,
  MessagesSquare,
  ScanSearch,
  ServerCog,
  Settings2,
  ShieldCheck,
  type LucideIcon,
} from "lucide-react";
import { parseHash } from "./browser";

/** The menu's groups, in order, under their headings; each tints its items' icons (--nav-<key> in globals.css). */
export const NAV_GROUPS = [
  { key: "monitor", label: "Monitor" },
  { key: "models", label: "Models" },
  { key: "data", label: "Data" },
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
  /** Switched off: not in the menu, and its hash leads to the overview */
  off?: boolean;
}

// In menu order within each group.
const ITEMS = [
  { key: "overview", label: "Overview", href: "/admin#/overview", blurb: "Requests, latency and memory at a glance", icon: LayoutDashboard, group: "monitor" },
  { key: "running", label: "Running", href: "/admin#/running", blurb: "Loaded models, their traffic, and the memory limits", icon: Cpu, group: "monitor" },
  { key: "activity", label: "Activity & logs", href: "/admin#/activity", blurb: "Traffic over time to zoom into, each request with its input and output, model events and the server log", icon: Activity, group: "monitor" },
  { key: "models", label: "Models", href: "/admin#/models", blurb: "Every model by task: load it, see what it needs, and try it", icon: Boxes, group: "models" },
  // Hub is switched off; drop `off` to bring it back.
  { key: "hub", label: "Hub", href: "/admin#/hub", blurb: "Search Hugging Face, download weights ahead of time, and see what is on disk", icon: HardDriveDownload, group: "models", off: true },
  { key: "servers", label: "Model servers", href: "/admin#/servers", blurb: "Where LLMs run: Ollama, LM Studio, a server started here, or in-process, tried in order", icon: ServerCog, group: "models" },
  { key: "playground", label: "Playground", href: "/playground", blurb: "Try models by category: chat and compare, or run a task", icon: MessagesSquare, group: "models" },
  { key: "evals", label: "Evals", href: "/admin#/evals", blurb: "Datasets, runs of models over them, scores and side-by-side comparison", icon: FlaskConical, group: "models" },
  { key: "data", label: "Data", href: "/admin#/data", blurb: "Namespaces, their tables and rows, in ArcadeDB", icon: Database, group: "data" },
  { key: "jobs", label: "Jobs", href: "/admin#/jobs", blurb: "Background jobs, schedules that run them on a cron, and webhooks on completion", icon: CalendarClock, group: "data" },
  { key: "enrichers", label: "Enrichers", href: "/admin#/enrichers", blurb: "Look values up in outside services: IPs, domains, hashes and more, one editable HTTP call each", icon: ScanSearch, group: "data" },
  { key: "evidence", label: "Evidence", href: "/admin#/evidence", blurb: "Web pages captured by the browser extension: signed, chained records with their files, kept by workspace, and a check of its ledger", icon: ShieldCheck, group: "data" },
  { key: "settings", label: "Settings", href: "/admin#/settings", blurb: "Configuration and the server process", icon: Settings2, group: "system" },
] as const satisfies readonly NavItem[];

export type NavKey = (typeof ITEMS)[number]["key"];
/** An /admin section: every page but the playground, switched off or not. */
export type Section = Exclude<NavKey, "playground">;

/** Every page, in menu order, switched off ones included. */
export const NAV: readonly NavItem<NavKey>[] = ITEMS;

const SECTIONS = new Set<string>(NAV.filter((n) => n.key !== "playground" && !n.off).map((n) => n.key));
/** Old section names that still work as links: Traces became the requests of Activity & logs. */
const RENAMED: Record<string, Section> = { traces: "activity" };

/** The /admin section a hash names ("#/models"), else the overview. */
export function sectionFromHash(hash: string): Section {
  const name = parseHash(hash).parts[0] ?? "";
  if (name in RENAMED) return RENAMED[name];
  return (SECTIONS.has(name) ? name : "overview") as Section;
}

export const navItem = (key: NavKey): NavItem<NavKey> => NAV.find((n) => n.key === key)!;
