// What a bucket's page (DataBucket.tsx) says of its files and of their copies at its remote.

import type { Schemas } from "../../../shared/api";
import type { Status } from "../../../shared/types";

/** A file of a bucket: its row (id, name, size, sha256, content_type, created, copied); its bytes are downloaded. */
export type BucketFile = Schemas["BucketFile"];
/** A bucket's remote and how its copy stands (GET /db/{ws}/{bucket}/_remote). */
export type BucketRemote = Schemas["BucketRemote"];

/** How the bucket's copy to its remote stands, in a line with the color of its dot. */
export function copyStatus(state: Pick<BucketRemote, "remote" | "waiting" | "task">): Status {
  const task = state.task;
  if (!state.remote) return { label: "No remote: its files are on this disk only", color: "var(--muted-foreground)" };
  if (task?.status === "failed") return { label: `Copying failed: ${task.error ?? "no reason given"}`, color: "var(--critical)" };
  if (task?.status === "queued" && task.retried > 0) {
    return { label: `Retry ${task.retried} of ${task.retries} waits: ${task.error ?? ""}`.trim(), color: "var(--warning)" };
  }
  if (task?.status === "running") return { label: `Copying: ${task.message ?? "under way"}`, color: "var(--warning)" };
  if (state.waiting) return { label: `${state.waiting === 1 ? "A file waits" : `${state.waiting} files wait`} to be copied`, color: "var(--warning)" };
  return { label: "Every file is copied", color: "var(--good)" };
}

/**
 * Whether the bucket's copy is behind with nothing copying it now: files wait, or its last copy failed, was cancelled
 * or waits for a retry. Then the admin gets Copy now, which names its remote again: that starts its copy at once.
 */
export function copyBehind(state: Pick<BucketRemote, "remote" | "waiting" | "task">): boolean {
  const task = state.task;
  if (!state.remote || task?.status === "running") return false;
  if (task?.status === "queued") return task.retried > 0;
  return Boolean(state.waiting) || task?.status === "failed" || task?.status === "cancelled";
}

/** A copy that is queued or running: the bucket's page asks again until it ends. */
export const copying = (state: Pick<BucketRemote, "task"> | undefined): boolean => state?.task?.status === "queued" || state?.task?.status === "running";

/** A file's Copied cell: when it reached the remote, "waiting" until then, "–" in a bucket without a remote. */
export function copiedCell(file: Pick<BucketFile, "copied">, remote: string | null, ago: (ts: number) => string): string {
  if (file.copied) return ago(file.copied);
  return remote ? "waiting" : "–";
}

/** The start of a SHA-256, enough to tell files apart at a glance; the whole one is the cell's title. */
export const shortHash = (sha256: string): string => sha256.slice(0, 12);

/** What uploading several files says: how many went in, and each that did not with why ("" when all went in). */
export function uploadReport(total: number, failed: [name: string, why: string][]): { done: string; failed: string } {
  const went = total - failed.length;
  const done = went === total ? `Uploaded ${went === 1 ? "the file" : `${went} files`}.` : `Uploaded ${went} of ${total} files.`;
  return { done, failed: failed.map(([name, why]) => `${name}: ${why}`).join("; ") };
}
