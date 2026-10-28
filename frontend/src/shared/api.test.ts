import { describe, expect, it, vi } from "vitest";

import { ApiError, AuthError, createClient, fillPath, queryString, type ApiClient } from "./api";
import type { Job } from "./types";

describe("fillPath", () => {
  it("fills the path's parameters, encoded", () => {
    expect(fillPath("/jobs/{id}/cancel", { id: "a b" })).toBe("/jobs/a%20b/cancel");
    expect(fillPath("/db/{namespace}/{table}", { namespace: "n", table: "t?x" })).toBe("/db/n/t%3Fx");
  });

  it("keeps a model ID's slash, but not a dot segment", () => {
    expect(fillPath("/hub/local/{repo_id}", { repo_id: "Qwen/Qwen2.5-0.5B" })).toBe("/hub/local/Qwen/Qwen2.5-0.5B");
    expect(fillPath("/hub/local/{repo_id}", { repo_id: "../admin" })).toBe("/hub/local/%2E%2E/admin");
  });

  it("refuses a missing parameter", () => {
    expect(() => fillPath("/jobs/{id}", {})).toThrow("no value for {id}");
  });
});

describe("queryString", () => {
  it("leaves unset values out and repeats an array's key", () => {
    expect(queryString({ limit: 5, status: undefined, x: null })).toBe("limit=5");
    expect(queryString({ _type: ["a", "b"], q: "é&" })).toBe("_type=a&_type=b&q=%C3%A9%26");
    expect(queryString({})).toBe("");
  });
});

function reply(status: number, body: string | null, type = "application/json"): Response {
  return new Response(status === 204 ? null : body, { status, headers: { "Content-Type": type } });
}

const ORIGIN = "http://toolkit.test";

/** A client whose requests get ``replies`` in turn; ``sent`` holds the requests. */
function fake(replies: Response[], key = "", onUnauthorized: (hadKey: boolean) => void = () => {}, fixed: Record<string, string> = {}) {
  const sent: Request[] = [];
  const fetch = vi.fn(async (request: Request) => {
    sent.push(request);
    return replies.shift()!;
  });
  return { client: createClient(() => key, onUnauthorized, fixed, { baseUrl: ORIGIN, fetch }), sent };
}

describe("createClient", () => {
  it("sends the key, the fixed headers and a JSON body", async () => {
    const { client, sent } = fake([reply(200, '{"id":"j1"}'), reply(200, "{}")], "k", () => {}, { "X-Trace-Tag": "t" });
    await client.post("/jobs/{id}/cancel", { params: { path: { id: "j1" } } });
    expect(sent[0].url).toBe(`${ORIGIN}/jobs/j1/cancel`);
    expect(sent[0].method).toBe("POST");
    expect(sent[0].headers.get("X-API-Key")).toBe("k");
    expect(sent[0].headers.get("X-Trace-Tag")).toBe("t");

    await client.put("/jobs/schedules/{id}", { params: { path: { id: "s" } }, body: { job: "x", params: {}, enabled: true, interval_s: 60 } });
    expect(sent[1].headers.get("Content-Type")).toBe("application/json");
    expect(await sent[1].json()).toMatchObject({ job: "x", interval_s: 60 });
  });

  it("sends no key when there is none, and the query string", async () => {
    const { client, sent } = fake([reply(200, '{"jobs":[]}')]);
    await client.get("/jobs", { params: { query: { limit: 5, status: undefined } } });
    expect(sent[0].url).toBe(`${ORIGIN}/jobs?limit=5`);
    expect(sent[0].headers.has("X-API-Key")).toBe(false);
  });

  it("answers with JSON, text when asked, or nothing", async () => {
    const { client } = fake([
      reply(200, '{"jobs":[]}'),
      reply(200, "line 1\n", "text/plain; charset=utf-8"),
      reply(204, null),
    ]);
    expect(await client.get("/jobs")).toEqual({ jobs: [] });
    expect(await client.get("/jobs/{id}/log", { params: { path: { id: "j" } }, parseAs: "text" })).toBe("line 1\n");
    expect(await client.delete("/jobs/schedules/{id}", { params: { path: { id: "s" } } })).toBeUndefined();
  });

  it("hands over the reply itself, unread, for streams and files", async () => {
    const { client } = fake([reply(200, '{"a":1}\n', "application/jsonl")]);
    const response = await client.raw("get", "/admin/datasets/{dataset_id}/export", { params: { path: { dataset_id: "d" } } });
    expect(await response.text()).toBe('{"a":1}\n');
  });

  it("refuses a web page where a reply should be", async () => {
    const { client } = fake([reply(200, "<!doctype html><html></html>", "text/html")]);
    await expect(client.get("/admin/setup/status")).rejects.toThrow("got a web page");
  });

  it("throws AuthError on 401, after telling the page whether a key was sent", async () => {
    const onUnauthorized = vi.fn();
    const { client } = fake([reply(401, '{"detail":"no"}')], "k", onUnauthorized);
    await expect(client.get("/jobs")).rejects.toBeInstanceOf(AuthError);
    expect(onUnauthorized).toHaveBeenCalledWith(true);
  });

  it("throws ApiError with the server's detail and status", async () => {
    const { client } = fake([
      reply(404, '{"detail":"No such job"}'),
      reply(422, '{"detail":[{"loc":["body","limit"],"msg":"too big"}]}'),
      reply(500, "oops", "text/plain"),
    ]);
    await expect(client.get("/jobs/{id}", { params: { path: { id: "x" } } })).rejects.toMatchObject({ message: "No such job", status: 404 });
    await expect(client.get("/jobs")).rejects.toThrow("limit: too big");
    await expect(client.get("/jobs")).rejects.toEqual(new ApiError("HTTP 500", 500));
  });

  it("keys a query by method, path and options", () => {
    const { client } = fake([]);
    expect(client.queryOptions("get", "/jobs/{id}", { params: { path: { id: "x" } } }).queryKey).toEqual([
      "get",
      "/jobs/{id}",
      { params: { path: { id: "x" } } },
    ]);
  });
});

// Checked by tsc, never run: each @ts-expect-error fails the build if its line stops being an error.
function typeChecks(client: ApiClient) {
  // @ts-expect-error a path the API does not have
  void client.get("/jobz");
  // @ts-expect-error a method the path does not have
  void client.put("/jobs/{id}", { params: { path: { id: "x" } } });
  // @ts-expect-error the path's parameter left out
  void client.get("/jobs/{id}");
  // @ts-expect-error a parameter misnamed
  void client.get("/jobs/{id}", { params: { path: { job_id: "x" } } });
  // @ts-expect-error a body that is not the route's
  void client.post("/jobs/schedules", { body: { nope: 1 } });
  // @ts-expect-error a query parameter the route does not take
  void client.get("/jobs", { params: { query: { lim: 5 } } });

  const job: Promise<Job> = client.get("/jobs/{id}", { params: { path: { id: "x" } } });
  const gone: Promise<void> = client.delete("/jobs/schedules/{id}", { params: { path: { id: "x" } } });
  const log: Promise<string> = client.get("/jobs/{id}/log", { params: { path: { id: "x" }, query: { tail: 100 } }, parseAs: "text" });
  const jobs: Job[] | undefined = client.useQuery("get", "/jobs", {}, { select: (d) => d.jobs }).data;
  return [job, gone, log, jobs, client.get("/jobs"), client.get("/jobs", { params: { query: { limit: 5 } } })];
}

describe("the client's types", () => {
  it("are checked by tsc", () => expect(typeChecks).toBeTypeOf("function"));
});
