// What the Enrichers section (EnrichersSection.tsx) needs that is not
// drawing: where the hash points, what the editor starts from and sends (its
// form is made from the EnricherSpec schema), and the words and colors of
// statuses.

import { parseHash } from "../shared/browser";
import type { Status } from "../shared/types";
import { fmtAgo, fmtMs, statusOf } from "../shared/format";
import {
  type EnricherInfo,
  type EnricherSpec,
  type EnricherTerms,
  type EnrichResult,
  type EnrichStatus,
} from "../shared/types";

// ------------------------------------------------------------------ hash
export type EnricherLocation =
  | { view: "lookup" }
  /** The new-enricher form, prefilled from ``dup`` when given */
  | { view: "new"; dup?: string }
  | { view: "edit"; name: string };

/** What "#/enrichers", "#/enrichers/new?dup=x" and "#/enrichers/<name>" point at. */
export function locationFromHash(hash: string): EnricherLocation {
  const { parts, params } = parseHash(hash);
  const [section, name] = parts;
  if (section !== "enrichers" || !name) return { view: "lookup" };
  if (name === "new") {
    const dup = params.get("dup");
    return dup ? { view: "new", dup } : { view: "new" };
  }
  return { view: "edit", name };
}

export function enricherHash(loc: EnricherLocation): string {
  switch (loc.view) {
    case "lookup":
      return "#/enrichers";
    case "new":
      return loc.dup ? `#/enrichers/new?dup=${encodeURIComponent(loc.dup)}` : "#/enrichers/new";
    case "edit":
      return `#/enrichers/${encodeURIComponent(loc.name)}`;
  }
}

// ------------------------------------------------------------------ editor
/** The editor's value: the spec as its form (made from the EnricherSpec schema) holds it while it is written. */
export type EditedSpec = Partial<EnricherSpec>;

/** A new enricher before anything is written: the form fills in the schema's defaults. */
export const NEW_SPEC: EditedSpec = { name: "", inputs: [], request: { url: "" } as EnricherSpec["request"] };

/** The terms, in words, for the editor's choice of them. */
export const TERMS: [EnricherTerms, string][] = [
  ["open", "Free to use, no conditions"],
  ["attribution", "Free, with credit to the source"],
  ["non_commercial", "Free for non-commercial use only"],
  ["paid", "A paid plan or contract"],
];

/**
 * The spec as the editor sends it. A GET has no body (the server refuses one), so a body written before switching
 * to GET is left out; it stays in the form, should POST come back.
 */
export function sentSpec(spec: EnricherSpec): EnricherSpec {
  return spec.request.method === "POST" ? spec : { ...spec, request: { ...spec.request, body: null } };
}

// --------------------------------------------------------------- display
export const STATUS: Record<EnrichStatus, Status> = {
  ok: { label: "ok", color: "var(--good)" },
  not_found: { label: "not found", color: "var(--muted-foreground)" },
  error: { label: "error", color: "var(--critical)" },
  no_key: { label: "no key", color: "var(--warning)" },
  disabled: { label: "disabled", color: "var(--muted-foreground)" },
  not_allowed: { label: "not allowed", color: "var(--critical)" },
  rate_limited: { label: "rate limited", color: "var(--warning)" },
};

/** The status of a result in words, with the color of its dot. */
export function statusLabel(result: Pick<EnrichResult, "status">): Status {
  return statusOf(STATUS, result.status, "var(--muted-foreground)");
}

/** "cached · 3 min ago" or "fresh · 412 ms". */
export function fmtFreshness(result: Pick<EnrichResult, "cached" | "observed_at" | "ms">): string {
  if (result.cached) return `cached · ${fmtAgo(result.observed_at)}`;
  return result.ms == null ? "fresh" : `fresh · ${fmtMs(result.ms)}`;
}

/** An output's value for a table cell: primitives inline, null as "—", the rest as compact JSON. */
export function fmtOutput(value: unknown): string {
  if (value == null) return "—";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return JSON.stringify(value);
}

type Usability = "usable" | "needs key" | "disabled" | "not allowed by terms";
const USABILITY_COLOR: Record<Usability, string> = {
  usable: "var(--good)",
  "needs key": "var(--warning)",
  disabled: "var(--muted-foreground)",
  "not allowed by terms": "var(--critical)",
};

/** Whether a lookup can use the enricher, and why not, as a status with the color of its dot. */
export function usability(info: Pick<EnricherInfo, "usable" | "enabled" | "allowed" | "secret" | "secret_set" | "secret_optional">): Status {
  let label: Usability = "usable";
  if (!info.usable) {
    if (!info.allowed) label = "not allowed by terms";
    else if (!info.enabled) label = "disabled";
    else if (info.secret && !info.secret_set && !info.secret_optional) label = "needs key";
  }
  return { label, color: USABILITY_COLOR[label] };
}

export type Origin = "built-in" | "modified" | "custom";

/** Where the enricher comes from: shipped, shipped but edited, or the admin's own. */
export function origin(info: Pick<EnricherInfo, "builtin" | "overridden">): Origin {
  if (!info.builtin) return "custom";
  return info.overridden ? "modified" : "built-in";
}
