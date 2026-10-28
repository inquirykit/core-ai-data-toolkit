import { describe, expect, it } from "vitest";
import { queryString } from "../shared/api";
import type { VerifyReport } from "../shared/types";
import {
  captureLabel,
  enrichmentLine,
  evidenceHash,
  fmtIso,
  issuesBySeq,
  locationFromHash,
  shortHash,
  timestampState,
  verifySummary,
  workspaceQuery,
} from "./evidence";

describe("hash", () => {
  it("reads the workspace, case and capture from the hash", () => {
    const inDefault = { workspace: "default" };
    expect(locationFromHash("#/evidence")).toEqual({ ...inDefault, caseId: null, captureId: null });
    expect(locationFromHash("#/evidence/inbox")).toEqual({ ...inDefault, caseId: "inbox", captureId: null });
    expect(locationFromHash("#/evidence/inbox/abc-1?x=1")).toEqual({ ...inDefault, caseId: "inbox", captureId: "abc-1" });
    expect(locationFromHash("#/evidence/a%20b")).toEqual({ ...inDefault, caseId: "a b", captureId: null });
    expect(locationFromHash("#/jobs?workspace=w1")).toEqual({ ...inDefault, caseId: null, captureId: null });
    expect(locationFromHash("#/evidence?workspace=w1")).toEqual({ workspace: "w1", caseId: null, captureId: null });
    expect(locationFromHash("#/evidence/inbox/c1?workspace=w1")).toEqual({ workspace: "w1", caseId: "inbox", captureId: "c1" });
  });

  it("writes hashes that read back", () => {
    expect(evidenceHash("default", null)).toBe("#/evidence");
    expect(evidenceHash("default", "inbox")).toBe("#/evidence/inbox");
    expect(evidenceHash("default", "a b", "c/d")).toBe("#/evidence/a%20b/c%2Fd");
    expect(evidenceHash("w-1", null)).toBe("#/evidence?workspace=w-1");
    expect(evidenceHash("w-1", "inbox", "c1")).toBe("#/evidence/inbox/c1?workspace=w-1");
    for (const [workspace, caseId, captureId] of [["default", "a b", "c/d"], ["w-1", "inbox", "c1"]] as const) {
      expect(locationFromHash(evidenceHash(workspace, caseId, captureId))).toEqual({ workspace, caseId, captureId });
    }
  });

  it("puts API calls in the workspace, leaving the default one out", () => {
    expect(workspaceQuery("default")).toEqual({ workspace: undefined });
    expect(workspaceQuery("w1")).toEqual({ workspace: "w1" });
    expect(queryString(workspaceQuery("default"))).toBe("");
    expect(queryString(workspaceQuery("w 1"))).toBe("workspace=w%201");
  });
});

describe("words", () => {
  it("shortens hashes and formats times", () => {
    expect(shortHash("0123456789abcdef0123456789abcdef")).toBe("01234567…89abcdef");
    expect(shortHash("short")).toBe("short");
    expect(shortHash(null)).toBe("–");
    expect(fmtIso(null)).toBe("–");
    expect(fmtIso("not a time")).toBe("not a time");
    expect(fmtIso("2026-10-18T12:00:00.000Z")).not.toBe("2026-10-18T12:00:00.000Z");
  });

  it("names captures and their time stamp state", () => {
    expect(captureLabel({ title: "  ", url: "https://x", captureId: "id" })).toBe("https://x");
    expect(captureLabel({ title: "T", url: null, captureId: "id" })).toBe("T");
    expect(captureLabel({ title: null, url: null, captureId: "id" })).toBe("id");
    expect(timestampState({ timestampedAt: "2026-10-18T12:00:00Z" }).label).toBe("Timestamped");
    expect(timestampState({ timestampedAt: null }).label).toBe("No timestamp");
  });

  it("puts an enrichment item in a line", () => {
    expect(enrichmentLine({ value: "x", type: "host", error: "timeout" })).toBe("failed: timeout");
    expect(enrichmentLine({ value: "x", type: "domain", results: { value: "x", type: "domain", results: [], merged: {}, sources: {} } })).toBe(
      "no enricher takes this type",
    );
    const one = { enricher: "e", status: "not_found" } as never;
    expect(enrichmentLine({ value: "x", type: "domain", results: { value: "x", type: "domain", results: [one], merged: {}, sources: {} } })).toBe(
      "1 source, nothing found",
    );
    expect(
      enrichmentLine({
        value: "1.1.1.1",
        type: "ipv4",
        results: { value: "1.1.1.1", type: "ipv4", results: [], merged: { country: "AU", asn: 13335, tags: ["cdn"] }, sources: {} },
      }),
    ).toBe('country: AU · asn: 13335 · tags: ["cdn"]');
  });
});

describe("verify report", () => {
  const base: VerifyReport = { workspace: "default", ok: true, records: 3, timestamped: 2, headHash: "h", signingKeySha256: "k", issues: [] };
  it("summarizes", () => {
    expect(verifySummary({ ...base, records: 0, timestamped: 0 })).toBe("The ledger is empty.");
    expect(verifySummary(base)).toBe("All 3 records verify. 2 of 3 timestamped.");
    const issues: VerifyReport["issues"] = [
      { severity: "warning", seq: 3, captureId: "c", message: "no trusted timestamp" },
      { severity: "error", seq: 2, captureId: "b", message: "signature does not verify" },
      { severity: "warning", seq: 2, captureId: "b", message: "signed by a different key" },
    ];
    expect(verifySummary({ ...base, ok: false, issues })).toBe("1 error in 3 records. 2 of 3 timestamped, 2 warnings.");
    expect(issuesBySeq({ ...base, issues }).map(([seq, list]) => [seq, list.map((i) => i.severity)])).toEqual([
      [2, ["error", "warning"]],
      [3, ["warning"]],
    ]);
  });
});
