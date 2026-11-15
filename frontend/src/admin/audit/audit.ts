// What the Audit log section (AuditSection.tsx) needs that is not drawing: the
// filters as GET /admin/audit takes them, and the words and colors of an
// entry's outcome and key.

import type { Status } from "../../shared/types";
import type { AuditEntry } from "../../shared/types";

export type AuditMethod = "" | "POST" | "PUT" | "PATCH" | "DELETE";
export type AuditOutcome = "" | "ok" | "refused" | "failed";
export type AuditActor = "" | AuditEntry["actor"];

/** The list's filters, each empty when not used. */
export interface AuditFilters {
  method: AuditMethod;
  outcome: AuditOutcome;
  actor: AuditActor;
  /** Part of the route or the path asked for */
  route: string;
}

export const NO_FILTERS: AuditFilters = { method: "", outcome: "", actor: "", route: "" };

/** Entries per page; "Older" asks for the next page. */
export const AUDIT_PAGE = 100;

/** The query of GET /admin/audit: the filters that are set, and the page (the previous page's ``next``). */
export function auditQuery(filters: AuditFilters, cursor?: string) {
  return {
    limit: AUDIT_PAGE,
    cursor,
    method: filters.method || undefined,
    outcome: filters.outcome || undefined,
    actor: (filters.actor || undefined) as Exclude<AuditActor, ""> | undefined,
    route: filters.route.trim() || undefined,
  };
}

/** An entry's status as an outcome: done (2xx, 3xx), refused (4xx: a bad request, a missing key) or failed (5xx). */
export function outcomeOf(status: number): Status {
  if (status >= 500) return { label: `Failed · ${status}`, color: "var(--critical)" };
  if (status >= 400) return { label: `Refused · ${status}`, color: "var(--warning)" };
  return { label: `Done · ${status}`, color: "var(--good)" };
}

const ACTORS: Record<string, string> = {
  admin: "Admin key",
  client: "Client key",
  none: "No key",
  wrong: "Wrong key",
  open: "No keys set",
};

/** Which key an entry's request carried, in words. */
export const actorLabel = (actor: string): string => ACTORS[actor] ?? actor;

export const ACTOR_OPTIONS = Object.entries(ACTORS).map(([value, label]) => ({ value, label }));
