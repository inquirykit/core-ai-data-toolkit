// One input per task. Each holds its own fields and reports, as they
// change, the input they make or why they make none (see build.ts).
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { Plus, Trash2, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { CodeEditor } from "../code-editor";
import { Field, Segmented, SelectField } from "../components";
import { fmtBytes } from "../format";
import { formSchema } from "../form-schema";
import type { FormSchema } from "../schema-form";
import { SchemaForm } from "../schema-form";
import {
  classificationInput,
  decisionInput,
  defaultGeneration,
  embeddingInput,
  imageProblem,
  questionAnsweringInput,
  speechInput,
  textGenerationInput,
  visionInput,
  type Built,
  type DraftQuestion,
  type GenerationSettings,
  type LoadedFile,
  type QuestionType,
} from "./build";
import { numberRange, property, type JSONSchema, type TaskLimits } from "./schema";

interface IOContext {
  task: string;
  /** The task's input schema; null for a task without a contract */
  schema: JSONSchema | null;
  limits: TaskLimits;
}

export interface InputProps {
  ctx: IOContext;
  onChange(built: Built): void;
}

function readFile(file: File): Promise<LoadedFile> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve({ name: file.name, url: String(reader.result), bytes: file.size });
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}

// Textareas keep the height their rows give (shadcn's grow with their content, from one line).
const AREA = "field-sizing-fixed";
// Small fields side by side, wrapping on a narrow screen.
const ROW = "flex flex-wrap items-end gap-2.5";

/** Like Field, for more than one control (or one with buttons of its own): a heading, not a <label>. */
function FieldGroup({ label, hint, children }: { label: string; hint?: ReactNode; children: ReactNode }) {
  return (
    <div className="grid min-w-0 content-start gap-2">
      <span className="text-sm leading-none font-medium">
        {label}
        {hint && <span className="ml-2 text-xs font-normal text-muted-foreground">{hint}</span>}
      </span>
      {children}
    </div>
  );
}

/** A checkbox with its text beside it. */
function Check(props: { checked: boolean; title?: string; onChange(checked: boolean): void; children: ReactNode }) {
  return (
    <label className="flex w-fit items-center gap-2 text-sm text-muted-foreground" title={props.title}>
      <Checkbox checked={props.checked} onCheckedChange={(c) => props.onChange(c === true)} />
      {props.children}
    </label>
  );
}

// --------------------------------------------------------------- shared parts
function GenerationControls(props: { settings: GenerationSettings; schema: JSONSchema | null; onChange(s: GenerationSettings): void }) {
  const { settings, schema, onChange } = props;
  const set = (patch: Partial<GenerationSettings>) => onChange({ ...settings, ...patch });
  const number = (name: string) => numberRange(property(schema, name));
  return (
    <div className={ROW}>
      <Field label="Max new tokens" className="w-34">
        <Input type="number" {...number("max_length")} placeholder="model default" value={settings.maxLength} onChange={(e) => set({ maxLength: e.target.value })} />
      </Field>
      <Field label="Temperature" className="w-34">
        <Input type="number" {...number("temperature")} disabled={!settings.sample} value={settings.temperature} onChange={(e) => set({ temperature: e.target.value })} />
      </Field>
      <Field label="Top p" className="w-34">
        <Input type="number" {...number("top_p")} disabled={!settings.sample} value={settings.topP} onChange={(e) => set({ topP: e.target.value })} />
      </Field>
      <div className="flex h-9 items-center">
        <Check checked={settings.sample} onChange={(sample) => set({ sample })}>
          Sample (off: always the likeliest token)
        </Check>
      </div>
    </div>
  );
}

