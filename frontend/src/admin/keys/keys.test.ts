import { describe, expect, it } from "vitest";
import { queryString } from "../../shared/api";
import { expiryText, grantsBody, grantsForm, grantsText, keysQuery, keyStatus, newKeyBody, NO_GRANTS, toggled } from "./keys";

describe("a new key", () => {
  it("is sent with its name trimmed and its expiry in days, or none", () => {
    expect(newKeyBody(" worker-3 ", "90")).toEqual({ name: "worker-3", expires_in_days: 90, grants: {} });
    expect(newKeyBody("worker-3", "", { models: ["a/b"] })).toEqual({ name: "worker-3", grants: { models: ["a/b"] } });
  });

  it("needs a name of at most 100 characters", () => {
    expect(newKeyBody("   ", "90")).toMatch(/Name the key/);
    expect(newKeyBody("x".repeat(101), "90")).toMatch(/at most 100/);
  });
});

describe("the list", () => {
  it("asks for a state and a page only when given", () => {
    expect(queryString(keysQuery(""))).toBe("limit=50");
    expect(queryString(keysQuery("revoked", "abc"))).toBe("limit=50&state=revoked&cursor=abc");
  });
});

describe("a key", () => {
  it("says its state and when it expires", () => {
    expect(keyStatus("revoked").label).toBe("Revoked");
    const now = 1_000_000;
    expect(expiryText({ expires_at: null }, now)).toBe("Never");
    expect(expiryText({ expires_at: now + 3 * 86400 }, now)).toBe("in 3 d");
    expect(expiryText({ expires_at: now + 7200 }, now)).toBe("in 2.0 h");
    expect(expiryText({ expires_at: now + 30 }, now)).toBe("in 1 min");
  });
});

describe("a key's grants", () => {
  it("limit nothing until the form does", () => {
    expect(grantsBody(NO_GRANTS)).toEqual({ tables: null, models: null, manage_models: false });
    expect(grantsForm({})).toEqual(NO_GRANTS);
    expect(grantsText({ tables: null, models: null, manage_models: false })).toBe("Every table and model");
  });

  it("are the form's tables, models and managing, and read back the same", () => {
    const form = {
      limitTables: true,
      rows: [
        { entry: " docs.* ", actions: toggled(["read"], "write", true) },
        { entry: "docs.notes", actions: ["delete" as const] },
        { entry: "", actions: ["read" as const] },
      ],
      limitModels: true,
      models: "a/b, my model:latest ,",
      manageModels: true,
    };
    const grants = { tables: { "docs.*": ["read", "write"], "docs.notes": ["delete"] }, models: ["a/b", "my model:latest"], manage_models: true };
    expect(grantsBody(form)).toEqual(grants);
    expect(grantsBody(grantsForm(grantsBody(form)))).toEqual(grants);
    expect(grantsText(grantsBody(form))).toBe("2 table entries · 2 models · manages models");
  });

  it("add up a table named twice, as the service does", () => {
    const rows = [
      { entry: "docs.*", actions: ["write" as const] },
      { entry: "docs.*", actions: ["read" as const] },
    ];
    expect(grantsBody({ ...NO_GRANTS, limitTables: true, rows }).tables).toEqual({ "docs.*": ["read", "write"] });
  });

  it("can shut every table, or every model, and keep it so through the editor", () => {
    const none = { tables: {}, models: [], manage_models: false };
    expect(grantsBody({ ...NO_GRANTS, limitTables: true, rows: [], limitModels: true })).toEqual(none);
    expect(grantsBody(grantsForm(none))).toEqual(none);
    expect(grantsText(none)).toBe("0 table entries · 0 models");
  });

  it("keep their actions in one order", () => {
    expect(toggled(["delete"], "read", true)).toEqual(["read", "delete"]);
    expect(toggled(["read", "write"], "read", false)).toEqual(["write"]);
  });
});
