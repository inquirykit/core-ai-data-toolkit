import { describe, expect, it } from "vitest";
import { fmtSteps, locationFromHash, newSpec, PRESETS, readiness, remoteHash, sentSpec, summary, type RemoteLocation, type RemoteSpec } from "./remotes";

describe("where the Remotes section's hash points", () => {
  it("reads the intro, a new remote (from a preset) and one remote", () => {
    expect(locationFromHash("#/remotes")).toEqual({ view: "intro" });
    expect(locationFromHash("#/remotes/new")).toEqual({ view: "new" });
    expect(locationFromHash("#/remotes/new?preset=r2")).toEqual({ view: "new", preset: "r2" });
    expect(locationFromHash("#/remotes/backups")).toEqual({ view: "edit", name: "backups" });
    expect(locationFromHash("#/data")).toEqual({ view: "intro" });
  });

  it("writes what it reads", () => {
    const places: RemoteLocation[] = [{ view: "intro" }, { view: "new" }, { view: "new", preset: "b2" }, { view: "edit", name: "r2_backups" }];
    for (const loc of places) expect(locationFromHash(remoteHash(loc))).toEqual(loc);
  });
});

describe("a new remote", () => {
  it("starts blank, or from a preset whose secrets are variables to set", () => {
    expect(newSpec()).toEqual({ name: "", scheme: "s3", options: {}, secrets: {} });
    const r2 = newSpec("r2");
    expect(r2.scheme).toBe("s3");
    expect(r2.options).toMatchObject({ region: "auto" });
    expect(r2.secrets).toEqual({ access_key_id: "R2_ACCESS_KEY_ID", secret_access_key: "R2_SECRET_ACCESS_KEY" });
    expect(newSpec("nope")).toEqual(newSpec());
  });

  it("never puts a secret in a preset's options", () => {
    for (const p of PRESETS) {
      for (const option of Object.keys(p.spec.options ?? {})) expect(option).not.toMatch(/key|secret|password|token|credential/);
      for (const variable of Object.values(p.spec.secrets ?? {})) expect(variable).toMatch(/^[A-Z_][A-Z0-9_]*$/);
    }
  });

  it("starts each time afresh: editing one does not change the preset", () => {
    const first = newSpec("fs");
    first.options!.root = "/elsewhere";
    expect(newSpec("fs").options).toEqual({ root: "/mnt/backup/toolkit" });
  });

  it("sends a label of only spaces as none", () => {
    const spec: RemoteSpec = { name: "r2", scheme: "s3", options: {}, secrets: {}, label: "  " };
    expect(sentSpec(spec).label).toBeNull();
    expect(sentSpec({ ...spec, label: " Backups " }).label).toBe("Backups");
  });
});

describe("a remote in words", () => {
  it("is ready unless a variable its secrets name is not set", () => {
    expect(readiness({ missing: [] })).toEqual({ label: "ready", color: "var(--good)" });
    expect(readiness({ missing: ["R2_SECRET_ACCESS_KEY"] })).toEqual({ label: "R2_SECRET_ACCESS_KEY not set", color: "var(--warning)" });
  });

  it("says where its copies go", () => {
    expect(summary({ scheme: "s3", options: { bucket: "scans", region: "auto" } })).toBe("s3 · scans");
    expect(summary({ scheme: "fs", options: { root: "/mnt/backup" } })).toBe("fs · /mnt/backup");
    expect(summary({ scheme: "memory", options: {} })).toBe("memory");
  });

  it("says how a test went, step by step", () => {
    expect(fmtSteps({ steps: [{ step: "write", ms: 12.4 }, { step: "read", ms: 3.25 }, { step: "list", ms: 5 }, { step: "delete", ms: 0.4 }] })).toBe(
      "write 12 ms · read 3.3 ms · list 5.0 ms · delete 0.4 ms",
    );
  });
});
