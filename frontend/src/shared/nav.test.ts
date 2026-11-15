import { describe, expect, it } from "vitest";
import { menuEntry, NAV, NAV_GROUPS, sectionFromHash, tabsOf } from "./nav";

describe("NAV", () => {
  it("keeps each group's items together, in the groups' order, Settings last", () => {
    const order = [...NAV_GROUPS.map((g) => g.key), "system"];
    const groups = NAV.map((item) => order.indexOf(item.group));
    expect(groups).toEqual([...groups].sort((a, b) => a - b));
    expect(NAV.filter((item) => !item.under).at(-1)?.key).toBe("settings");
  });

  it("puts a tab under a menu entry of its group", () => {
    for (const item of NAV.filter((n) => n.under)) {
      const entry = NAV.find((n) => n.key === item.under);
      expect(entry, item.key).toBeDefined();
      expect(entry?.under, item.key).toBeUndefined();
      expect(entry?.group, item.key).toBe(item.group);
    }
  });

  it("gives a section with tabs its own first, and lights its entry for each", () => {
    expect(tabsOf("settings").map((t) => t.key)).toEqual(["settings", "servers", "remotes", "keys", "audit"]);
    expect(tabsOf("models")).toEqual([]);
    expect(menuEntry("keys")).toBe("settings");
    expect(menuEntry("settings")).toBe("settings");
    expect(menuEntry("jobs")).toBe("jobs");
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

  it("falls back to the overview", () => {
    expect(sectionFromHash("")).toBe("overview");
    expect(sectionFromHash("#code=abc")).toBe("overview");
    expect(sectionFromHash("#/hub")).toBe("overview"); // a section that is no more
  });

  it("links every admin section to its hash", () => {
    for (const item of NAV) {
      expect(sectionFromHash(new URL(item.href, "http://x").hash)).toBe(item.key);
    }
  });
});
