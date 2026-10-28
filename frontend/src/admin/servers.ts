// What the Model servers section (ServersSection.tsx) needs that is not
// drawing: where the hash points, the editor's forms to and from the API's
// entries, checking them, and the words and colors of what a probe found.

import { parseHash } from "../shared/browser";
import type { KV } from "../shared/components";
import type { Status } from "../shared/types";
import { parseJsonObject } from "../shared/json";
import type { Backend, BackendProbe, ModelEntry, ModelServerInfo, ModelTask, Provider } from "../shared/types";

// ------------------------------------------------------------------ hash
export type ServersLocation =
  | { view: "overview" }
  /** The new-entry form, prefilled from ``dup`` when given */
  | { view: "new"; dup?: string }
  | { view: "model"; id: string };

/** What "#/servers", "#/servers/new?dup=x" and "#/servers/model/<id>" point at. */
export function locationFromHash(hash: string): ServersLocation {
  const { parts, params } = parseHash(hash);
  const [section, view, id] = parts;
  if (section !== "servers") return { view: "overview" };
  if (view === "new") {
    const dup = params.get("dup");
    return dup ? { view: "new", dup } : { view: "new" };
  }
  if (view === "model" && id) return { view: "model", id };
  return { view: "overview" };
}

export function serversHash(loc: ServersLocation): string {
  switch (loc.view) {
    case "overview":
      return "#/servers";
    case "new":
      return loc.dup ? `#/servers/new?dup=${encodeURIComponent(loc.dup)}` : "#/servers/new";
    case "model":
      return `#/servers/model/${encodeURIComponent(loc.id)}`;
  }
}

// ----------------------------------------------------------- shell words
/** A command line as its words, the way the server's shlex.split reads it: quotes and backslashes. */
export function shellSplit(line: string): string[] {
  const words: string[] = [];
  let word = "";
  let inWord = false;
  let quote: "'" | '"' | null = null;
  for (let i = 0; i < line.length; i++) {
    const c = line[i];
    if (quote === "'") {
      if (c === "'") quote = null;
      else word += c;
    } else if (quote === '"') {
      if (c === '"') quote = null;
      else if (c === "\\" && i + 1 < line.length && '"\\$`'.includes(line[i + 1])) word += line[++i];
      else word += c;
    } else if (c === "'" || c === '"') {
      quote = c;
      inWord = true;
    } else if (c === "\\" && i + 1 < line.length) {
      word += line[++i];
      inWord = true;
    } else if (/\s/.test(c)) {
      if (inWord) words.push(word);
      word = "";
      inWord = false;
    } else {
      word += c;
      inWord = true;
    }
  }
  if (quote) throw new Error(`The command has an unclosed ${quote}`);
  if (inWord) words.push(word);
  return words;
}

