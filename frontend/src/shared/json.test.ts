import { describe, expect, it } from "vitest";

import { jsonLinesProblems, parseJsonObject } from "./json";

describe("parseJsonObject", () => {
  it("reads an object, says what is wrong otherwise, and lets blank stand for a default", () => {
    expect(parseJsonObject('{"a": 1}')).toEqual({ ok: true, value: { a: 1 } });
    expect(parseJsonObject("[1]", { what: "The body", hint: "like {}" })).toEqual({ ok: false, error: "The body is a JSON object, like {}" });
    expect(parseJsonObject("  ", { emptyMeans: {} })).toEqual({ ok: true, value: {} });
    expect(parseJsonObject("")).toMatchObject({ ok: false, error: "The input is required" });
  });
});

describe("jsonLinesProblems", () => {
  it("names each line that is not JSON, where it is, skipping blank ones", () => {
    const text = '{"a": 1}\n\n{oops}\n[2]\nnot json';
    const problems = jsonLinesProblems(text);
    expect(problems.map((p) => p.message.split(":")[0])).toEqual(["Line 3", "Line 5"]);
    expect(text.slice(problems[0].from, problems[0].to)).toBe("{oops}");
    expect(text.slice(problems[1].from, problems[1].to)).toBe("not json");
    expect(jsonLinesProblems('{"a": 1}\n')).toEqual([]);
  });
});
