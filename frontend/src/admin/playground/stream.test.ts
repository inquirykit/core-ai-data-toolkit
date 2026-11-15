import { describe, expect, it } from "vitest";
import { sseParser } from "./stream";

describe("sseParser", () => {
  it("returns each event's data once the event is complete", () => {
    const parser = sseParser();
    expect(parser.push('data: {"a":1}\n\ndata: {"b"')).toEqual(['{"a":1}']);
    expect(parser.push(":2}\n")).toEqual([]);
    expect(parser.push("\ndata: [DONE]\n\n")).toEqual(['{"b":2}', "[DONE]"]);
  });

  it("ignores lines other than data", () => {
    expect(sseParser().push(": keep-alive\nevent: x\ndata: hi\n\n")).toEqual(["hi"]);
  });
});
