// A table's search for its nearest rows (DataNearest.tsx): what it asks, and its distances in words.

import type { Schemas } from "../../../shared/api";
import type { FieldInfo } from "../data";

/** What a table's search for its nearest rows asks: a text its vector field's model embeds, or a vector as JSON. */
export interface NearestForm {
  /** The text, or the vector as JSON (a field without a model) */
  text: string;
  k: number;
  /** Words its full-text fields hold; empty for none */
  words: string;
  /** Rank by the text's words too (a field with a model, a table with full-text fields) */
  hybrid?: boolean;
  /** A rerank model to read the rows found with (a field with a model); empty for none */
  rerank?: string;
}

export type NearestQuery = Schemas["VectorQuery"];

/**
 * The body of POST /db/{ws}/{table}/_search for the form, or what is wrong with it: a text when the field has a
 * model, else a JSON list of exactly its dimensions in numbers.
 */
export function nearestQuery(field: FieldInfo, form: NearestForm): { query: NearestQuery } | { error: string } {
  const words = form.words.trim();
  const rest = { k: form.k, ...(words ? { q: words } : {}) };
  if (field.model) {
    const text = form.text.trim();
    const rerank = form.rerank?.trim();
    return text
      ? { query: { text, ...rest, ...(form.hybrid ? { hybrid: true } : {}), ...(rerank ? { rerank } : {}) } }
      : { error: "Type a text to find the rows nearest to" };
  }
  let vector: unknown;
  try {
    vector = JSON.parse(form.text);
  } catch {
    return { error: `A vector is a JSON list of ${field.dimensions} numbers, like [0.1, 0.2, …]` };
  }
  if (!Array.isArray(vector) || !vector.every((x) => typeof x === "number" && Number.isFinite(x))) {
    return { error: `A vector is a JSON list of ${field.dimensions} numbers, like [0.1, 0.2, …]` };
  }
  if (vector.length !== field.dimensions) return { error: `${field.name} holds ${field.dimensions} numbers, not ${vector.length}` };
  return { query: { vector: vector as number[], ...rest } };
}

/** A distance as the results show it: four decimals, or in powers of ten when that would hide it. */
export const fmtDistance = (distance: number): string =>
  distance === 0 ? "0" : Math.abs(distance) < 0.0001 ? distance.toExponential(1) : distance.toFixed(4);

/** What a distance is by the field's similarity: lower is nearer for each. */
export function distanceMeaning(similarity: string | null | undefined): string {
  if (similarity === "DOT_PRODUCT") return "By DOT_PRODUCT: -(1 + the dot product) / 2, lower nearer, -1 for the same unit vector";
  if (similarity === "EUCLIDEAN") return "By EUCLIDEAN: the squared distance, lower nearer, 0 for the same vector";
  return "By COSINE: 1 minus the cosine, lower nearer, 0 for the same direction";
}
