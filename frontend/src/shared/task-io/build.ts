// What the task inputs send and what the outputs compute: plain functions,
// so they are tested without a browser. Each builder returns the task's
// input (as models/tasks.py defines it) or why the form cannot run yet.
import { fmtBytes } from "../format";
import { numberProblem, property, type JSONSchema, type TaskLimits } from "./schema";

export interface Built {
  input: unknown;
  /** "" when the input can run */
  problem: string;
}

const fail = (problem: string): Built => ({ input: null, problem });

function textProblem(label: string, text: string, limits: TaskLimits): string {
  if (!text.trim()) return `${label} is empty.`;
  if (text.length > limits.max_input_chars) return `${label} is longer than ${limits.max_input_chars} characters.`;
  return "";
}

// ------------------------------------------------------------ generation
export interface GenerationSettings {
  maxLength: string;
  temperature: string;
  topP: string;
  sample: boolean;
}

/** The settings' starting values, from the input schema's defaults. */
export function defaultGeneration(schema: JSONSchema | null): GenerationSettings {
  const d = (name: string, fallback: unknown) => String(property(schema, name)?.default ?? fallback);
  return { maxLength: "128", temperature: d("temperature", 0.7), topP: d("top_p", 0.9), sample: property(schema, "do_sample")?.default !== false };
}

/** The generation fields of an input, checked against the schema's bounds. */
function generationFields(settings: GenerationSettings, schema: JSONSchema | null): Built {
  const fields: Record<string, unknown> = { do_sample: settings.sample };
  const checks: [string, string, string][] = [
    ["max_length", "Max new tokens", settings.maxLength],
    ["temperature", "Temperature", settings.temperature],
    ["top_p", "Top p", settings.topP],
  ];
  for (const [name, label, raw] of checks) {
    // Sampling settings only count when sampling.
    if (!settings.sample && name !== "max_length") continue;
    if (!raw.trim()) {
      if (name === "max_length") continue; // unset: the model's default
      return fail(`${label} is empty.`);
    }
    const value = Number(raw);
    const problem = numberProblem(label, value, property(schema, name));
    if (problem) return fail(problem);
    fields[name] = value;
  }
  return { input: fields, problem: "" };
}

export function textGenerationInput(prompt: string, settings: GenerationSettings, schema: JSONSchema | null, limits: TaskLimits): Built {
  const problem = textProblem("The prompt", prompt, limits);
  if (problem) return fail(problem);
  const generation = generationFields(settings, schema);
  if (generation.problem) return generation;
  return { input: { text: prompt, ...(generation.input as object) }, problem: "" };
}

/** A prompt, about an image when there is one (sent as a chat with an image part, as /v1/chat/completions does). */
export function visionInput(
  prompt: string,
  imageUrl: string | null,
  settings: GenerationSettings,
  schema: JSONSchema | null,
  limits: TaskLimits,
): Built {
  const built = textGenerationInput(prompt, settings, schema, limits);
  if (built.problem || !imageUrl) return built;
  const messages = [{ role: "user", content: [{ type: "text", text: prompt }, { type: "image_url", image_url: { url: imageUrl } }] }];
  return { input: { ...(built.input as object), messages }, problem: "" };
}

// ------------------------------------------------------------ text inputs
export function classificationInput(text: string, limits: TaskLimits): Built {
  const problem = textProblem("The text", text, limits);
  return problem ? fail(problem) : { input: { text }, problem: "" };
}

export function questionAnsweringInput(question: string, context: string, limits: TaskLimits): Built {
  const problem = textProblem("The question", question, limits) || textProblem("The context", context, limits);
  return problem ? fail(problem) : { input: { question, context }, problem: "" };
}

/** One text per non-empty line. */
export function embeddingInput(lines: string, limits: TaskLimits): Built {
  const texts = lines.split("\n").map((l) => l.trim()).filter(Boolean);
  if (!texts.length) return fail("Enter at least one text, one per line.");
  if (texts.length > limits.max_embedding_inputs) return fail(`At most ${limits.max_embedding_inputs} texts at once.`);
  const long = texts.findIndex((t) => t.length > limits.max_input_chars);
  if (long >= 0) return fail(`Line ${long + 1} is longer than ${limits.max_input_chars} characters.`);
  return { input: { texts }, problem: "" };
}

// ------------------------------------------------------------------ files
export interface LoadedFile {
  name: string;
  /** A data: URL of the file */
  url: string;
  bytes: number;
}

/** The base64 part of a data URL. */
const base64Of = (dataUrl: string): string => dataUrl.slice(dataUrl.indexOf(",") + 1);

