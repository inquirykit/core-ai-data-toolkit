import { describe, expect, it } from "vitest";
import { numberProblem, numberRange, property, type JSONSchema } from "./schema";

// The shapes pydantic writes (models/tasks.py, as GET /tasks serves them).
const generation: JSONSchema = {
  properties: {
    text: { type: "string", minLength: 1, maxLength: 20000, title: "Text" },
    max_length: { anyOf: [{ type: "integer", minimum: 1, maximum: 1024 }, { type: "null" }], default: null, title: "Max Length" },
    temperature: { type: "number", exclusiveMinimum: 0, maximum: 5, default: 0.7, title: "Temperature" },
    do_sample: { type: "boolean", default: true, title: "Do Sample" },
    messages: { anyOf: [{ type: "array", items: { $ref: "#/$defs/ChatTurn" } }, { type: "null" }], default: null },
    mood: { $ref: "#/$defs/Mood", default: "calm" },
    tags: { type: "array", items: { type: "string" } },
  },
  required: ["text"],
  $defs: { ChatTurn: { type: "object" }, Mood: { type: "string", enum: ["calm", "loud"], title: "Mood" } },
};

describe("property", () => {
  it("narrows optional fields and follows refs, keeping the outer default", () => {
    expect(property(generation, "max_length")).toMatchObject({ type: "integer", maximum: 1024, default: null });
    expect(property(generation, "mood")).toMatchObject({ enum: ["calm", "loud"], default: "calm" });
    expect(property(generation, "nothing")).toBeNull();
    expect(property(null, "text")).toBeNull();
  });
});

describe("numbers", () => {
  it("turns bounds into input attributes", () => {
    expect(numberRange(property(generation, "max_length"))).toEqual({ min: 1, max: 1024, step: 1 });
    expect(numberRange(property(generation, "temperature"))).toEqual({ min: 0.01, max: 5, step: "any" });
  });

  it("says what breaks the bounds", () => {
    const temperature = property(generation, "temperature");
    expect(numberProblem("Temperature", 0, temperature)).toMatch(/above 0/);
    expect(numberProblem("Temperature", 6, temperature)).toMatch(/at most 5/);
    expect(numberProblem("Temperature", 0.5, temperature)).toBe("");
    expect(numberProblem("Max", 2.5, property(generation, "max_length"))).toMatch(/whole number/);
    expect(numberProblem("Max", NaN, null)).toMatch(/must be a number/);
  });
});
