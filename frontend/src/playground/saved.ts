// Saved conversations and prompt presets (/playground/conversations and
// /playground/presets): the server's shapes, and the steps between them and
// the chat's own state.
import type { Schemas } from "../shared/api";
import type { Column, Entry, Settings } from "./chat";

export type ConversationSummary = Schemas["ConversationSummary"];
export type SavedSettings = Schemas["SavedSettings-Input"];
/** A turn as stored: assistant entries carry the model that answered. */
export type SavedEntry = Schemas["SavedEntry-Input"];
export type SavedColumn = Schemas["SavedColumn-Input"];
/** What POST and PUT /playground/conversations take. */
export type ConversationBody = Schemas["ConversationBody"];
export type Conversation = Schemas["Conversation"];
export type Preset = Schemas["Preset"];
/** What POST and PUT /playground/presets take. */
export type PresetBody = Schemas["PresetBody"];
export type DatasetSummary = Pick<Schemas["Dataset"], "id" | "name" | "description" | "examples">;
/** POST /playground/conversations/{id}/example. */
export type ExampleBody = Schemas["ExampleBody"];

const TITLE_LENGTH = 60;
export const UNTITLED = "Untitled chat";

/**
 * The chat as the server stores it: the settings as numbers and, per column,
 * the turns that count, like messagesFor sends them (no pending, failed or
 * error entries) and without the timing meta. The title is the caller's.
 */
export function toSaved(columns: Column[], settings: Settings): Omit<ConversationBody, "title"> {
  return {
    settings: {
      system: settings.system,
      temperature: Number(settings.temperature),
      max_tokens: Number(settings.maxTokens),
    },
    columns: columns.map((column) => ({
      model: column.model,
      log: column.log.flatMap((entry): SavedEntry[] => {
        if (entry.role === "pending" || entry.role === "error" || entry.failed) return [];
        if (entry.role === "user") return [{ role: "user", content: entry.content }];
        return [{ role: "assistant", content: entry.content, model: entry.meta.model }];
      }),
    })),
  };
}

/**
 * A saved conversation as the chat shows it, with ids from ``newId``. Replies
 * come back finished and without timings; one saved without its model is
 * taken to be from the column's.
 */
export function fromSaved(conversation: Conversation, newId: () => number): { columns: Column[]; settings: Settings } {
  return {
    settings: {
      system: conversation.settings.system,
      temperature: String(conversation.settings.temperature),
      maxTokens: String(conversation.settings.max_tokens),
    },
    columns: conversation.columns.map((column) => ({
      id: newId(),
      model: column.model,
      log: column.log.map(
        (entry): Entry =>
          entry.role === "user"
            ? { id: newId(), role: "user", content: entry.content }
            : { id: newId(), role: "assistant", content: entry.content, started: 0, done: true, meta: { model: entry.model ?? column.model } },
      ),
    })),
  };
}

/** A title from the first user message (the columns share their turns), on one line and at most 60 characters. */
export function titleFor(columns: Column[]): string {
  for (const column of columns) {
    const first = column.log.find((entry) => entry.role === "user" && entry.content.trim());
    if (!first) continue;
    const text = first.content.replace(/\s+/g, " ").trim();
    return text.length > TITLE_LENGTH ? `${text.slice(0, TITLE_LENGTH - 1).trimEnd()}…` : text;
  }
  return UNTITLED;
}

/** The last assistant reply in one column of a saved conversation; null when it has none. */
export function lastReply(conversation: Conversation, column: number): string | null {
  const log = conversation.columns[column]?.log ?? [];
  for (let i = log.length - 1; i >= 0; i--) {
    if (log[i].role === "assistant") return log[i].content;
  }
  return null;
}

/** Whether the chat has turns the saved copy (``snapshot``, from JSON.stringify of toSaved's columns) does not. */
export function hasUnsaved(columns: Column[], settings: Settings, snapshot: string): boolean {
  if (!columns.some((c) => c.log.some((e) => e.role === "user" || e.role === "assistant"))) return false;
  return JSON.stringify(toSaved(columns, settings).columns) !== snapshot;
}