/** Choose an image file: a preview, and why it cannot be sent if it cannot. */
function ImagePicker(props: { types: string[]; maxBytes: number; value: LoadedFile | null; onChange(file: LoadedFile | null): void }) {
  const [problem, setProblem] = useState("");
  const accept = props.types.map((t) => `image/${t}`).join(",");
  return (
    <div className="grid gap-1.5">
      {props.value ? (
        <div className="flex animate-in items-start gap-3 duration-300 fade-in-0 zoom-in-95">
          <img src={props.value.url} alt={props.value.name} className="h-[90px] w-[120px] rounded-lg border object-cover shadow-xs" />
          <div className="grid min-w-0 justify-items-start gap-1 text-sm wrap-anywhere">
            <div className="font-mono text-[13px]">{props.value.name}</div>
            <div className="text-xs text-muted-foreground">{fmtBytes(props.value.bytes)}</div>
            <Button type="button" variant="outline" size="sm" className="mt-1" onClick={() => props.onChange(null)}>
              <X aria-hidden="true" />
              Remove
            </Button>
          </div>
        </div>
      ) : (
        <Input
          type="file"
          className="cursor-pointer"
          accept={accept}
          aria-label="Image"
          onChange={async (e) => {
            const file = e.target.files?.[0];
            e.target.value = "";
            if (!file) return;
            const why = imageProblem(file, props.types, props.maxBytes);
            setProblem(why);
            if (!why) props.onChange(await readFile(file));
          }}
        />
      )}
      {problem && <p className="text-xs text-critical-text">{problem}</p>}
      {!props.value && !problem && (
        <p className="text-xs text-muted-foreground">
          {props.types.join(", ").toUpperCase()}, up to {fmtBytes(props.maxBytes)}
        </p>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- the inputs
export function TextGenerationInput({ ctx, onChange }: InputProps) {
  const [prompt, setPrompt] = useState("");
  const [settings, setSettings] = useState(() => defaultGeneration(ctx.schema));
  useEffect(() => onChange(textGenerationInput(prompt, settings, ctx.schema, ctx.limits)), [prompt, settings]);
  return (
    <>
      <Field label="Prompt" hint="the model continues it, or replies if it has a chat template">
        <Textarea rows={4} className={AREA} maxLength={ctx.limits.max_input_chars} value={prompt} onChange={(e) => setPrompt(e.target.value)} />
      </Field>
      <GenerationControls settings={settings} schema={ctx.schema} onChange={setSettings} />
    </>
  );
}

export function VisionInput({ ctx, onChange }: InputProps) {
  const [prompt, setPrompt] = useState("Describe this image.");
  const [image, setImage] = useState<LoadedFile | null>(null);
  const [settings, setSettings] = useState(() => defaultGeneration(ctx.schema));
  useEffect(() => onChange(visionInput(prompt, image?.url ?? null, settings, ctx.schema, ctx.limits)), [prompt, image, settings]);
  return (
    <>
      {/* Not a <label>: a click on the picked image would press its Remove button. */}
      <FieldGroup label="Image" hint="optional">
        <ImagePicker types={["png", "jpeg", "webp", "gif"]} maxBytes={ctx.limits.max_media_bytes} value={image} onChange={setImage} />
      </FieldGroup>
      <Field label="Prompt">
        <Textarea rows={3} className={AREA} maxLength={ctx.limits.max_input_chars} value={prompt} onChange={(e) => setPrompt(e.target.value)} />
      </Field>
      <GenerationControls settings={settings} schema={ctx.schema} onChange={setSettings} />
    </>
  );
}

export function ClassificationInput({ ctx, onChange }: InputProps) {
  const [text, setText] = useState("");
  useEffect(() => onChange(classificationInput(text, ctx.limits)), [text]);
  return (
    <Field label="Text to label">
      <Textarea rows={4} className={AREA} maxLength={ctx.limits.max_input_chars} value={text} onChange={(e) => setText(e.target.value)} />
    </Field>
  );
}

export function QuestionAnsweringInput({ ctx, onChange }: InputProps) {
  const [question, setQuestion] = useState("");
  const [context, setContext] = useState("");
  useEffect(() => onChange(questionAnsweringInput(question, context, ctx.limits)), [question, context]);
  return (
    <>
      <Field label="Question">
        <Input maxLength={ctx.limits.max_input_chars} value={question} onChange={(e) => setQuestion(e.target.value)} />
      </Field>
      <Field label="Context" hint="the answer is copied from this passage">
        <Textarea rows={5} className={AREA} maxLength={ctx.limits.max_input_chars} value={context} onChange={(e) => setContext(e.target.value)} />
      </Field>
    </>
  );
}

export function EmbeddingInput({ ctx, onChange }: InputProps) {
  const [lines, setLines] = useState("");
  useEffect(() => onChange(embeddingInput(lines, ctx.limits)), [lines]);
  const count = lines.split("\n").filter((l) => l.trim()).length;
  return (
    <Field label="Texts, one per line" hint={`${count} of at most ${ctx.limits.max_embedding_inputs}; two or more are compared`}>
      <Textarea
        rows={5}
        className={AREA}
        value={lines}
        placeholder={"A cat sat on the mat.\nA dog lay on the rug.\nStocks fell sharply today."}
        onChange={(e) => setLines(e.target.value)}
      />
    </Field>
  );
}

export function SpeechInput({ ctx, onChange }: InputProps) {
  const [audio, setAudio] = useState<LoadedFile | null>(null);
  const [language, setLanguage] = useState("");
  const [prompt, setPrompt] = useState("");
  useEffect(() => onChange(speechInput(audio, language, prompt, ctx.limits)), [audio, language, prompt]);
  return (
    <>
      <Field label="Audio file" hint={`WAV, MP3, FLAC, OGG…, up to ${fmtBytes(ctx.limits.max_audio_bytes)}`}>
        <div className="grid gap-2">
          <Input
            type="file"
            className="cursor-pointer"
            accept="audio/*"
            aria-label="Audio file"
            onChange={async (e) => {
              const file = e.target.files?.[0];
              setAudio(file ? await readFile(file) : null);
            }}
          />
          {audio && <audio controls src={audio.url} className="w-full max-w-[420px] animate-in fade-in-0" />}
        </div>
      </Field>
      <div className={ROW}>
        <Field label="Language" className="w-34">
          <Input placeholder="detect" maxLength={16} value={language} onChange={(e) => setLanguage(e.target.value)} />
        </Field>
        <Field label="Prompt (optional: names and spellings to expect)" className="flex-[1_1_220px]">
          <Input maxLength={ctx.limits.max_input_chars} value={prompt} onChange={(e) => setPrompt(e.target.value)} />
        </Field>
      </div>
    </>
  );
}

const QUESTION_TYPES: [QuestionType, string, string][] = [
  ["choice", "Choice", "Options, one per line"],
  ["boolean", "Yes / no", ""],
  ["score", "Score", "Levels, lowest first, one per line (up to 10)"],
];

type Mode = "text" | "image";
const MODES: [Mode, string][] = [
  ["text", "Text"],
  ["image", "Image"],
];

export function DecisionInput({ ctx, onChange }: InputProps) {
  const [mode, setMode] = useState<Mode>("text");
  const [state, setState] = useState("");
  const [image, setImage] = useState<LoadedFile | null>(null);
  const [questions, setQuestions] = useState<DraftQuestion[]>([
    { name: "sentiment", type: "choice", instructions: "The customer's overall sentiment", options: "positive\nneutral\nnegative" },
  ]);
  useEffect(() => onChange(decisionInput(state, mode === "image" ? image : null, questions, ctx.limits)), [mode, state, image, questions]);
  const update = (i: number, patch: Partial<DraftQuestion>) => setQuestions((qs) => qs.map((q, j) => (j === i ? { ...q, ...patch } : q)));

  return (
    <>
      <FieldGroup label="What to decide about">
        <Segmented<Mode> label="State" size="sm" options={MODES} value={mode} onChange={setMode} />
        {mode === "text" ? (
          <Textarea
            rows={4}
            className={AREA}
            aria-label="State"
            maxLength={ctx.limits.max_input_chars}
            value={state}
            onChange={(e) => setState(e.target.value)}
          />
        ) : (
          <ImagePicker types={["png", "jpeg", "webp"]} maxBytes={ctx.limits.max_media_bytes} value={image} onChange={setImage} />
        )}
      </FieldGroup>
      <FieldGroup label="Questions" hint="each gets an answer with probabilities">
        <div className="grid gap-2.5">
          {questions.map((q, i) => {
            const help = QUESTION_TYPES.find(([t]) => t === q.type)![2];
            return (
              <fieldset
                key={i}
                className="grid min-w-0 animate-in gap-3 rounded-lg border bg-card p-3 duration-300 fade-in-0 slide-in-from-top-1"
              >
                <div className={ROW}>
                  <Field label="Name" className="w-40">
                    <Input value={q.name} onChange={(e) => update(i, { name: e.target.value })} />
                  </Field>
                  <SelectField
                    label="Type"
                    className="w-34"
                    value={q.type}
                    onChange={(type) => update(i, { type: type as QuestionType })}
                    options={QUESTION_TYPES.map(([t, label]) => ({ value: t, label }))}
                  />
                  <Button
                    type="button"
                    variant="ghost"
                    className="text-muted-foreground"
                    disabled={questions.length === 1}
                    onClick={() => setQuestions((qs) => qs.filter((_, j) => j !== i))}
                  >
                    <Trash2 aria-hidden="true" />
                    Remove
                  </Button>
                </div>
                <Field label={`Instructions${q.type === "boolean" ? " (what is true?)" : " (optional)"}`}>
                  <Input value={q.instructions} onChange={(e) => update(i, { instructions: e.target.value })} />
                </Field>
                {help && (
                  <Field label={help}>
                    <Textarea rows={3} className={AREA} value={q.options} onChange={(e) => update(i, { options: e.target.value })} />
                  </Field>
                )}
              </fieldset>
            );
          })}
        </div>
        <div>
          <Button
            type="button"
            variant="outline"
            size="sm"
            disabled={questions.length >= 128}
            onClick={() => setQuestions((qs) => [...qs, { name: `question_${qs.length + 1}`, type: "boolean", instructions: "", options: "" }])}
          >
            <Plus aria-hidden="true" />
            Add question
          </Button>
        </div>
      </FieldGroup>
    </>
  );
}

/** For a task without its own input: a form made from its input schema, checked as it is filled in. */
export function SchemaFormInput({ ctx, onChange }: InputProps) {
  const schema = useMemo(() => formSchema(ctx.schema as FormSchema), [ctx.schema]);
  const [value, setValue] = useState<Record<string, unknown>>({});
  const [problems, setProblems] = useState<string[]>([]);
  useEffect(() => onChange({ input: value, problem: problems[0] ?? "" }), [value, problems]);
  return <SchemaForm schema={schema} value={value} onChange={setValue} onProblems={setProblems} live />;
}

/** For a task without a contract: the input as JSON, sent as is. */
export function JsonInput({ onChange }: InputProps) {
  const [text, setText] = useState('{\n  "text": ""\n}');
  useEffect(() => {
    try {
      onChange({ input: JSON.parse(text), problem: "" });
    } catch {
      onChange({ input: null, problem: "The input is not valid JSON." });
    }
  }, [text]);
  return (
    <Field label="Input (JSON)" hint="this task has no contract, so the model gets it unchecked">
      <CodeEditor aria-label="Input (JSON)" language="json" minHeight="8rem" value={text} onChange={setText} />
    </Field>
  );
}
