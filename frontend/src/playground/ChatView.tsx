import { useEffect, useLayoutEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { ArrowUp, Bookmark, MessageSquarePlus, Plus, Save, Square, X } from "lucide-react";
import { motion } from "motion/react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import { useAction } from "../shared/action";
import { AuthError, type ApiClient } from "../shared/api";
import { CHAT_TASKS, type CatalogModel } from "../shared/model-browser/catalog";
import { Field, Notice, type NoticeState } from "../shared/components";
import { failure } from "../shared/errors";
import { fmtMs } from "../shared/format";
import { readLocal, writeLocal } from "../shared/storage";
import type { Limits } from "../shared/types";
import { chunkDelta, letter, messagesFor, reduceLog, settingsProblem, tokensPerSecond, type ChatEvent, type Column, type Entry, type Settings, type Turn } from "./chat";
import { ModelPicker } from "./ModelPicker";
import { PresetPicker } from "./Presets";
import { fromSaved, hasUnsaved, titleFor, toSaved, type Conversation } from "./saved";
import { SavedDialog } from "./SavedChats";
import { streamChat } from "./stream";

const PREFS_STORAGE = "ai-toolkit-playground";

/** Settings and model choices only; conversations are stored on the server, and only when asked (Save). */
interface Prefs extends Settings {
  models: string[];
}

/**
 * The Chat tab: one conversation per column, each with its own model, all
 * sent the same turns so their replies can be compared.
 */
export function ChatView(props: {
  client: ApiClient;
  /** The same key, but a 401 does not bring up the sign-in form: for admin-only calls (datasets). */
  quiet: ApiClient;
  limits: Limits;
  /** The chat models to offer, loaded first (see modelsOf). */
  models: CatalogModel[];
  /** A model to put in column A, from /playground?model=ID. */
  linked: string | null;
}) {
  const { client, limits, models } = props;
  const nextId = useRef(1);
  const newId = () => nextId.current++;
  const [settings, setSettings] = useState<Settings>(() => {
    const prefs = readLocal<Prefs>(PREFS_STORAGE);
    return {
      system: prefs.system ?? "",
      temperature: prefs.temperature ?? "0.7",
      maxTokens: String(Math.min(Number(prefs.maxTokens) || 256, limits.max_generation_length)),
    };
  });
  const [columns, setColumns] = useState<Column[]>(() => {
    const chosen = (readLocal<Prefs>(PREFS_STORAGE).models ?? []).slice(0, Math.max(1, limits.max_models));
    if (!chosen.length) chosen.push(models[0]?.id ?? "");
    if (props.linked) chosen[0] = props.linked;
    return chosen.map((model) => ({ id: newId(), model, log: [] }));
  });
  const [input, setInput] = useState("");
  const [notice, setNotice] = useState<NoticeState>({ text: "" });
  const controller = useRef<AbortController | null>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const [focusColumn, setFocusColumn] = useState<number | null>(null);
  // The preset the settings came from, until they are edited.
  const [presetId, setPresetId] = useState<string | null>(null);
  // The saved conversation the chat came from (or was saved as), so Save updates it.
  const [loaded, setLoaded] = useState<{ id: string; title: string } | null>(null);
  // The columns as last saved or loaded, to tell whether Load would lose turns.
  const [snapshot, setSnapshot] = useState("");
  const [savedOpen, setSavedOpen] = useState(false);

  useEffect(() => inputRef.current?.focus(), []);

  const clampTokens = (n: number) => String(Math.min(Math.max(1, n || 1), limits.max_generation_length));

  // Remember the settings and models.
  const columnModels = columns.map((c) => c.model).join("\n");
  useEffect(() => {
    writeLocal(PREFS_STORAGE, { ...settings, models: columns.map((c) => c.model) } satisfies Prefs);
    // columnModels stands in for columns: only the model choices are saved, not every streamed piece.
  }, [settings, columnModels]);

  const updateLog = (columnId: number, update: (log: Entry[]) => Entry[]) =>
    setColumns((cols) => cols.map((c) => (c.id === columnId ? { ...c, log: update(c.log) } : c)));

  /** Sends ``text`` to one column's model and follows the reply into its log, as events the log reduces (chat.ts). */
  async function ask(column: Column, text: string, temperature: number, maxTokens: number, signal: AbortSignal) {
    const turn: Turn = { userId: newId(), replyId: newId() };
    const dispatch = (event: ChatEvent) => updateLog(column.id, (log) => reduceLog(log, event));
    const messages = messagesFor([...column.log, { id: turn.userId, role: "user", content: text }], settings.system);
    dispatch({ type: "started", turn, text, model: column.model, at: performance.now() });
    try {
      await streamChat(client, { model: column.model, messages, temperature, max_tokens: maxTokens }, signal, (chunk) =>
        dispatch({ type: "delta", turn, ...chunkDelta(chunk), at: performance.now() }),
      );
      dispatch({ type: "finished", turn, at: performance.now() });
    } catch (error) {
      if ((error as Error).name === "AbortError") dispatch({ type: "cancelled", turn, at: performance.now() });
      else {
        const message = error instanceof AuthError ? "Not signed in." : (error as Error).message;
        dispatch({ type: "failed", turn, message, errorId: newId(), at: performance.now() });
      }
    }
  }

  /** One turn: the message sent to every column, until the last reply is in or they are stopped. Nothing to toast: ask() tells the log. */
  const turn = useAction(async (text: string, temperature: number, maxTokens: number) => {
    controller.current = new AbortController();
    const { signal } = controller.current;
    // Every column gets the turn at once. The server runs different models in
    // parallel, except on Apple GPUs (MPS), where they take turns.
    await Promise.all(columns.map((column) => ask(column, text, temperature, maxTokens, signal)));
  });
  const busy = turn.busy;

  async function send(event: FormEvent) {
    event.preventDefault();
    if (busy) {
      // The button reads "Stop" while replies are coming in.
      controller.current?.abort();
      return;
    }
    const text = input.trim();
    if (!text) return;
    const unnamed = columns.findIndex((c) => !c.model);
    if (unnamed >= 0) {
      setNotice({ text: `Choose a model for column ${letter(unnamed)} first.`, kind: "error" });
      return;
    }
    const problem = settingsProblem(settings, limits);
    if (problem) {
      setNotice({ text: problem, kind: "error" });
      return;
    }
    clearNotice();
    setInput("");
    await turn.run(text, Number(settings.temperature), Number(settings.maxTokens));
    inputRef.current?.focus();
  }

  const onInputKey = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      // Enter only sends; stopping takes the button.
      if (!busy) event.currentTarget.form?.requestSubmit();
    }
  };

  const maxColumns = Math.max(1, limits.max_models);
  const atMax = columns.length >= maxColumns;
  const setting = (name: keyof Settings) => (e: { target: { value: string } }) => {
    setSettings((s) => ({ ...s, [name]: e.target.value }));
    // Edited by hand: no longer the preset's.
    setPresetId(null);
  };

  const hasTurns = columns.some((c) => c.log.some((e) => e.role === "user" || e.role === "assistant"));

  /** Stores the chat: over the conversation it came from, else as a new one named after the first message. */
  const save = useAction(
    async () => {
      const problem = settingsProblem(settings, limits);
      if (problem) throw new Error(problem);
      const body = { title: loaded?.title ?? titleFor(columns), ...toSaved(columns, settings) };
      const saved = loaded
        ? await client.put("/playground/conversations/{conversation_id}", { params: { path: { conversation_id: loaded.id } }, body })
        : await client.post("/playground/conversations", { body });
      setLoaded({ id: saved.id, title: saved.title });
      setSnapshot(JSON.stringify(body.columns));
      return saved;
    },
    { notice: true, success: (saved) => `Saved "${saved.title}".` },
  );
  // The line under the input: Save's notice when it has one, else the chat's own (a column without a model, say).
  const clearNotice = () => {
    setNotice({ text: "" });
    save.clear();
  };
  /** A failure outside the hook's reach (the presets' list or delete) goes on the same line. */
  const fail = (e: unknown) => {
    const text = failure(e);
    if (text !== null) setNotice({ text, kind: "error" });
  };

  /** Replaces the chat with a saved conversation. */
  function loadChat(conversation: Conversation) {
    const next = fromSaved(conversation, newId);
    // One saved with no columns still needs a column to type into.
    if (!next.columns.length) next.columns.push({ id: newId(), model: columns[0]?.model ?? "", log: [] });
    setColumns(next.columns);
    setSettings({ ...next.settings, maxTokens: clampTokens(Number(next.settings.maxTokens)) });
    setPresetId(null);
    setLoaded({ id: conversation.id, title: conversation.title });
    setSnapshot(JSON.stringify(toSaved(next.columns, next.settings).columns));
    clearNotice();
    inputRef.current?.focus();
  }

  const addLabel = atMax
    ? `The model cache holds ${maxColumns} model${maxColumns === 1 ? "" : "s"} (MAX_MODELS); more columns would reload models every turn`
    : "";
  const addButton = (
    <Button
      type="button"
      variant="outline"
      disabled={busy || atMax}
      onClick={() => {
        // A new column joins with an empty history; "New chat" lines them all up again.
        const id = newId();
        setColumns((cols) => [...cols, { id, model: "", log: [] }]);
        setFocusColumn(id);
      }}
    >
      <Plus aria-hidden="true" />
      Add model to compare
    </Button>
  );


  return (
    <>
      <Card role="region" aria-label="Settings" className="py-4">
        <CardContent className="flex flex-wrap items-end gap-3 px-4">
          <Field label="System prompt (optional)" className="flex-[1_1_320px]">
            <Textarea
              rows={1}
              className="min-h-9"
              placeholder="You are a helpful assistant."
              value={settings.system}
              onChange={setting("system")}
            />
          </Field>
          <PresetPicker
            client={client}
            limits={limits}
            settings={settings}
            selected={presetId}
            onPick={(preset) => {
              setSettings({ system: preset.system, temperature: String(preset.temperature), maxTokens: clampTokens(preset.max_tokens) });
              setPresetId(preset.id);
            }}
            onSaved={(preset) => setPresetId(preset.id)}
            onDeleted={() => setPresetId(null)}
            onError={fail}
            disabled={busy}
          />
          <Field label="Temperature">
            <Input
              type="number"
              className="w-[100px]"
              min={0}
              max={2}
              step={0.1}
              value={settings.temperature}
              onChange={setting("temperature")}
            />
          </Field>
          <Field label="Max new tokens">
            <Input
              type="number"
              className="w-[100px]"
              min={1}
              max={limits.max_generation_length}
              step={1}
              value={settings.maxTokens}
              onChange={setting("maxTokens")}
            />
          </Field>
          <div className="ml-auto flex flex-wrap gap-2">
            {atMax ? (
              // A disabled button shows no tooltip of its own: a focusable wrapper carries the reason.
              <Tooltip>
                <TooltipTrigger asChild>
                  <span tabIndex={0} className="inline-flex rounded-md outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50">
                    {addButton}
                  </span>
                </TooltipTrigger>
                <TooltipContent className="max-w-xs">{addLabel}</TooltipContent>
              </Tooltip>
            ) : (
              addButton
            )}
            <Button
              type="button"
              variant="outline"
              disabled={busy}
              onClick={() => {
                setColumns((cols) => cols.map((c) => ({ ...c, log: [] })));
                // A fresh chat: Save will store it as a new conversation.
                setLoaded(null);
                setSnapshot("");
                clearNotice();
                inputRef.current?.focus();
              }}
            >
              <MessageSquarePlus aria-hidden="true" />
              New chat
            </Button>
            <Button
              type="button"
              variant="outline"
              disabled={busy || save.busy || !hasTurns}
              title={loaded ? `Save over "${loaded.title}"` : "Save this chat on the server"}
              onClick={save.run}
            >
              <Save aria-hidden="true" />
              Save
            </Button>
            <Button type="button" variant="outline" disabled={busy} onClick={() => setSavedOpen(true)}>
              <Bookmark aria-hidden="true" />
              Saved…
            </Button>
          </div>
        </CardContent>
      </Card>

      <SavedDialog
        client={client}
        quiet={props.quiet}
        open={savedOpen}
        onClose={() => setSavedOpen(false)}
        loadedId={loaded?.id ?? null}
        unsaved={() => hasUnsaved(columns, settings, snapshot)}
        onLoad={loadChat}
        onRenamed={(c) => setLoaded((l) => (l?.id === c.id ? { id: c.id, title: c.title } : l))}
        onDeleted={(id) => {
          // The chat stays; its next Save makes a new conversation.
          setLoaded((l) => (l?.id === id ? null : l));
          setSnapshot((s) => (loaded?.id === id ? "" : s));
        }}
      />

      <section
        className="grid grid-cols-[repeat(auto-fit,minmax(min(100%,300px),1fr))] items-stretch gap-4"
        aria-label="Conversations"
      >
        {columns.map((column, index) => (
          <ColumnCard
            key={column.id}
            client={client}
            models={models}
            column={column}
            index={index}
            busy={busy}
            autoFocus={column.id === focusColumn}
            onModel={(model) => setColumns((cols) => cols.map((c) => (c.id === column.id ? { ...c, model } : c)))}
            onRemove={columns.length > 1 ? () => setColumns((cols) => cols.filter((c) => c.id !== column.id)) : undefined}
          />
        ))}
      </section>

      {/* Stays at the bottom of the page while it scrolls, over a fade into the page. */}
      <section className="sticky bottom-0 z-10 bg-linear-to-t from-background from-70% to-transparent pt-3 pb-5">
        <form
          className="flex items-end gap-2 rounded-2xl border bg-card p-2 shadow-md transition-[border-color,box-shadow] focus-within:border-ring focus-within:ring-[3px] focus-within:ring-ring/50"
          onSubmit={send}
        >
          <Textarea
            ref={inputRef}
            rows={2}
            className="max-h-[40vh] min-h-10 flex-1 resize-none border-0 bg-transparent px-2 py-[9px] shadow-none focus-visible:ring-0 dark:bg-transparent"
            placeholder="Message (Enter to send, Shift+Enter for a new line)"
            aria-label="Message"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={onInputKey}
          />
          <Button
            type="submit"
            size="icon"
            variant={busy ? "outline" : "default"}
            className="size-[38px] rounded-full"
            aria-label={busy ? "Stop" : "Send"}
            title={busy ? "Stop" : "Send"}
          >
            {busy ? <Square className="size-3" aria-hidden="true" fill="currentColor" /> : <ArrowUp className="size-[18px]" aria-hidden="true" />}
          </Button>
        </form>
        <Notice notice={save.notice.text ? save.notice : notice} className="mt-2 pl-1" />
      </section>
    </>
  );
}

