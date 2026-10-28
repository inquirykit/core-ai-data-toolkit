import { describe, expect, it } from "vitest";
import { NAV, NAV_GROUPS, sectionFromHash } from "./nav";

describe("NAV", () => {
  it("keeps each group's items together, in the groups' order, Settings last", () => {
    const order = [...NAV_GROUPS.map((g) => g.key), "system"];
    const groups = NAV.map((item) => order.indexOf(item.group));
    expect(groups).toEqual([...groups].sort((a, b) => a - b));
    expect(NAV.at(-1)?.key).toBe("settings");
  });
});

describe("sectionFromHash", () => {
  it("reads the section from the hash", () => {
    expect(sectionFromHash("#/models")).toBe("models");
    expect(sectionFromHash("#running")).toBe("running");
    expect(sectionFromHash("#/jobs")).toBe("jobs");
    expect(sectionFromHash("#/activity?x=1")).toBe("activity");
    expect(sectionFromHash("#/data/app/todo")).toBe("data");
    expect(sectionFromHash("#/enrichers/new?dup=abuseipdb")).toBe("enrichers");
    expect(sectionFromHash("#/servers/model/qwen3.5-4b")).toBe("servers");
  });

  it("sends old Traces links to Activity & logs", () => {
    expect(sectionFromHash("#/traces")).toBe("activity");
    expect(sectionFromHash("#/traces/abc")).toBe("activity");
  });

  it("falls back to the overview", () => {
    expect(sectionFromHash("")).toBe("overview");
    expect(sectionFromHash("#/playground")).toBe("overview");
    expect(sectionFromHash("#code=abc")).toBe("overview");
    expect(sectionFromHash("#/hub")).toBe("overview"); // disabled section
  });

  it("links every admin section to its hash", () => {
    for (const item of NAV.filter((n) => n.key !== "playground" && !n.off)) {
      expect(sectionFromHash(new URL(item.href, "http://x").hash)).toBe(item.key);
    }
  });
});
