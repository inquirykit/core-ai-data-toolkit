// What the API keys section (KeysSection.tsx) needs that is not drawing: the
// expiry choices and the body a new key is made with, the list's query, a
// key's state and expiry in words, and its grants as the editor
// (GrantsEditor.tsx) holds them.

import type { Schemas } from "../../shared/api";
import { fmtAgo } from "../../shared/format";
import type { Status } from "../../shared/types";

/** A client key as GET /admin/keys lists it: never its token. */
export type ClientKey = Schemas["ClientKeyInfo"];
/** The reply that makes one: the key with its token, the only time it is shown. */
export type ClientKeyMade = Schemas["ClientKeyMade"];
export type KeyState = Schemas["ClientKeyInfo"]["state"];
export type KeyFilter = "" | KeyState;
/** What a key may reach (access/grants.py): a field left null leaves that alone. */
export type KeyGrants = Schemas["Grants"];
export type GrantAction = NonNullable<KeyGrants["tables"]>[string][number];

/** Keys per page. */
export const KEYS_PAGE = 50;

/** How long a new key lasts, in days; "" for never. */
export const EXPIRY_OPTIONS = [
  { value: "7", label: "7 days" },
  { value: "30", label: "30 days" },
  { value: "90", label: "90 days" },
  { value: "365", label: "A year" },
  { value: "", label: "Never" },
];
export const DEFAULT_EXPIRY = "90";

/** What POST /admin/keys takes, or what is wrong with the form. */
export function newKeyBody(
  name: string,
  expiry: string,
  grants: KeyGrants = {},
): { name: string; expires_in_days?: number; grants: KeyGrants } | string {
  const trimmed = name.trim();
  if (!trimmed) return "Name the key: the server or machine that will hold it.";
  if (trimmed.length > 100) return "A name has at most 100 characters.";
  return { name: trimmed, ...(expiry ? { expires_in_days: Number(expiry) } : {}), grants };
}

const ACTION_LABELS: Record<GrantAction, string> = { read: "Read", write: "Write", delete: "Delete" };
/** The actions, in the order the editor shows them, with their labels. */
export const ACTIONS = (Object.keys(ACTION_LABELS) as GrantAction[]).map((value) => ({ value, label: ACTION_LABELS[value] }));

/** One line of the editor: an entry (docs.notes, docs.*, *.*) and its actions. */
export type GrantRow = { entry: string; actions: GrantAction[] };
/** A key's grants as the editor holds them: the models as typed, comma-separated. */
export type GrantsForm = { limitTables: boolean; rows: GrantRow[]; limitModels: boolean; models: string; manageModels: boolean };

const NEW_ROW: GrantRow = { entry: "", actions: ["read"] };
export const NO_GRANTS: GrantsForm = { limitTables: false, rows: [NEW_ROW], limitModels: false, models: "", manageModels: false };

/** The editor's form for a key's grants. */
export function grantsForm(grants: KeyGrants): GrantsForm {
  const rows = Object.entries(grants.tables ?? {}).map(([entry, actions]) => ({ entry, actions }));
  return {
    limitTables: grants.tables != null,
    rows: rows.length ? rows : [NEW_ROW],
    limitModels: grants.models != null,
    models: (grants.models ?? []).join(", "),
    manageModels: Boolean(grants.manage_models),
  };
}

/** The grants the form says, as the API takes them. Entries add up, so a table named on two lines gets both lines'
 * actions. */
export function grantsBody(form: GrantsForm): KeyGrants {
  const tables: Record<string, GrantAction[]> = {};
  for (const row of form.rows) {
    const entry = row.entry.trim();
    const before = tables[entry] ?? [];
    if (entry) tables[entry] = ACTIONS.map((a) => a.value).filter((a) => before.includes(a) || row.actions.includes(a));
  }
  return {
    tables: form.limitTables ? tables : null,
    models: form.limitModels ? form.models.split(",").map((m) => m.trim()).filter(Boolean) : null,
    manage_models: form.manageModels,
  };
}

/** A line's actions with ``action`` turned on or off, in the editor's order. */
export function toggled(actions: GrantAction[], action: GrantAction, on: boolean): GrantAction[] {
  return ACTIONS.map((a) => a.value).filter((a) => (a === action ? on : actions.includes(a)));
}

/** A key's grants in a few words, for the list: "Every table and model", "2 table entries · 1 model". */
export function grantsText(grants: KeyGrants): string {
  if (grants.tables == null && grants.models == null && !grants.manage_models) return "Every table and model";
  const tables = grants.tables == null ? "every table" : plural(Object.keys(grants.tables).length, "table entry", "table entries");
  const models = grants.models == null ? "every model" : plural(grants.models.length, "model", "models");
  return [tables, models, ...(grants.manage_models ? ["manages models"] : [])].join(" · ");
}

/** Every entry of a key's grants, one a line, for the list's tooltip. */
export function grantsDetail(grants: KeyGrants): string | undefined {
  const tables = Object.entries(grants.tables ?? {}).map(([entry, actions]) => `${entry}: ${actions.join(", ") || "none"}`);
  return [...tables, ...(grants.models ?? [])].join("\n") || undefined;
}

const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;

/** GET /admin/keys's query for a filter and a page. */
export function keysQuery(state: KeyFilter, cursor?: string) {
  return { limit: KEYS_PAGE, state: state || undefined, cursor };
}

const STATES: Record<KeyState, Status> = {
  live: { label: "Live", color: "var(--good)" },
  expired: { label: "Expired", color: "var(--warning)" },
  revoked: { label: "Revoked", color: "var(--critical)" },
};

export const keyStatus = (state: KeyState): Status => STATES[state];

export const STATE_OPTIONS = (Object.keys(STATES) as KeyState[]).map((value) => ({ value, label: STATES[value].label }));

/** When a key expires, in words: "never", "in 3.0 d", or how long ago it did. */
export function expiryText(key: Pick<ClientKey, "expires_at">, now = Date.now() / 1000): string {
  if (key.expires_at == null) return "Never";
  const left = key.expires_at - now;
  if (left <= 0) return fmtAgo(key.expires_at);
  if (left < 3600) return `in ${Math.max(1, Math.round(left / 60))} min`;
  if (left < 86400) return `in ${(left / 3600).toFixed(1)} h`;
  return `in ${Math.round(left / 86400)} d`;
}