function ColumnCard(props: {
  client: ApiClient;
  models: CatalogModel[];
  column: Column;
  index: number;
  busy: boolean;
  autoFocus: boolean;
  onModel(model: string): void;
  onRemove?(): void;
}) {
  const { column, index, busy } = props;
  const header = useRef<HTMLDivElement>(null);
  // A column just added opens on its model list's button, ready to choose.
  useEffect(() => {
    if (props.autoFocus) header.current?.querySelector<HTMLElement>("[data-slot=select-trigger]")?.focus();
  }, [props.autoFocus]);
  return (
    <motion.article
      className="grid min-w-0"
      layout="position"
      initial={{ opacity: 0, scale: 0.97, y: 8 }}
      animate={{ opacity: 1, scale: 1, y: 0 }}
      transition={{ duration: 0.35, ease: [0.22, 1, 0.36, 1] }}
    >
      <Card className="max-h-[calc(100svh-360px)] min-h-[340px] gap-0 overflow-hidden p-0 transition-[border-color,box-shadow] duration-200 focus-within:border-axis focus-within:shadow-md">
        <div ref={header} className="flex items-center gap-2 border-b bg-muted/40 px-3 py-2.5">
          <span
            className="grid size-6 flex-none place-items-center rounded-[7px] bg-linear-135 from-brand-1 to-brand-2 text-xs font-semibold text-white shadow-[0_2px_6px_-2px_var(--brand-1)]"
            aria-hidden="true"
          >
            {letter(index)}
          </span>
          <ModelPicker
            client={props.client}
            models={props.models}
            tasks={CHAT_TASKS}
            value={column.model}
            onChange={props.onModel}
            label={`Model ${letter(index)}`}
            pickLabel={`Use in column ${letter(index)}`}
            disabled={busy}
            className="flex-1"
          />
          {props.onRemove && (
            <Button
              type="button"
              variant="ghost"
              size="icon-sm"
              className="text-muted-foreground hover:text-foreground"
              title="Remove this column"
              aria-label={`Remove model ${letter(index)}`}
              disabled={busy}
              onClick={props.onRemove}
            >
              <X aria-hidden="true" />
            </Button>
          )}
        </div>
        <Log column={column} />
      </Card>
    </motion.article>
  );
}

