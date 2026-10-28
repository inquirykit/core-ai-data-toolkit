import { describe, expect, it } from "vitest";

import { report } from "./action";
import { AuthError } from "./api";

const ok = <R,>(result: R) => ({ ok: true as const, result });
const failed = (error: unknown) => ({ ok: false as const, error });

describe("report", () => {
  it("toasts a failure under the title given, or as the title; an AuthError is told to nobody", () => {
    expect(report({ failed: "Could not save" }, [], failed(new Error("down")))).toEqual({
      toast: { kind: "error", title: "Could not save", description: "down" },
    });
    expect(report({ failed: (name: string) => `Could not save ${name}` }, ["x"], failed(new Error("down")))).toEqual({
      toast: { kind: "error", title: "Could not save x", description: "down" },
    });
    expect(report({}, [], failed(new Error("gone")))).toEqual({ toast: { kind: "error", title: "gone", description: undefined } });
    expect(report({ failed: "Could not save" }, [], failed(new AuthError("Not signed in")))).toEqual({});
  });

  it("keeps a failure under the form when asked", () => {
    expect(report({ notice: true }, [], failed(new Error("down")))).toEqual({ notice: { text: "down", kind: "error" } });
    expect(report({ notice: true }, [], failed(new AuthError("Not signed in")))).toEqual({});
  });

  it("says what worked: a text is toasted, made from the reply and the arguments; a notice goes under the form", () => {
    expect(report({ success: (r: { id: string }, name: string) => `Saved ${name} as ${r.id}.` }, ["x"], ok({ id: "1" }))).toEqual({
      toast: { kind: "success", title: "Saved x as 1." },
    });
    expect(report({ success: "Done." }, [], ok(undefined))).toEqual({ toast: { kind: "success", title: "Done." } });
    expect(report({ success: (r: { id: string }) => ({ text: `${r.id} added`, kind: "ok" as const }) }, [], ok({ id: "3" }))).toEqual({
      notice: { text: "3 added", kind: "ok" },
    });
    expect(report({ success: () => undefined }, [], ok(1))).toEqual({});
    expect(report({}, [], ok(1))).toEqual({});
  });
});
