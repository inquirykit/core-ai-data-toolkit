// A column's conversation: what it shows, and what is sent to the model.
import type { Limits } from "../shared/types";
import type { ChatChunk } from "./stream";

export interface ReplyMeta {
  /** The model the column had when the reply was asked for. */
  model: string;
  /** Total time, once done. */
  ms?: number;
  /** Time to the first piece of text. */
  firstMs?: number;
  /** The server's own time (from the reply's toolkit field). */
  serverMs?: number;
  /** This request loaded the model first. */
  cold?: boolean;
  /** The user stopped it part way. */
  stopped?: boolean;
  /** Token counts from the reply's usage, when the model reports them. */
  promptTokens?: number;
  completionTokens?: number;
}

/** Generated tokens per second, from the time after the first token arrived; null when unknown or too short to tell. */
export function tokensPerSecond(meta: ReplyMeta): number | null {
  if (meta.completionTokens == null || meta.ms == null || meta.completionTokens < 2) return null;
  const generating = meta.ms - (meta.firstMs ?? 0);
  if (generating < 50) return null;
  return ((meta.completionTokens - 1) * 1000) / generating;
}

export type Entry =
  | { id: number; role: "user"; content: string; failed?: boolean }
  /** "pending" until the first text arrives, then "assistant". */
  | { id: number; role: "pending" | "assistant"; content: string; started: number; done: boolean; failed?: boolean; meta: ReplyMeta }
  | { id: number; role: "error"; content: string };

export type Reply = Extract<Entry, { role: "pending" | "assistant" }>;

export interface Column {
  id: number;
  model: string;
  log: Entry[];
}

/** The settings form's values, as typed. */
export interface Settings {
  system: string;
  temperature: string;
  maxTokens: string;
}

/**
 * What the model is sent: the system prompt, then the column's own turns
 * except failed ones (so the next turn does not pile up on them). A reply the
 * user stopped stays in, as far as it got.
 */
export function messagesFor(log: Entry[], system: string): { role: string; content: string }[] {
  const messages = [];
  if (system.trim()) messages.push({ role: "system", content: system.trim() });
  for (const entry of log) {
    if ((entry.role === "user" || entry.role === "assistant") && !entry.failed) {
      messages.push({ role: entry.role, content: entry.content });
    }
  }
  return messages;
}

/** Why the settings cannot be sent (or saved as a preset), or null when they can. */
export function settingsProblem(settings: Settings, limits: Limits): string | null {
  const temperature = Number(settings.temperature);
  const maxTokens = Number(settings.maxTokens);
  if (!(temperature >= 0 && temperature <= 2)) return "Temperature must be between 0 and 2.";
  if (!(maxTokens >= 1 && maxTokens <= limits.max_generation_length)) {
    return `Max new tokens must be between 1 and ${limits.max_generation_length}.`;
  }
  return null;
}

/** Column letters: A, B, C… */
export const letter = (index: number): string => String.fromCharCode(65 + index);

// ---------------------------------------------------------------- a turn
/** The ids of a turn's two entries, the message and its reply, minted by the view when the turn starts. */
export interface Turn {
  userId: number;
  replyId: number;
}

/** What a streamed chunk adds to the reply: text, and the server's figures when it carries them (the last chunk does). */
export interface Delta {
  text: string;
  meta?: Partial<Pick<ReplyMeta, "serverMs" | "cold" | "promptTokens" | "completionTokens">>;
}

export function chunkDelta(chunk: ChatChunk): Delta {
  const delta: Delta = { text: chunk.choices?.[0]?.delta?.content ?? "" };
  if (chunk.toolkit) delta.meta = { serverMs: chunk.toolkit.processing_ms, cold: chunk.toolkit.cold_start };
  if (chunk.usage) delta.meta = { ...delta.meta, promptTokens: chunk.usage.prompt_tokens, completionTokens: chunk.usage.completion_tokens };
  return delta;
}

/** What happens to a column's log over a turn; ``at`` is performance.now() when it did, the clock the reply's ``started`` is on. */
export type ChatEvent =
  | { type: "started"; turn: Turn; text: string; model: string; at: number }
  | ({ type: "delta"; turn: Turn; at: number } & Delta)
  | { type: "finished"; turn: Turn; at: number }
  /** Stopped by the user: the reply stays as far as it got, or the turn failed when nothing had come. */
  | { type: "cancelled"; turn: Turn; at: number }
  /** ``errorId`` is for the error line under a reply that had started; a reply with nothing in it becomes the error. */
  | { type: "failed"; turn: Turn; message: string; errorId: number; at: number };

export const STOPPED_EARLY = "Stopped before any text arrived.";

/**
 * The log after ``event``. A turn that fails marks its message, so that it is
 * not sent again (messagesFor), and what came of the reply stays on show but
 * out of the history. An event for a reply the log does not have changes nothing.
 */
export function reduceLog(log: Entry[], event: ChatEvent): Entry[] {
  const { turn, at } = event;
  if (event.type === "started") {
    const reply: Reply = { id: turn.replyId, role: "pending", content: "", started: at, done: false, meta: { model: event.model } };
    return [...log, { id: turn.userId, role: "user", content: event.text }, reply];
  }
  const reply = log.find((e): e is Reply => e.id === turn.replyId && (e.role === "pending" || e.role === "assistant"));
  if (!reply) return log;
  const replace = (entries: Entry[], id: number, entry: Entry) => entries.map((e) => (e.id === id ? entry : e));
  const markMessage = (entries: Entry[]) => entries.map((e) => (e.id === turn.userId && e.role === "user" ? { ...e, failed: true } : e));
  /** The reply as it ends: an assistant turn, timed. */
  const ended = (how: { failed?: boolean; stopped?: boolean } = {}): Reply => ({
    ...reply,
    role: "assistant",
    done: true,
    ...(how.failed ? { failed: true } : {}),
    meta: { ...reply.meta, ms: at - reply.started, ...(how.stopped ? { stopped: true } : {}) },
  });
  switch (event.type) {
    case "delta": {
      if (!event.text && !event.meta) return log;
      const first = event.text !== "" && reply.role === "pending";
      return replace(log, reply.id, {
        ...reply,
        role: first ? "assistant" : reply.role,
        content: reply.content + event.text,
        meta: { ...reply.meta, ...(first ? { firstMs: at - reply.started } : {}), ...event.meta },
      });
    }
    case "finished":
      return replace(log, reply.id, ended());
    case "cancelled":
      if (reply.content) return replace(log, reply.id, ended({ stopped: true }));
      return replace(markMessage(log), reply.id, { id: reply.id, role: "error", content: STOPPED_EARLY });
    case "failed": {
      const error: Entry = { id: reply.content ? event.errorId : reply.id, role: "error", content: event.message };
      if (!reply.content) return replace(markMessage(log), reply.id, error);
      return [...replace(markMessage(log), reply.id, ended({ failed: true })), error];
    }
  }
}