/** The column's messages, following the newest text unless the user scrolled up. */
function Log({ column }: { column: Column }) {
  const ref = useRef<HTMLDivElement>(null);
  const following = useRef(true);
  useLayoutEffect(() => {
    if (ref.current && following.current) ref.current.scrollTop = ref.current.scrollHeight;
  });
  return (
    <div
      className="flex flex-1 flex-col gap-3.5 overflow-y-auto p-4"
      aria-live="polite"
      ref={ref}
      onScroll={(e) => {
        const el = e.currentTarget;
        following.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
      }}
    >
      {column.log.length ? (
        column.log.map((entry) => <EntryView key={entry.id} entry={entry} columnModel={column.model} />)
      ) : (
        <div className="my-auto grid justify-items-center gap-2 py-10 text-center text-sm text-muted-foreground">
          <MessageSquarePlus className="size-7 opacity-50" aria-hidden="true" />
          <span>No messages yet</span>
        </div>
      )}
    </div>
  );
}

// Text as it was typed or written: line breaks kept, long words broken.
const TEXT = "whitespace-pre-wrap wrap-anywhere leading-relaxed";

function EntryView({ entry, columnModel }: { entry: Entry; columnModel: string }) {
  if (entry.role === "user") {
    return (
      <motion.div
        className={cn(
          TEXT,
          "max-w-[85%] self-end rounded-2xl rounded-br-sm bg-primary px-3.5 py-2 text-primary-foreground shadow-xs",
          entry.failed && "line-through opacity-55",
        )}
        title={entry.failed ? "Not sent again: this turn failed" : undefined}
        initial={{ opacity: 0, y: 10, scale: 0.96 }}
        animate={{ opacity: 1, y: 0, scale: 1 }}
        transition={{ type: "spring", stiffness: 420, damping: 30 }}
        style={{ transformOrigin: "bottom right" }}
      >
        {entry.content}
      </motion.div>
    );
  }
  if (entry.role === "pending") return <Pending started={entry.started} />;
  if (entry.role === "error") {
    return (
      <motion.div
        className={cn(TEXT, "rounded-lg border border-critical/25 bg-critical/8 px-3 py-2 text-[13px] text-critical-text")}
        initial={{ opacity: 0, y: 6 }}
        animate={{ opacity: 1, y: 0 }}
      >
        {entry.content}
      </motion.div>
    );
  }

  const meta = entry.meta;
  return (
    <motion.div
      className={cn(TEXT, "max-w-full", entry.failed && "opacity-55")}
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.3, ease: [0.22, 1, 0.36, 1] }}
    >
      {entry.content || (entry.done && <span className="text-muted-foreground">(empty reply)</span>)}
      {!entry.done && (
        <span className="ml-0.5 inline-block h-[1.05em] w-[7px] animate-blink rounded-[1px] bg-foreground align-[-2px]" aria-hidden="true" />
      )}
      <div className="mt-1.5 text-xs whitespace-normal text-muted-foreground">
        {!entry.done ? (
          "Writing…"
        ) : (
          <>
            {fmtMs(meta.ms)}
            {meta.firstMs != null && <span title="Time to the first piece of text">{` · first text ${fmtMs(meta.firstMs)}`}</span>}
            {meta.serverMs != null && ` · server ${fmtMs(meta.serverMs)}`}
            {meta.completionTokens != null && (
              <span title={`${meta.promptTokens ?? "?"} prompt tokens, ${meta.completionTokens} generated`}>
                {` · ${meta.completionTokens} tokens`}
                {tokensPerSecond(meta) != null && ` at ${tokensPerSecond(meta)!.toFixed(1)} tok/s`}
              </span>
            )}
          </>
        )}
        {meta.stopped && " · stopped"}
        {meta.cold && (
          <>
            {" · "}
            <Tooltip>
              <TooltipTrigger asChild>
                <Badge variant="outline" className="rounded-full px-1.5 py-0 font-normal text-muted-foreground">
                  cold load
                </Badge>
              </TooltipTrigger>
              <TooltipContent>This request loaded the model first, so its time includes the load</TooltipContent>
            </Tooltip>
          </>
        )}
        {/* The column's model was changed later in the chat. */}
        {meta.model !== columnModel && ` · ${meta.model}`}
      </div>
    </motion.div>
  );
}

/** Counts up while waiting, since a cold load can take a while. */
function Pending({ started }: { started: number }) {
  const [, setTick] = useState(0);
  useEffect(() => {
    const timer = setInterval(() => setTick((t) => t + 1), 1000);
    return () => clearInterval(timer);
  }, []);
  const s = Math.floor((performance.now() - started) / 1000);
  return (
    <motion.div
      className="inline-flex items-center gap-2.5 self-start text-xs text-muted-foreground"
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
    >
      {/* Three bouncing dots, one after another. */}
      <span className="inline-flex gap-[3px] rounded-full bg-muted px-2.5 py-[7px]" aria-hidden="true">
        <span className="size-[5px] animate-bounce-dot rounded-full bg-muted-foreground" />
        <span className="size-[5px] animate-bounce-dot rounded-full bg-muted-foreground [animation-delay:0.15s]" />
        <span className="size-[5px] animate-bounce-dot rounded-full bg-muted-foreground [animation-delay:0.3s]" />
      </span>
      {s < 2 ? "Waiting for the reply…" : `Waiting for the reply… ${s} s`}
    </motion.div>
  );
}
