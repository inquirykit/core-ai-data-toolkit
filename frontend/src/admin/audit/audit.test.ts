import { describe, expect, it } from "vitest";
import { queryString } from "../../shared/api";
import { actorLabel, AUDIT_PAGE, auditQuery, NO_FILTERS, outcomeOf } from "./audit";

describe("the audit log's query", () => {
  it("holds the filters that are set and the page", () => {
    expect(auditQuery(NO_FILTERS)).toEqual({ limit: AUDIT_PAGE, cursor: undefined, method: undefined, outcome: undefined, actor: undefined, route: undefined });
    expect(queryString(auditQuery({ method: "DELETE", outcome: "refused", actor: "wrong", route: " /db " }, "abc"))).toBe(
      "limit=100&cursor=abc&method=DELETE&outcome=refused&actor=wrong&route=%2Fdb",
    );
  });
});

describe("an entry", () => {
  it("names its outcome by status class and its key in words", () => {
    expect(outcomeOf(204)).toEqual({ label: "Done · 204", color: "var(--good)" });
    expect(outcomeOf(302).label).toBe("Done · 302");
    expect(outcomeOf(401)).toEqual({ label: "Refused · 401", color: "var(--warning)" });
    expect(outcomeOf(500).color).toBe("var(--critical)");
    expect(actorLabel("admin")).toBe("Admin key");
    expect(actorLabel("open")).toBe("No keys set");
    expect(actorLabel("someday")).toBe("someday");
  });
});
