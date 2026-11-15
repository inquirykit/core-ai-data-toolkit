// What the Remotes section (RemotesSection.tsx) needs that is not drawing:
// where the hash points, what a new remote starts from (a blank one, or a
// preset of a common place: R2, B2, S3, a folder), what the editor sends,
// and a remote's state and test in words.

import type { Schemas } from "../../shared/api";
import { parseHash } from "../../shared/browser";
import type { Status } from "../../shared/types";

/** A remote: its spec, the buckets that name it, and the variables its secrets name that are not set. */
export type RemoteInfo = Schemas["RemoteInfo"];
/** What PUT /remotes/{name} takes. */
export type RemoteSpec = Schemas["RemoteSpec"];
/** A test that worked: each step with its time. */
export type RemoteTest = Schemas["RemoteTest"];

// ------------------------------------------------------------------ hash
export type RemoteLocation =
  | { view: "intro" }
  /** The new-remote form, started from a preset when given */
  | { view: "new"; preset?: string }
  | { view: "edit"; name: string };

/** What "#/remotes", "#/remotes/new?preset=r2" and "#/remotes/<name>" point at. */
export function locationFromHash(hash: string): RemoteLocation {
  const { parts, params } = parseHash(hash);
  const [section, name] = parts;
  if (section !== "remotes" || !name) return { view: "intro" };
  if (name === "new") {
    const preset = params.get("preset");
    return preset ? { view: "new", preset } : { view: "new" };
  }
  return { view: "edit", name };
}

export function remoteHash(loc: RemoteLocation): string {
  switch (loc.view) {
    case "intro":
      return "#/remotes";
    case "new":
      return loc.preset ? `#/remotes/new?preset=${encodeURIComponent(loc.preset)}` : "#/remotes/new";
    case "edit":
      return `#/remotes/${encodeURIComponent(loc.name)}`;
  }
}

// ------------------------------------------------------------------ editor
/** The editor's value: the spec as its form (made from the RemoteSpec schema) holds it while it is written. */
export type EditedSpec = Partial<RemoteSpec>;

/** A common place to copy to, as the start of a new remote: its scheme, its options to fill in, its secrets named. */
export interface Preset {
  key: string;
  label: string;
  spec: EditedSpec;
}

/**
 * The presets a new remote can start from. Their options hold what to replace (<account id>, <region>), and their
 * secrets name the variables the service's environment is to set.
 */
export const PRESETS: Preset[] = [
  {
    key: "r2",
    label: "Cloudflare R2",
    spec: {
      scheme: "s3",
      options: { bucket: "", endpoint: "https://<account id>.r2.cloudflarestorage.com", region: "auto" },
      secrets: { access_key_id: "R2_ACCESS_KEY_ID", secret_access_key: "R2_SECRET_ACCESS_KEY" },
    },
  },
  {
    key: "b2",
    label: "Backblaze B2",
    spec: {
      scheme: "s3",
      options: { bucket: "", endpoint: "https://s3.<region>.backblazeb2.com", region: "<region>" },
      secrets: { access_key_id: "B2_KEY_ID", secret_access_key: "B2_APPLICATION_KEY" },
    },
  },
  {
    key: "s3",
    label: "Amazon S3",
    spec: {
      scheme: "s3",
      options: { bucket: "", region: "us-east-1" },
      secrets: { access_key_id: "AWS_ACCESS_KEY_ID", secret_access_key: "AWS_SECRET_ACCESS_KEY" },
    },
  },
  { key: "fs", label: "A folder", spec: { scheme: "fs", options: { root: "/mnt/backup/toolkit" }, secrets: {} } },
];

/** A new remote before anything is written: the preset's, when one is named, else a blank one of S3's API. */
export function newSpec(preset?: string): EditedSpec {
  const from = PRESETS.find((p) => p.key === preset)?.spec ?? { scheme: "s3", options: {}, secrets: {} };
  return { name: "", ...from, options: { ...from.options }, secrets: { ...from.secrets } };
}

/** The spec as the editor sends it: a label of only spaces is none. */
export function sentSpec(spec: RemoteSpec): RemoteSpec {
  const label = spec.label?.trim();
  return { ...spec, label: label ? label : null };
}

// --------------------------------------------------------------- display
/** Whether the remote can be used now: not while a variable its secrets name is not set. */
export function readiness(info: Pick<RemoteInfo, "missing">): Status {
  return info.missing.length
    ? { label: `${info.missing.join(", ")} not set`, color: "var(--warning)" }
    : { label: "ready", color: "var(--good)" };
}

/** Where a remote's copies go, in a line: "s3 · scans" (the bucket, else the root, when it has one). */
export function summary(info: Pick<RemoteSpec, "scheme" | "options">): string {
  const where = info.options?.bucket || info.options?.root || info.options?.container || "";
  return where ? `${info.scheme} · ${where}` : info.scheme;
}

/** A test that worked, in words: "write 12 ms · read 3 ms · list 5 ms · delete 4 ms". */
export function fmtSteps(test: Pick<RemoteTest, "steps">): string {
  return test.steps.map((s) => `${s.step} ${s.ms < 10 ? s.ms.toFixed(1) : Math.round(s.ms)} ms`).join(" · ");
}
