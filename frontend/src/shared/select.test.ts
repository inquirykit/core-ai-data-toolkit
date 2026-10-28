import { describe, expect, it } from "vitest";
import { EMPTY_ITEM, fromItem, toItem } from "./select";

describe("the select's empty value", () => {
  it("travels as an item Radix accepts and comes back empty", () => {
    expect(toItem("")).toBe(EMPTY_ITEM);
    expect(fromItem(toItem(""))).toBe("");
    expect(EMPTY_ITEM).not.toBe("");
  });

  it("leaves every other value alone", () => {
    for (const value of ["detect", "none", "all", "DEFAULT", "x"]) {
      expect(toItem(value)).toBe(value);
      expect(fromItem(value)).toBe(value);
    }
  });
});
