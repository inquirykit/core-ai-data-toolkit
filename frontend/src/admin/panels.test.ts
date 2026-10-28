import { describe, expect, it } from "vitest";
import { limitsBody } from "./panels";

describe("limitsBody", () => {
  it("turns the fields into the request, or says what is wrong", () => {
    expect(limitsBody("6.5", "4")).toEqual({ max_memory_gb: 6.5, max_models: 4 });
    expect(limitsBody("0", "1")).toEqual({ max_memory_gb: 0, max_models: 1 });
    expect(limitsBody("", "3")).toMatch(/memory budget/);
    expect(limitsBody("-1", "3")).toMatch(/memory budget/);
    expect(limitsBody("8", "2.5")).toMatch(/Max models/);
    expect(limitsBody("8", "0")).toMatch(/Max models/);
  });
});
