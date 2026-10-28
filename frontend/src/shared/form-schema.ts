// What a form (schema-form.tsx) is made from: a JSON Schema the API serves,
// made plain, and how its fields are edited when nothing says otherwise.
//
// formSchema inlines the $refs, from the OpenAPI document's components or the
// schema's own $defs, and narrows Pydantic's optional fields (anyOf: [X,
// null]) to X, keeping the outer title, description and default; a default of
// null is dropped, since a field left unset is left out of what the form sends
// and the server fills in its default. An array of choices (items with an
// enum) is a set, so it gets uniqueItems, which the form edits as checkboxes.
//
// uiDefaults gives every field of a schema its editor by its shape, so a field
// added to a Pydantic model shows up in its form with nothing else to write: a
// dict of strings is rows of key and value, any other object without declared
// properties is JSON, a set of choices is checkboxes, a constant is hidden.
import type { RJSFSchema, UiSchema } from "@rjsf/utils";

/** The node ``ref`` ("#/components/schemas/X", "#/$defs/X") points at in ``root``. */
function pointer(root: RJSFSchema, ref: string): RJSFSchema | undefined {
  if (!ref.startsWith("#/")) return undefined;
  let node: unknown = root;
  for (const part of ref.slice(2).split("/")) {
    const key = decodeURIComponent(part).replace(/~1/g, "/").replace(/~0/g, "~");
    node = node && typeof node === "object" ? (node as Record<string, unknown>)[key] : undefined;
  }
  return node && typeof node === "object" ? (node as RJSFSchema) : undefined;
}

const isSchema = (value: unknown): value is RJSFSchema => Boolean(value) && typeof value === "object" && !Array.isArray(value);

/** Past this depth a $ref is left as it is: a schema that refers to itself. */
const MAX_DEPTH = 24;

/** ``schema`` with its $refs (into ``root``) inlined and its optional fields narrowed. */
export function formSchema(schema: RJSFSchema, root: RJSFSchema = schema, depth = 0): RJSFSchema {
  if (depth > MAX_DEPTH) return schema;
  if (typeof schema.$ref === "string") {
    const { $ref, ...rest } = schema;
    const target = pointer(root, $ref);
    return target ? formSchema({ ...target, ...rest }, root, depth + 1) : schema;
  }
  const choices = schema.anyOf ?? schema.oneOf;
  if (Array.isArray(choices)) {
    const set = choices.filter((c) => !(isSchema(c) && c.type === "null"));
    if (set.length === 1 && set.length < choices.length && isSchema(set[0])) {
      const { anyOf: _anyOf, oneOf: _oneOf, ...outer } = schema;
      const narrowed: RJSFSchema = { ...formSchema(set[0], root, depth + 1), ...outer };
      if (narrowed.default === null) delete narrowed.default;
      return narrowed;
    }
  }
  const out: RJSFSchema = { ...schema };
  delete out.$defs;
  if (isSchema(out.properties)) {
    out.properties = Object.fromEntries(
      Object.entries(out.properties).map(([name, p]) => [name, isSchema(p) ? formSchema(p, root, depth + 1) : p]),
    );
  }
  if (isSchema(out.items)) {
    out.items = formSchema(out.items, root, depth + 1);
    if (Array.isArray(out.items.enum)) out.uniqueItems = true;
  }
  if (isSchema(out.additionalProperties)) out.additionalProperties = formSchema(out.additionalProperties, root, depth + 1);
  for (const key of ["anyOf", "oneOf", "allOf"] as const) {
    const list = out[key];
    if (Array.isArray(list)) out[key] = list.map((s) => (isSchema(s) ? formSchema(s, root, depth + 1) : s));
  }
  return out;
}

const isDict = (s: RJSFSchema) => s.type === "object" && !isSchema(s.properties);

/** The editor of each field of ``schema`` (made plain by formSchema) that its shape calls for. */
export function uiDefaults(schema: RJSFSchema): UiSchema {
  if (schema.const !== undefined) return { "ui:widget": "hidden" };
  if (isDict(schema)) {
    const values = schema.additionalProperties;
    return isSchema(values) && values.type === "string" ? { "ui:field": "keyValue" } : { "ui:field": "json" };
  }
  if (schema.type === "array" && isSchema(schema.items)) {
    if (schema.uniqueItems && Array.isArray(schema.items.enum)) return { "ui:widget": "checkboxes", "ui:options": { inline: true } };
    const items = uiDefaults(schema.items);
    return Object.keys(items).length ? { items } : {};
  }
  const ui: UiSchema = {};
  if (isSchema(schema.properties)) {
    for (const [name, p] of Object.entries(schema.properties)) {
      if (!isSchema(p)) continue;
      const field = uiDefaults(p);
      if (Object.keys(field).length) ui[name] = field;
    }
  }
  return ui;
}

const isPlain = (value: unknown): value is Record<string, unknown> => Boolean(value) && typeof value === "object" && !Array.isArray(value);

/** ``base`` with ``over`` laid on it, key by key into nested fields and ``ui:options``; ``over`` wins. */
export function mergeUi(base: UiSchema, over: UiSchema = {}): UiSchema {
  const out: UiSchema = { ...base };
  for (const [key, value] of Object.entries(over)) {
    const nested = key === "ui:options" || !key.startsWith("ui:");
    out[key] = nested && isPlain(value) && isPlain(out[key]) ? mergeUi(out[key], value) : value;
  }
  return out;
}

/** The fields of ``value`` that ``schema`` declares: a record the server sent, as the form edits it (no read-only extras). */
export function formValue<T extends Record<string, unknown>>(schema: RJSFSchema, value: T): Partial<T> {
  const names = isSchema(schema.properties) ? Object.keys(schema.properties) : Object.keys(value);
  return Object.fromEntries(names.filter((n) => n in value).map((n) => [n, value[n]])) as Partial<T>;
}

/** A value as text that is the same for the same content: unset, null and empty strings left out, keys in order. */
export function valueKey(value: unknown): string {
  const plain = (v: unknown): unknown => {
    if (Array.isArray(v)) return v.map(plain);
    if (!isPlain(v)) return v;
    const entries = Object.entries(v)
      .filter(([, x]) => x !== undefined && x !== null && x !== "")
      .sort(([a], [b]) => a.localeCompare(b));
    return Object.fromEntries(entries.map(([k, x]) => [k, plain(x)]));
  };
  return JSON.stringify(plain(value));
}
