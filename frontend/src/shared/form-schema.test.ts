import { describe, expect, it } from "vitest";
import type { RJSFSchema } from "@rjsf/utils";

import { formSchema, formValue, mergeUi, uiDefaults, valueKey } from "./form-schema";

describe("formSchema", () => {
  it("inlines refs into the OpenAPI document's components and the schema's own $defs", () => {
    const doc = {
      components: {
        schemas: {
          Spec: { type: "object", properties: { request: { $ref: "#/components/schemas/Request" } } },
          Request: { type: "object", title: "Request", properties: { method: { $ref: "#/components/schemas/Method" } } },
          Method: { type: "string", enum: ["GET", "POST"] },
        },
      },
    } as RJSFSchema;
    expect(formSchema((doc.components as { schemas: Record<string, RJSFSchema> }).schemas.Spec, doc)).toEqual({
      type: "object",
      properties: { request: { type: "object", title: "Request", properties: { method: { type: "string", enum: ["GET", "POST"] } } } },
    });
    const own: RJSFSchema = { type: "object", properties: { kind: { $ref: "#/$defs/Kind", title: "Kind" } }, $defs: { Kind: { enum: ["a", "b"] } } };
    expect(formSchema(own)).toEqual({ type: "object", properties: { kind: { enum: ["a", "b"], title: "Kind" } } });
  });

  it("narrows an optional field to its type, keeping its title and description and dropping a null default", () => {
    const schema: RJSFSchema = {
      type: "object",
      properties: {
        secret: { anyOf: [{ type: "string", pattern: "^[A-Z]+$" }, { type: "null" }], title: "Secret", description: "An env var", default: null },
        body: { anyOf: [{ type: "object", additionalProperties: true }, { type: "null" }] },
        either: { anyOf: [{ type: "string" }, { type: "integer" }] },
      },
    };
    expect(formSchema(schema).properties).toEqual({
      secret: { type: "string", pattern: "^[A-Z]+$", title: "Secret", description: "An env var" },
      body: { type: "object", additionalProperties: true },
      either: { anyOf: [{ type: "string" }, { type: "integer" }] },
    });
  });

  it("goes into array items and dict values, and leaves a schema that refers to itself alone past a depth", () => {
    const doc = {
      components: { schemas: { Node: { type: "object", properties: { children: { type: "array", items: { $ref: "#/components/schemas/Node" } } } } } },
    } as RJSFSchema;
    const tree = formSchema({ $ref: "#/components/schemas/Node" }, doc);
    expect(tree.type).toBe("object");
    expect(JSON.stringify(tree)).toContain('"$ref":"#/components/schemas/Node"');
    expect(formSchema({ type: "object", additionalProperties: { anyOf: [{ type: "string" }, { type: "null" }] } })).toEqual({
      type: "object",
      additionalProperties: { type: "string" },
    });
  });
});

describe("uiDefaults", () => {
  it("edits each field by its shape: dicts of strings as rows, other dicts as JSON, sets as checkboxes, constants hidden", () => {
    const schema = formSchema({
      type: "object",
      properties: {
        kind: { const: "http", type: "string" },
        headers: { type: "object", additionalProperties: { type: "string" } },
        body: { anyOf: [{ type: "object", additionalProperties: true }, { type: "null" }] },
        inputs: { type: "array", items: { enum: ["ipv4", "domain"], type: "string" } },
        tags: { type: "array", items: { type: "string" } },
        request: { type: "object", properties: { extra: { type: "object" } } },
        backends: { type: "array", items: { type: "object", properties: { env: { type: "object", additionalProperties: { type: "string" } } } } },
        name: { type: "string" },
      },
    });
    expect(schema.properties!.inputs).toMatchObject({ uniqueItems: true });
    expect(uiDefaults(schema)).toEqual({
      kind: { "ui:widget": "hidden" },
      headers: { "ui:field": "keyValue" },
      body: { "ui:field": "json" },
      inputs: { "ui:widget": "checkboxes", "ui:options": { inline: true } },
      request: { extra: { "ui:field": "json" } },
      backends: { items: { env: { "ui:field": "keyValue" } } },
    });
  });
});

describe("mergeUi", () => {
  it("lays the page's settings over the defaults, into nested fields, the page's winning", () => {
    expect(
      mergeUi(
        { request: { body: { "ui:field": "json" } }, inputs: { "ui:widget": "checkboxes", "ui:options": { inline: true } } },
        { request: { body: { "ui:title": "Body" } }, inputs: { "ui:options": { label: false } } },
      ),
    ).toEqual({
      request: { body: { "ui:field": "json", "ui:title": "Body" } },
      inputs: { "ui:widget": "checkboxes", "ui:options": { inline: true, label: false } },
    });
  });
});

describe("formValue and valueKey", () => {
  it("keeps the declared fields of a record, and keys values by content", () => {
    const schema: RJSFSchema = { type: "object", properties: { name: { type: "string" }, ttl: { type: "string" } } };
    expect(formValue(schema, { name: "a", ttl: "1h", builtin: true })).toEqual({ name: "a", ttl: "1h" });
    expect(valueKey({ b: 1, a: { y: null, x: "" }, c: [{ z: undefined, w: 2 }] })).toBe(valueKey({ c: [{ w: 2 }], a: {}, b: 1 }));
    expect(valueKey({ a: 1 })).not.toBe(valueKey({ a: 2 }));
  });
});
