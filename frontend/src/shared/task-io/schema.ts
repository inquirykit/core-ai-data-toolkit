// What GET /tasks serves: each task's input and output as JSON Schema
// (from the contracts in models/tasks.py), and the limits schemas cannot
// show. The inputs read their caps and defaults from here instead of
// repeating the server's numbers; tasks without their own input get a form
// made from the schema (SchemaFormInput, shared/schema-form.tsx).
import type { Schemas } from "../api";

export interface JSONSchema {
  type?: string | string[];
  title?: string;
  description?: string;
  default?: unknown;
  enum?: unknown[];
  const?: unknown;
  format?: string;
  properties?: Record<string, JSONSchema>;
  required?: string[];
  items?: JSONSchema;
  anyOf?: JSONSchema[];
  $ref?: string;
  $defs?: Record<string, JSONSchema>;
  minimum?: number;
  maximum?: number;
  exclusiveMinimum?: number;
  exclusiveMaximum?: number;
  minLength?: number;
  maxLength?: number;
  minItems?: number;
  maxItems?: number;
}

export type TaskLimits = Schemas["TaskLimits"];

// The API types each contract as a plain JSON object; it is a JSON Schema, so it is read as one here.
export type TaskInfo = Omit<Schemas["TaskInfo"], "input" | "output"> & { input: JSONSchema | null; output: JSONSchema | null };

/** GET /tasks */
export type TasksResponse = Omit<Schemas["Tasks"], "tasks"> & { tasks: Record<string, TaskInfo> };

/**
 * ``schema`` as the value looks when set: a ``$ref`` followed (within
 * ``root``), and an optional field's ``anyOf: [X, null]`` narrowed to X,
 * keeping the outer title, description and default.
 */
export function resolve(schema: JSONSchema, root: JSONSchema): JSONSchema {
  if (schema.$ref) {
    const name = schema.$ref.replace(/^#\/\$defs\//, "");
    const { $ref: _ref, ...rest } = schema;
    return resolve({ ...(root.$defs?.[name] ?? {}), ...rest }, root);
  }
  if (schema.anyOf) {
    const set = schema.anyOf.filter((s) => s.type !== "null");
    if (set.length === 1) {
      const { anyOf: _anyOf, ...rest } = schema;
      return resolve({ ...set[0], ...rest }, root);
    }
  }
  return schema;
}

/** The resolved schema of ``root``'s property ``name``, or null if it has none. */
export function property(root: JSONSchema | null, name: string): JSONSchema | null {
  const raw = root?.properties?.[name];
  return raw ? resolve(raw, root!) : null;
}

/**
 * Bounds and a step for a number input, from a number or integer schema.
 * Decimals step "any": a numeric step counts from min, so 0.7 would be "off
 * step" between min 0.01 and step 0.1.
 */
export function numberRange(schema: JSONSchema | null): { min?: number; max?: number; step: number | "any" } {
  if (!schema) return { step: "any" };
  const integer = schema.type === "integer";
  const step = integer ? 1 : "any";
  const min = schema.minimum ?? (schema.exclusiveMinimum != null ? schema.exclusiveMinimum + (integer ? 1 : 0.01) : undefined);
  const max = schema.maximum ?? (schema.exclusiveMaximum != null ? schema.exclusiveMaximum - (integer ? 1 : 0.01) : undefined);
  return { min, max, step };
}

/** Why ``value`` breaks ``schema``'s bounds, or "". */
export function numberProblem(label: string, value: number, schema: JSONSchema | null): string {
  if (Number.isNaN(value)) return `${label} must be a number.`;
  if (!schema) return "";
  if (schema.type === "integer" && !Number.isInteger(value)) return `${label} must be a whole number.`;
  if (schema.minimum != null && value < schema.minimum) return `${label} must be at least ${schema.minimum}.`;
  if (schema.exclusiveMinimum != null && value <= schema.exclusiveMinimum) return `${label} must be above ${schema.exclusiveMinimum}.`;
  if (schema.maximum != null && value > schema.maximum) return `${label} must be at most ${schema.maximum}.`;
  if (schema.exclusiveMaximum != null && value >= schema.exclusiveMaximum) return `${label} must be below ${schema.exclusiveMaximum}.`;
  return "";
}
