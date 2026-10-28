// The one reading of a JSON object typed into a form field, and of JSON Lines.
import { message } from "./errors";

export type Parsed<T> = { ok: true; value: T } | { ok: false; error: string };

interface ParseOptions {
  /** Names the field in the error: "The body is not valid JSON: …". */
  what?: string;
  /** ``what`` is plural ("Extra fields are …"). */
  plural?: boolean;
  /** An example, shown after "a JSON object": ``like {"id": "ann"}``. */
  hint?: string;
}

/**
 * The JSON object in ``text``, or what is wrong with it. Blank text is an error unless ``emptyMeans``
 * says what it stands for: an empty object (a params box), or null (no body).
 */
export function parseJsonObject(text: string, options?: ParseOptions & { emptyMeans?: Record<string, unknown> }): Parsed<Record<string, unknown>>;
export function parseJsonObject(text: string, options: ParseOptions & { emptyMeans: null }): Parsed<Record<string, unknown> | null>;
export function parseJsonObject(
  text: string,
  { what = "The input", plural = false, hint, emptyMeans }: ParseOptions & { emptyMeans?: Record<string, unknown> | null } = {},
): Parsed<Record<string, unknown> | null> {
  const subject = `${what} ${plural ? "are" : "is"}`;
  if (!text.trim()) return emptyMeans === undefined ? { ok: false, error: `${subject} required` } : { ok: true, value: emptyMeans };
  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch (e) {
    return { ok: false, error: `${subject} not valid JSON: ${message(e)}` };
  }
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return { ok: false, error: `${subject} a JSON object${hint ? `, ${hint}` : ""}` };
  }
  return { ok: true, value: value as Record<string, unknown> };
}

/** One problem in JSON Lines text: where it is (character offsets) and what is wrong. */
export interface LineProblem {
  from: number;
  to: number;
  message: string;
}

/** The lines of ``text`` that are not JSON, each with its error; blank lines are skipped. */
export function jsonLinesProblems(text: string): LineProblem[] {
  const problems: LineProblem[] = [];
  let from = 0;
  for (const [i, line] of text.split("\n").entries()) {
    if (line.trim()) {
      try {
        JSON.parse(line);
      } catch (e) {
        problems.push({ from, to: from + line.length, message: `Line ${i + 1}: ${message(e)}` });
      }
    }
    from += line.length + 1;
  }
  return problems;
}
