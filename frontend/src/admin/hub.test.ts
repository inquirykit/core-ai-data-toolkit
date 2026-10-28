import { describe, expect, it } from "vitest";
import { queryString } from "../shared/api";
import type { HubDownload, HubLocalRepo, HubSearchResult } from "../shared/types";
import { downloadProgress, mergeDownloads, newlyFinished, searchAction, searchQuery, sortLocal, TASK_FILTERS } from "./hub";

const download = (over: Partial<HubDownload> = {}): HubDownload => ({
  id: "d1",
  repo_id: "org/model",
  status: "running",
  total_bytes: 1000,
  done_bytes: 250,
  started: 1,
  finished: null,
  error: null,
  ...over,
});

const result = (over: Partial<HubSearchResult> = {}): HubSearchResult => ({
  id: "org/model",
  pipeline_tag: "text-generation",
  library_name: "transformers",
  downloads: 10,
  likes: 1,
  last_modified: null,
  gated: false,
  tags: [],
  on_disk: false,
  ...over,
});

const repo = (over: Partial<HubLocalRepo> = {}): HubLocalRepo => ({
  id: "org/model",
  size_bytes: 10,
  weight_bytes: 8,
  files: 3,
  last_accessed: null,
  last_modified: null,
  loaded: false,
  loaded_tasks: [],
  tasks: [],
  ...over,
});

describe("the search query", () => {
  it("leaves the task out for all tasks and trims the words", () => {
    expect(searchQuery(" bert base ", "")).toEqual({ q: "bert base", task: undefined, limit: 30 });
    expect(searchQuery("qwen", "text-generation", 10)).toEqual({ q: "qwen", task: "text-generation", limit: 10 });
    expect(queryString(searchQuery(" bert base ", ""))).toBe("q=bert%20base&limit=30");
  });

  it("offers every task once, all tasks first", () => {
    expect(TASK_FILTERS).not.toContain("");
    expect(new Set(TASK_FILTERS).size).toBe(TASK_FILTERS.length);
  });
});

describe("the search row's action", () => {
  it("prefers what is on disk, then a download on its way, then the gate", () => {
    const none = new Set<string>();
    expect(searchAction(result(), [], none)).toBe("download");
    expect(searchAction(result({ gated: true }), [], none)).toBe("gated");
    expect(searchAction(result({ gated: true }), [download({ status: "queued" })], none)).toBe("downloading");
    expect(searchAction(result(), [download({ status: "done" })], none)).toBe("download");
    expect(searchAction(result(), [download()], new Set(["org/model"]))).toBe("on_disk");
    expect(searchAction(result({ on_disk: true }), [], none)).toBe("on_disk");
  });
});

describe("download progress", () => {
  it("is the fraction done with a total", () => {
    expect(downloadProgress(download())).toEqual({ fraction: 0.25, label: "250 B of 1000 B" });
    expect(downloadProgress(download({ done_bytes: 1200 })).fraction).toBe(1);
  });

  it("is indeterminate without a total until done", () => {
    expect(downloadProgress(download({ total_bytes: null }))).toEqual({ fraction: null, label: "250 B so far" });
    expect(downloadProgress(download({ total_bytes: null, status: "done" }))).toEqual({ fraction: 1, label: "250 B" });
  });
});

describe("merging polled downloads", () => {
  it("estimates the speed from the previous poll while running", () => {
    const first = mergeDownloads([], [download()], 10_000);
    expect(first[0].speed).toBeNull();
    const second = mergeDownloads(first, [download({ done_bytes: 750 })], 12_000);
    expect(second[0].speed).toBe(250);
    expect(second[0].sampledAt).toBe(12_000);
  });

  it("keeps the last estimate over a very short interval", () => {
    const first = mergeDownloads([], [download()], 10_000);
    const second = mergeDownloads(first, [download({ done_bytes: 750 })], 12_000);
    const third = mergeDownloads(second, [download({ done_bytes: 760 })], 12_400);
    expect(third[0].speed).toBe(250);
    expect(third[0].sampledAt).toBe(12_000);
  });

  it("follows the server's list and drops the speed once a row stops", () => {
    const prev = mergeDownloads([], [download(), download({ id: "d2", repo_id: "a/b" })], 1000);
    const next = mergeDownloads(prev, [download({ id: "d2", repo_id: "a/b", status: "done", done_bytes: 1000 })], 3000);
    expect(next.map((v) => v.row.id)).toEqual(["d2"]);
    expect(next[0].speed).toBeNull();
  });
});

describe("a download finishing", () => {
  it("is seen when a row turns done, including one never seen before", () => {
    expect(newlyFinished([download()], [download({ status: "done" })])).toBe(true);
    expect(newlyFinished([], [download({ status: "done" })])).toBe(true);
    expect(newlyFinished([download({ status: "done" })], [download({ status: "done" })])).toBe(false);
    expect(newlyFinished([download()], [download({ status: "failed" })])).toBe(false);
  });
});

describe("local repos", () => {
  it("sorts loaded first, then the most recently used, never used last", () => {
    const repos = [
      repo({ id: "c/old", last_accessed: 10 }),
      repo({ id: "b/never" }),
      repo({ id: "a/new", last_accessed: 20 }),
      repo({ id: "d/loaded", loaded: true }),
    ];
    expect(sortLocal(repos).map((r) => r.id)).toEqual(["d/loaded", "a/new", "c/old", "b/never"]);
    // The input is left alone.
    expect(repos[0].id).toBe("c/old");
  });
});
