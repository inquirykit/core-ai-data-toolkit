import { describe, expect, it } from "vitest";
import type { Column, Entry } from "./chat";
import { fromSaved, hasUnsaved, lastReply, titleFor, toSaved, UNTITLED, type Conversation } from "./saved";

const reply = (id: number, content: string, extra: Partial<Entry> = {}): Entry =>
  ({ id, role: "assistant", content, started: 12, done: true, meta: { model: "m", ms: 300, completionTokens: 7 }, ...extra }) as Entry;

const settings = { system: "Be brief.", temperature: "0.3", maxTokens: "128" };

const saved: Conversation = {
  id: "c1",
  title: "Hi there",
  created: 1,
  updated: 2,
  settings: { system: "Be brief.", temperature: 0.3, max_tokens: 128 },
  columns: [
    {
      model: "a",
      log: [
        { role: "user", content: "hi", model: null },
        { role: "assistant", content: "hello", model: "a-old" },
        { role: "user", content: "more", model: null },
        { role: "assistant", content: "sure", model: null },
      ],
    },
    { model: "b", log: [{ role: "user", content: "hi", model: null }] },
  ],
};

describe("toSaved", () => {
  it("keeps the turns that count, with the answering model, and drops the meta", () => {
    const columns: Column[] = [
      {
        id: 1,
        model: "a",
        log: [
          { id: 1, role: "user", content: "hi" },
          reply(2, "hello", { meta: { model: "a-old", ms: 5 } } as Partial<Entry>),
          { id: 3, role: "user", content: "broken", failed: true },
          { id: 4, role: "error", content: "HTTP 500" },
          { id: 5, role: "user", content: "again" },
          reply(6, "half", { failed: true }),
          { id: 7, role: "pending", content: "", started: 0, done: false, meta: { model: "a" } },
        ],
      },
      { id: 2, model: "b", log: [] },
    ];
    expect(toSaved(columns, settings)).toEqual({
      settings: { system: "Be brief.", temperature: 0.3, max_tokens: 128 },
      columns: [
        {
          model: "a",
          log: [
            { role: "user", content: "hi" },
            { role: "assistant", content: "hello", model: "a-old" },
            { role: "user", content: "again" },
          ],
        },
        { model: "b", log: [] },
      ],
    });
  });
});

describe("fromSaved", () => {
  it("gives every column and entry its own id and finishes the replies", () => {
    let next = 10;
    const { columns, settings: back } = fromSaved(saved, () => next++);
    expect(back).toEqual(settings);
    expect(columns.map((c) => c.id)).toEqual([10, 15]);
    expect(columns[0].log.map((e) => e.id)).toEqual([11, 12, 13, 14]);
    expect(columns[0].log[0]).toEqual({ id: 11, role: "user", content: "hi" });
    expect(columns[0].log[1]).toEqual({ id: 12, role: "assistant", content: "hello", started: 0, done: true, meta: { model: "a-old" } });
    // A reply saved without its model answered as the column's.
    expect(columns[0].log[3]).toMatchObject({ role: "assistant", content: "sure", meta: { model: "a" } });
    expect(columns[1]).toEqual({ id: 15, model: "b", log: [{ id: 16, role: "user", content: "hi" }] });
  });

  it("round-trips through toSaved", () => {
    let next = 1;
    const back = fromSaved(saved, () => next++);
    expect(toSaved(back.columns, back.settings)).toEqual({
      settings: saved.settings,
      columns: [
        {
          model: "a",
          log: [
            { role: "user", content: "hi" },
            { role: "assistant", content: "hello", model: "a-old" },
            { role: "user", content: "more" },
            { role: "assistant", content: "sure", model: "a" },
          ],
        },
        { model: "b", log: [{ role: "user", content: "hi" }] },
      ],
    });
  });
});

describe("titleFor", () => {
  const column = (log: Entry[]): Column => ({ id: 1, model: "m", log });

  it("takes the first user message, on one line", () => {
    expect(titleFor([column([{ id: 1, role: "user", content: "  What is\n\n  a monad?  " }, reply(2, "...")])])).toBe("What is a monad?");
  });

  it("cuts a long message to 60 characters", () => {
    const long = "word ".repeat(30).trim();
    const title = titleFor([column([{ id: 1, role: "user", content: long }])]);
    expect(title.length).toBe(60);
    expect(title.endsWith("…")).toBe(true);
    expect(title.startsWith("word word")).toBe(true);
    expect(title).not.toContain(" …");
  });

  it("looks in the next column when the first has no user message, else falls back", () => {
    expect(titleFor([column([reply(1, "unprompted")]), column([{ id: 2, role: "user", content: "second" }])])).toBe("second");
    expect(titleFor([column([]), column([{ id: 1, role: "user", content: "   " }])])).toBe(UNTITLED);
    expect(titleFor([])).toBe(UNTITLED);
  });
});

describe("lastReply", () => {
  it("is the column's last assistant reply, or null", () => {
    expect(lastReply(saved, 0)).toBe("sure");
    expect(lastReply(saved, 1)).toBeNull();
    expect(lastReply(saved, 5)).toBeNull();
  });
});

describe("hasUnsaved", () => {
  const columns: Column[] = [{ id: 1, model: "a", log: [{ id: 1, role: "user", content: "hi" }, reply(2, "hello")] }];

  it("is false for an empty chat, or one matching the snapshot, whatever the settings", () => {
    expect(hasUnsaved([{ id: 1, model: "a", log: [] }], settings, "")).toBe(false);
    expect(hasUnsaved([{ id: 1, model: "a", log: [{ id: 1, role: "error", content: "x" }] }], settings, "")).toBe(false);
    const snapshot = JSON.stringify(toSaved(columns, settings).columns);
    expect(hasUnsaved(columns, { ...settings, temperature: "1.5" }, snapshot)).toBe(false);
  });

  it("is true once a turn is added", () => {
    const snapshot = JSON.stringify(toSaved(columns, settings).columns);
    const more: Column[] = [{ ...columns[0], log: [...columns[0].log, { id: 3, role: "user", content: "again" }] }];
    expect(hasUnsaved(more, settings, snapshot)).toBe(true);
    expect(hasUnsaved(columns, settings, "")).toBe(true);
  });
});