/** Why ``file`` cannot be sent: too large, or not one of ``types`` (MIME subtypes after "image/"). */
export function imageProblem(file: { type: string; size: number }, types: string[], maxBytes: number): string {
  const subtype = file.type.replace(/^image\//, "");
  if (!file.type.startsWith("image/") || !types.includes(subtype)) return `Use a ${types.join(", ").toUpperCase()} image.`;
  if (file.size > maxBytes) return `The image is larger than ${fmtBytes(maxBytes)}.`;
  return "";
}

export function speechInput(audio: LoadedFile | null, language: string, prompt: string, limits: TaskLimits): Built {
  if (!audio) return fail("Choose an audio file.");
  if (audio.bytes > limits.max_audio_bytes) return fail(`The audio is larger than ${fmtBytes(limits.max_audio_bytes)}.`);
  const input: Record<string, unknown> = { audio: base64Of(audio.url), filename: audio.name };
  if (language.trim()) input.language = language.trim();
  if (prompt.trim()) input.prompt = prompt.trim();
  return { input, problem: "" };
}

// --------------------------------------------------------------- decision
export type QuestionType = "boolean" | "choice" | "score";

export interface DraftQuestion {
  name: string;
  type: QuestionType;
  instructions: string;
  /** Choice: the options, one per line. Score: the levels, lowest first, one per line. */
  options: string;
}

/** vegaml's questions from the drafts, or why they are not ready. */
function decisionQuestions(drafts: DraftQuestion[]): { questions: Record<string, unknown>; problem: string } {
  const questions: Record<string, unknown> = {};
  if (!drafts.length) return { questions, problem: "Add a question." };
  for (const [i, q] of drafts.entries()) {
    const name = q.name.trim();
    const where = name ? `Question "${name}"` : `Question ${i + 1}`;
    if (!name) return { questions, problem: `${where} needs a name.` };
    if (name in questions) return { questions, problem: `Two questions are named "${name}".` };
    const lines = q.options.split("\n").map((l) => l.trim()).filter(Boolean);
    let criteria: unknown = null;
    if (q.type === "boolean") {
      if (!q.instructions.trim()) return { questions, problem: `${where} needs instructions: what is true or false?` };
    } else if (q.type === "choice") {
      if (!lines.length) return { questions, problem: `${where} needs options, one per line.` };
      criteria = Object.fromEntries(lines.map((o) => [o, null]));
    } else {
      if (!lines.length || lines.length > 10) return { questions, problem: `${where} needs 1 to 10 levels, lowest first.` };
      criteria = lines;
    }
    questions[name] = { type: q.type, instructions: q.instructions.trim(), criteria };
  }
  return { questions, problem: "" };
}

/** A decision about a text state or an image. */
export function decisionInput(state: string, image: LoadedFile | null, drafts: DraftQuestion[], limits: TaskLimits): Built {
  const { questions, problem } = decisionQuestions(drafts);
  if (problem) return fail(problem);
  if (image) return { input: { image: base64Of(image.url), questions }, problem: "" };
  const stateProblem = textProblem("The state", state, limits);
  return stateProblem ? fail(stateProblem) : { input: { state, questions }, problem: "" };
}

// ---------------------------------------------------------------- outputs
/** Labels by score, highest first. */
export function rankedScores(scores: Record<string, number>): [string, number][] {
  return Object.entries(scores).sort(([, a], [, b]) => b - a);
}

/** The context split around the first occurrence of the answer (ignoring case), or null if it is not found. */
export function answerSpan(context: string, answer: string): [string, string, string] | null {
  const at = answer ? context.toLowerCase().indexOf(answer.toLowerCase()) : -1;
  if (at < 0) return null;
  return [context.slice(0, at), context.slice(at, at + answer.length), context.slice(at + answer.length)];
}

export function norm(v: number[]): number {
  return Math.sqrt(v.reduce((sum, x) => sum + x * x, 0));
}

export function cosine(a: number[], b: number[]): number {
  const n = norm(a) * norm(b);
  return n ? a.reduce((sum, x, i) => sum + x * b[i], 0) / n : 0;
}

/** Cosine similarity of every pair. */
export function similarityMatrix(vectors: number[][]): number[][] {
  return vectors.map((a) => vectors.map((b) => cosine(a, b)));
}

/** One vegaml answer, read for display: the headline, the probabilities, and its flags. */
export function decisionSummary(answer: Record<string, unknown>): { headline: string; probs: [string, number][]; flags: string[] } {
  const probs = answer.probs && typeof answer.probs === "object" ? rankedScores(answer.probs as Record<string, number>) : [];
  let headline = "";
  if (typeof answer.choice === "string") headline = answer.choice;
  else if (typeof answer.expected_score === "number") headline = `score ${answer.expected_score.toFixed(2)}`;
  else if (typeof answer.boolean === "number") headline = answer.boolean >= 0.5 ? "true" : "false";
  else if (typeof answer.boolean === "boolean") headline = String(answer.boolean);
  else if (probs.length) headline = probs[0][0];
  const flags: string[] = [];
  if (answer.abstain === true) flags.push("abstains: not confident enough to answer");
  if (answer.unbound === true) flags.push("input looks unlike what it was trained on");
  if (Array.isArray(answer.prediction_set) && answer.prediction_set.length > 1) flags.push(`could be any of: ${answer.prediction_set.join(", ")}`);
  return { headline, probs, flags };
}
