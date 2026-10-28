import { describe, expect, it } from "vitest";
import type { EnricherSpec } from "../shared/types";
import { enricherHash, fmtFreshness, fmtOutput, locationFromHash, origin, sentSpec, statusLabel, usability } from "./enrichers";

const spec: EnricherSpec = {
  name: "abuseipdb",
  kind: "http",
  description: "IP reputation",
  docs: "https://docs.abuseipdb.com",
  inputs: ["ipv4", "ipv6"],
  terms: "non_commercial",
  attribution: null,
  secret: "ABUSEIPDB_KEY",
  secret_optional: false,
  enabled: true,
  ttl: "24h",
  rate_limit: "1000/day",
  timeout: 10,
  value_filters: ["strip", "lower"],
  request: {
    method: "GET",
    url: "https://api.abuseipdb.com/api/v2/check",
    query: { ipAddress: "{value}", maxAgeInDays: "90" },
    headers: { Key: "{secret}", Accept: "application/json" },
    body: null,
    body_format: "json",
  },
  outputs: { abuse_score: "data.abuseConfidenceScore", country: "data.countryCode" },
};

describe("the enricher location in the hash", () => {
  it("reads the view", () => {
    expect(locationFromHash("#/enrichers")).toEqual({ view: "lookup" });
    expect(locationFromHash("#/enrichers/")).toEqual({ view: "lookup" });
    expect(locationFromHash("#/enrichers/new")).toEqual({ view: "new" });
    expect(locationFromHash("#/enrichers/new?dup=abuseipdb")).toEqual({ view: "new", dup: "abuseipdb" });
    expect(locationFromHash("#/enrichers/abuseipdb")).toEqual({ view: "edit", name: "abuseipdb" });
    expect(locationFromHash("#/data/app")).toEqual({ view: "lookup" });
  });

  it("round-trips", () => {
    for (const loc of [{ view: "lookup" }, { view: "new" }, { view: "new", dup: "a_b" }, { view: "edit", name: "shodan" }] as const) {
      expect(locationFromHash(enricherHash(loc))).toEqual(loc);
    }
  });
});

describe("the spec sent", () => {
  it("leaves a GET's body out, and keeps a POST's", () => {
    expect(sentSpec({ ...spec, request: { ...spec.request, body: { x: 1 } } }).request.body).toBeNull();
    const post: EnricherSpec = { ...spec, request: { ...spec.request, method: "POST", body: { q: "{value}" } } };
    expect(sentSpec(post)).toBe(post);
  });
});

describe("display", () => {
  it("formats outputs", () => {
    expect(fmtOutput(null)).toBe("—");
    expect(fmtOutput(undefined)).toBe("—");
    expect(fmtOutput("US")).toBe("US");
    expect(fmtOutput(0)).toBe("0");
    expect(fmtOutput(false)).toBe("false");
    expect(fmtOutput([1, "a"])).toBe('[1,"a"]');
    expect(fmtOutput({ a: { b: null } })).toBe('{"a":{"b":null}}');
  });

  it("names statuses", () => {
    expect(statusLabel({ status: "ok" })).toEqual({ label: "ok", color: "var(--good)" });
    expect(statusLabel({ status: "rate_limited" }).label).toBe("rate limited");
    expect(statusLabel({ status: "not_found" }).label).toBe("not found");
    expect(statusLabel({ status: "error" }).color).toBe("var(--critical)");
  });

  it("says how fresh a result is", () => {
    expect(fmtFreshness({ cached: false, observed_at: null, ms: 412 })).toBe("fresh · 412 ms");
    expect(fmtFreshness({ cached: false, observed_at: null, ms: null })).toBe("fresh");
    expect(fmtFreshness({ cached: true, observed_at: Date.now() / 1000 - 180, ms: null })).toBe("cached · 3 min ago");
  });

  it("tells usability", () => {
    const base = { usable: true, enabled: true, allowed: true, secret: null, secret_set: false, secret_optional: false };
    expect(usability(base)).toEqual({ label: "usable", color: "var(--good)" });
    expect(usability({ ...base, usable: false, allowed: false }).label).toBe("not allowed by terms");
    expect(usability({ ...base, usable: false, enabled: false }).label).toBe("disabled");
    expect(usability({ ...base, usable: false, secret: "K" })).toEqual({ label: "needs key", color: "var(--warning)" });
    expect(usability({ ...base, secret: "K", secret_optional: true }).label).toBe("usable");
  });

  it("tells where an enricher comes from", () => {
    expect(origin({ builtin: true, overridden: false })).toBe("built-in");
    expect(origin({ builtin: true, overridden: true })).toBe("modified");
    expect(origin({ builtin: false, overridden: false })).toBe("custom");
  });
});