/** Words as one command line that ``shellSplit`` reads back the same; only words that need it are quoted. */
export const shellJoin = (words: string[]): string =>
  words.map((w) => (w && /^[\w@%+=:,./{}-]+$/.test(w) ? w : `'${w.replaceAll("'", `'"'"'`)}'`)).join(" ");

// ------------------------------------------------------------------ form
export type BackendKind = "provider" | "url" | "cmd" | "hub";
export const BACKEND_KINDS: [BackendKind, string, string][] = [
  ["provider", "Provider", "A server named once under providers, such as Ollama or LM Studio"],
  ["url", "URL", "A server that already runs elsewhere"],
  ["cmd", "Command", "A server this service starts when the model loads, and stops when it is evicted"],
  ["hub", "In-process", "A Hugging Face model run in this process: the last resort"],
];
export const TASKS: ModelTask[] = ["text-generation", "vision-language", "embedding", "rerank", "speech-to-text"];

/** One backend's editor: every field as text, empty meaning not given (the provider's value, or the default). */
export interface BackendForm {
  kind: BackendKind;
  provider: string;
  url: string;
  /** The command as one line of shell words */
  cmd: string;
  hub: string;
  upstream_model: string;
  memory_gb: string;
  max_concurrency: string;
  startup_timeout: string;
  request_timeout: string;
  health_path: string;
  on_evict: "" | NonNullable<Backend["on_evict"]>;
  env: KV[];
  /** A JSON object as text, parsed on submit */
  extra: string;
}

export interface EntryForm {
  model_id: string;
  task: ModelTask;
  description: string;
  /** Comma-separated */
  tags: string;
  backends: BackendForm[];
}

export const emptyBackend = (kind: BackendKind = "provider", provider = ""): BackendForm => ({
  kind,
  provider: kind === "provider" ? provider : "",
  url: "",
  cmd: "",
  hub: "",
  upstream_model: "",
  memory_gb: "",
  max_concurrency: "",
  startup_timeout: "",
  request_timeout: "",
  health_path: "",
  on_evict: "",
  env: [],
  extra: "",
});

export const emptyEntry = (provider = ""): EntryForm => ({
  model_id: "",
  task: "vision-language",
  description: "",
  tags: "",
  backends: [emptyBackend(provider ? "provider" : "url", provider)],
});

const text = (value: string | number | null | undefined) => (value == null ? "" : String(value));

export function kindOf(backend: Backend): BackendKind {
  if (backend.provider) return "provider";
  if (backend.cmd?.length) return "cmd";
  if (backend.hub) return "hub";
  return "url";
}

export function backendForm(backend: Backend): BackendForm {
  return {
    kind: kindOf(backend),
    provider: text(backend.provider),
    url: text(backend.url),
    cmd: backend.cmd?.length ? shellJoin(backend.cmd) : "",
    hub: text(backend.hub),
    upstream_model: text(backend.upstream_model),
    memory_gb: text(backend.memory_gb),
    max_concurrency: text(backend.max_concurrency),
    startup_timeout: text(backend.startup_timeout),
    request_timeout: text(backend.request_timeout),
    health_path: text(backend.health_path),
    on_evict: backend.on_evict ?? "",
    env: Object.entries(backend.env ?? {}).map(([key, value]) => ({ key, value })),
    extra: backend.extra && Object.keys(backend.extra).length ? JSON.stringify(backend.extra, null, 2) : "",
  };
}

export function entryForm(entry: ModelEntry, modelId: string): EntryForm {
  return {
    model_id: modelId,
    task: entry.task,
    description: entry.description ?? "",
    tags: (entry.tags ?? []).join(", "),
    backends: entry.backends.map(backendForm),
  };
}

/** The extra fields as an object, or what is wrong with the text. Empty text is none. */
export function parseExtra(value: string): { extra: Record<string, unknown> } | { error: string } {
  const parsed = parseJsonObject(value, { what: "Extra fields", plural: true, emptyMeans: {} });
  return parsed.ok ? { extra: parsed.value } : { error: parsed.error };
}

/** The backend the form describes, with only the fields it gives. Throws on bad JSON or quotes: validate first. */
export function backendFromForm(form: BackendForm): Backend {
  const out: Backend = {};
  if (form.kind === "provider") out.provider = form.provider.trim();
  if (form.kind === "url") out.url = form.url.trim();
  if (form.kind === "cmd") out.cmd = shellSplit(form.cmd);
  if (form.kind === "hub") out.hub = form.hub.trim();
  if (form.upstream_model.trim()) out.upstream_model = form.upstream_model.trim();
  for (const name of ["memory_gb", "max_concurrency", "startup_timeout", "request_timeout"] as const) {
    if (form[name].trim()) out[name] = Number(form[name]);
  }
  if (form.health_path.trim()) out.health_path = form.health_path.trim();
  if (form.on_evict) out.on_evict = form.on_evict;
  if (form.kind === "cmd" && form.env.length) out.env = Object.fromEntries(form.env.map((r) => [r.key.trim(), r.value]));
  const extra = parseExtra(form.extra);
  if ("error" in extra) throw new Error(extra.error);
  if (Object.keys(extra.extra).length) out.extra = extra.extra;
  return out;
}

export function entryFromForm(form: EntryForm): ModelEntry {
  const tags = form.tags.split(",").map((t) => t.trim()).filter(Boolean);
  return {
    task: form.task,
    backends: form.backends.map(backendFromForm),
    ...(form.description.trim() ? { description: form.description.trim() } : {}),
    ...(tags.length ? { tags } : {}),
  };
}

/** The same form as the editor shows it; two forms with the same key are the same entry. */
export const formKey = (form: EntryForm): string => JSON.stringify(form);

const MODEL_ID_RE = /^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}$/;
const ENV_RE = /^[A-Za-z_][A-Za-z0-9_]*$/;
const URL_RE = /^https?:\/\/[^\s/]+/;

function numberProblem(value: string, what: string, min: number, max: number, integer = false): string | null {
  if (!value.trim()) return null;
  const n = Number(value);
  if (!Number.isFinite(n) || n < min || n > max || (integer && !Number.isInteger(n))) {
    return `${what} is ${integer ? "a whole number" : "a number"} from ${min} to ${max}`;
  }
  return null;
}

/** What stops the form being saved; empty when nothing does. ``providers``: the names that exist. */
export function validateEntry(form: EntryForm, providers: string[]): string[] {
  const errors: string[] = [];
  if (!MODEL_ID_RE.test(form.model_id.trim())) errors.push("The model ID is letters, digits and ._:/@+-, up to 200");
  if (!form.backends.length) errors.push("Add at least one backend");
  form.backends.forEach((b, i) => {
    const at = `Backend ${i + 1}`;
    if (b.kind === "provider" && !b.provider) errors.push(`${at}: pick a provider`);
    if (b.kind === "provider" && b.provider && !providers.includes(b.provider)) errors.push(`${at}: there is no provider named ${b.provider}`);
    if (b.kind === "url" && !URL_RE.test(b.url.trim())) errors.push(`${at}: the URL starts with http:// or https:// and a host`);
    if (b.kind === "hub" && !b.hub.trim()) errors.push(`${at}: give the Hub model ID`);
    if (b.kind === "cmd") {
      try {
        if (!shellSplit(b.cmd).length) errors.push(`${at}: give the command`);
      } catch (e) {
        errors.push(`${at}: ${(e as Error).message}`);
      }
    }
    for (const problem of [
      numberProblem(b.memory_gb, "Memory", 0, 4096),
      numberProblem(b.max_concurrency, "Concurrency", 1, 1024, true),
      numberProblem(b.startup_timeout, "The startup timeout", 1, 86400),
      numberProblem(b.request_timeout, "The request timeout", 1, 86400),
    ]) {
      if (problem) errors.push(`${at}: ${problem}`);
    }
    if (b.health_path.trim() && !b.health_path.trim().startsWith("/")) errors.push(`${at}: the health path starts with /`);
    if (b.on_evict === "sleep" && b.kind !== "cmd") errors.push(`${at}: sleep on evict is for commands (vLLM)`);
    if (b.on_evict === "unload" && (b.kind === "cmd" || b.kind === "hub")) errors.push(`${at}: unload on evict is for providers and URLs (Ollama)`);
    if (b.kind === "cmd") {
      for (const row of b.env) if (!ENV_RE.test(row.key.trim())) errors.push(`${at}: ${row.key.trim() || "a variable"} is not a valid variable name`);
    }
    const extra = parseExtra(b.extra);
    if ("error" in extra) errors.push(`${at}: ${extra.error}`);
  });
  return errors;
}

// -------------------------------------------------------------- providers
/** Providers most people run, to start from: a name and the settings (the Provider schema's fields; its form fills the rest). */
export const PROVIDER_PRESETS: [label: string, name: string, settings: Partial<Provider>][] = [
  ["Ollama", "ollama", { url: "http://127.0.0.1:11434", max_concurrency: 4, on_evict: "unload", description: "Ollama on this machine" }],
  ["LM Studio", "lmstudio", { url: "http://127.0.0.1:1234", max_concurrency: 4, on_evict: "stop", description: "LM Studio's server on this machine (JIT loading on)" }],
];

const PROVIDER_NAME_RE = /^[a-z][a-z0-9_-]{0,62}$/;

/** What is wrong with a provider's name (it is in the path, not in the Provider schema), or "". */
export const providerNameProblem = (name: string): string =>
  PROVIDER_NAME_RE.test(name.trim()) ? "" : "The name is lower case letters, digits, _ and -, starting with a letter";

// --------------------------------------------------------------- display
/** What a backend is in a few words: the provider, the URL's host, the program, or in-process with the Hub ID. */
export function backendLabel(backend: Backend): string {
  switch (kindOf(backend)) {
    case "provider":
      return backend.provider ?? "";
    case "cmd":
      return backend.cmd?.[0]?.split("/").pop() ?? "";
    case "hub":
      return `in-process ${backend.hub ?? ""}`.trim();
    case "url":
      return (backend.url ?? "").replace(/^https?:\/\//, "");
  }
}

export const PROBE: Record<BackendProbe["state"], Status> = {
  ok: { label: "can serve", color: "var(--good)" },
  unlisted: { label: "does not list the model", color: "var(--warning)" },
  unavailable: { label: "unavailable", color: "var(--critical)" },
};

export const ORIGIN: Record<ModelServerInfo["origin"], string> = {
  file: "file",
  saved: "saved here",
  replaced: "edited here",
};

export const STATUS_COLOR: Record<string, string> = {
  loaded: "var(--good)",
  loading: "var(--warning)",
  unloaded: "var(--muted-foreground)",
};
