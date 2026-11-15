import { describe, expect, it } from "vitest";
import { chunkDelta, letter, messagesFor, reduceLog, STOPPED_EARLY, tokensPerSecond, type Entry, type Turn } from "./chat";

const reply = (id: number, content: string, extra: Partial<Entry> = {}): Entry =>
  ({ id, role: "assistant", content, started: 0, done: true, meta: { model: "m" }, ...extra }) as Entry;

describe("messagesFor", () => {
  it("sends the system prompt and the turns, leaving out failed ones and errors", () => {
    const log: Entry[] = [
      { id: 1, role: "user", content: "hi" },
      reply(2, "hello"),
      { id: 3, role: "user", content: "broken", failed: true },
      { id: 4, role: "error", content: "HTTP 500" },
      { id: 5, role: "user", content: "again" },
      reply(6, "half", { failed: true }),
    ];
    expect(messagesFor(log, "  Be brief. ")).toEqual([
      { role: "system", content: "Be brief." },
      { role: "user", content: "hi" },
      { role: "assistant", content: "hello" },
      { role: "user", content: "again" },
    ]);
  });

  it("leaves out an empty system prompt", () => {
    expect(messagesFor([{ id: 1, role: "user", content: "hi" }], " ")).toEqual([{ role: "user", content: "hi" }]);
  });

  it("letters columns", () => {
    expect([0, 1, 2].map(letter)).toEqual(["A", "B", "C"]);
  });
});

describe("tokensPerSecond", () => {
  it("uses the time after the first token", () => {
    expect(tokensPerSecond({ model: "m", ms: 1100, firstMs: 100, completionTokens: 11 })).toBeCloseTo(10);
  });
  it("is unknown without counts, timing, or enough tokens", () => {
    expect(tokensPerSecond({ model: "m", ms: 500 })).toBeNull();
    expect(tokensPerSecond({ model: "m", completionTokens: 5 })).toBeNull();
    expect(tokensPerSecond({ model: "m", ms: 500, completionTokens: 1 })).toBeNull();
    expect(tokensPerSecond({ model: "m", ms: 20, firstMs: 10, completionTokens: 5 })).toBeNull();
  });
});

describe("reduceLog", () => {
  const turn: Turn = { userId: 10, replyId: 11 };
  const before: Entry[] = [{ id: 1, role: "user", content: "hi" }, reply(2, "hello")];
  const started = reduceLog(before, { type: "started", turn, text: "and?", model: "m", at: 1000 });
  const user: Entry = { id: 10, role: "user", content: "and?" };

  it("starts with the message and a pending reply, leaving what was there", () => {
    expect(started).toEqual([...before, user, { id: 11, role: "pending", content: "", started: 1000, done: false, meta: { model: "m" } }]);
  });

  it("turns the reply into an assistant's on the first text, timing it, and adds the text and figures after", () => {
    const first = reduceLog(started, { type: "delta", turn, text: "Wel", at: 1300 });
    expect(first.at(-1)).toEqual({ id: 11, role: "assistant", content: "Wel", started: 1000, done: false, meta: { model: "m", firstMs: 300 } });
    const second = reduceLog(first, { type: "delta", turn, text: "l.", meta: { serverMs: 250, cold: true, promptTokens: 7, completionTokens: 2 }, at: 1400 });
    expect(second.at(-1)).toEqual({
      id: 11,
      role: "assistant",
      content: "Well.",
      started: 1000,
      done: false,
      meta: { model: "m", firstMs: 300, serverMs: 250, cold: true, promptTokens: 7, completionTokens: 2 },
    });
    const finished = reduceLog(second, { type: "finished", turn, at: 2000 });
    expect(finished.at(-1)).toMatchObject({ role: "assistant", done: true, meta: { ms: 1000, firstMs: 300 } });
    expect(finished).toHaveLength(4);
  });

  it("finishes an empty reply as one, still timed", () => {
    expect(reduceLog(started, { type: "finished", turn, at: 1500 }).at(-1)).toEqual({
      id: 11,
      role: "assistant",
      content: "",
      started: 1000,
      done: true,
      meta: { model: "m", ms: 500 },
    });
  });

  it("keeps a stopped reply as far as it got, and makes an empty one the failure", () => {
    const some = reduceLog(started, { type: "delta", turn, text: "Wel", at: 1300 });
    const stopped = reduceLog(some, { type: "cancelled", turn, at: 1600 });
    expect(stopped.slice(2)).toEqual([user, { id: 11, role: "assistant", content: "Wel", started: 1000, done: true, meta: { model: "m", firstMs: 300, ms: 600, stopped: true } }]);
    const early = reduceLog(started, { type: "cancelled", turn, at: 1600 });
    expect(early.slice(2)).toEqual([{ ...user, failed: true }, { id: 11, role: "error", content: STOPPED_EARLY }]);
  });

  it("marks the message failed; a reply with nothing in it becomes the error, one part way keeps its text under an error line", () => {
    const early = reduceLog(started, { type: "failed", turn, message: "HTTP 500", errorId: 12, at: 1600 });
    expect(early.slice(2)).toEqual([{ ...user, failed: true }, { id: 11, role: "error", content: "HTTP 500" }]);
    const some = reduceLog(started, { type: "delta", turn, text: "Wel", at: 1300 });
    const late = reduceLog(some, { type: "failed", turn, message: "cut off", errorId: 12, at: 1600 });
    expect(late.slice(2)).toEqual([
      { ...user, failed: true },
      { id: 11, role: "assistant", content: "Wel", started: 1000, done: true, failed: true, meta: { model: "m", firstMs: 300, ms: 600 } },
      { id: 12, role: "error", content: "cut off" },
    ]);
    // Out of the history, like any failed turn.
    expect(messagesFor(late, "")).toEqual([
      { role: "user", content: "hi" },
      { role: "assistant", content: "hello" },
    ]);
  });

  it("changes nothing for a chunk with nothing in it, or a reply it does not know", () => {
    expect(reduceLog(started, { type: "delta", turn, text: "", at: 1300 })).toBe(started);
    expect(reduceLog(before, { type: "finished", turn, at: 1300 })).toBe(before);
  });
});

describe("chunkDelta", () => {
  it("takes the text, and the figures only when the chunk has them", () => {
    expect(chunkDelta({ choices: [{ delta: { content: "a" } }] })).toEqual({ text: "a" });
    expect(chunkDelta({})).toEqual({ text: "" });
    expect(chunkDelta({ toolkit: { processing_ms: 5, cold_start: false }, usage: { prompt_tokens: 1, completion_tokens: 2, total_tokens: 3 } })).toEqual({
      text: "",
      meta: { serverMs: 5, cold: false, promptTokens: 1, completionTokens: 2 },
    });
  });
});
