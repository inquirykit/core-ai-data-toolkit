import { describe, expect, it } from "vitest";
import { newKey, readForm, type SetupFields } from "./form";

const ADMIN = "admin-key-0123456789abcdef";
const CLIENT = "client-key-0123456789abcdef";
const fields = (overrides: Partial<SetupFields> = {}): SetupFields => ({
  code: " code ",
  adminKey: ADMIN,
  requireClientKey: true,
  clientKey: CLIENT,
  modelsMode: "list",
  selectedModels: ["gpt2", "gpt2", "org/model"],
  corsMode: "list",
  corsOrigins: "http://localhost:3000/\nhttps://app.example.com, http://localhost:3000",
  ...overrides,
});

describe("readForm", () => {
  it("builds the setup request", () => {
    expect(readForm(fields())).toEqual({
      code: "code",
      admin_api_key: ADMIN,
      api_key: CLIENT,
      allowed_models: ["gpt2", "org/model"],
      cors_origins: ["http://localhost:3000", "https://app.example.com"],
    });
  });

  it("maps the 'any' choices and a missing client key", () => {
    const body = readForm(fields({ requireClientKey: false, modelsMode: "any", corsMode: "any" }));
    expect(body.api_key).toBeNull();
    expect(body.allowed_models).toBeNull();
    expect(body.cors_origins).toEqual(["*"]);
  });

  it.each<[Partial<SetupFields>, RegExp]>([
    [{ code: " " }, /setup code/],
    [{ adminKey: "short" }, /admin key needs/],
    [{ clientKey: "has spaces in it, too" }, /client key needs/],
    [{ clientKey: ADMIN }, /different keys/],
    [{ selectedModels: [] }, /at least one model/],
    [{ corsOrigins: "http://localhost:3000/path" }, /not an origin/],
  ])("rejects %o", (overrides, message) => {
    expect(() => readForm(fields(overrides))).toThrow(message);
  });
});

describe("newKey", () => {
  it("makes keys the server accepts", () => {
    const key = newKey();
    expect(key).toMatch(/^[A-Za-z0-9_-]{43}$/);
    expect(newKey()).not.toEqual(key);
  });
});
