import type { Schemas } from "../shared/api";

export type SetupBody = Schemas["SetupRequest"];

/** What the wizard's fields hold. */
export interface SetupFields {
  code: string;
  adminKey: string;
  requireClientKey: boolean;
  clientKey: string;
  modelsMode: "list" | "any";
  selectedModels: string[];
  corsMode: "list" | "any";
  /** One origin per line (commas and spaces also separate). */
  corsOrigins: string;
}

// The same patterns the server checks (api/setup_routes.py), so most mistakes are caught here first.
const KEY_PATTERN = /^[A-Za-z0-9._~+/=-]{16,256}$/;
const ORIGIN_PATTERN = /^https?:\/\/[A-Za-z0-9.-]+(:[0-9]{1,5})?$/;
const KEY_RULE = "16 or more characters: letters, digits, or . _ ~ + / = -";

/** The POST /admin/setup body for ``fields``; throws an Error with a message for the user. */
export function readForm(fields: SetupFields): SetupBody {
  const code = fields.code.trim();
  const adminKey = fields.adminKey.trim();
  const clientKey = fields.clientKey.trim();

  if (!code) throw new Error("Enter the setup code from the server log.");
  if (!KEY_PATTERN.test(adminKey)) throw new Error(`The admin key needs ${KEY_RULE}`);
  if (fields.requireClientKey) {
    if (!KEY_PATTERN.test(clientKey)) throw new Error(`The client key needs ${KEY_RULE}`);
    if (clientKey === adminKey) throw new Error("Use different keys for clients and for the admin.");
  }

  let allowedModels: string[] | null = null;
  if (fields.modelsMode === "list") {
    allowedModels = [...new Set(fields.selectedModels)];
    if (!allowedModels.length) throw new Error("Select at least one model, or allow any model.");
  }

  let corsOrigins = ["*"];
  if (fields.corsMode === "list") {
    corsOrigins = fields.corsOrigins
      .split(/[\s,]+/)
      .map((o) => o.trim().replace(/\/+$/, ""))
      .filter(Boolean);
    const bad = corsOrigins.find((o) => !ORIGIN_PATTERN.test(o));
    if (bad) throw new Error(`“${bad}” is not an origin like http://localhost:3000 (no path).`);
  }

  return {
    code,
    admin_api_key: adminKey,
    api_key: fields.requireClientKey ? clientKey : null,
    allowed_models: allowedModels,
    cors_origins: [...new Set(corsOrigins)],
  };
}

/** 32 random bytes as base64url: 43 characters the server accepts as a key. */
export function newKey(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(32));
  return btoa(String.fromCharCode(...bytes))
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "");
}
