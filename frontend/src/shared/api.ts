import createFetchClient, { createQuerySerializer, type FetchResponse, type MaybeOptionalInit, type Middleware } from "openapi-fetch";
import createQueryHooks, { type OpenapiQueryClient } from "openapi-react-query";
import type { MediaType, PathsWithMethod, RequiredKeysOf } from "openapi-typescript-helpers";
import type { components, paths } from "./api-schema";

/** The server refused the key (401). The page has already been told to show the sign-in form. */
export class AuthError extends Error {}

/** A reply other than 2xx or 401, with its status (404: not found). */
export class ApiError extends Error {
  constructor(
    message: string,
    public readonly status: number,
  ) {
    super(message);
  }
}

export type Schemas = components["schemas"];
// POST /inference: generated from the task contracts (npm run gen:api).
export type InferenceResponse = paths["/inference"]["post"]["responses"]["200"]["content"]["application/json"];

// ------------------------------------------------------- typed by the schema
// Every call names a path as the API declares it ("/jobs/{id}") and takes
// openapi-fetch's options: ``params.path`` fills the path's ``{name}``s,
// ``params.query`` the query string, ``body`` the JSON body. The types come
// from api-schema.d.ts, so a misspelt path, a missing parameter or a reply
// read wrongly fails `tsc`, not the page.

export type Method = "get" | "put" | "post" | "delete" | "patch";

export type Operation<P extends keyof paths, M extends Method> = paths[P][M];

/** What a call of ``M`` on ``P`` takes, and the reply's body: its JSON, its text with ``parseAs: "text"``, nothing for 204. */
type Init<M extends Method, P extends PathsWithMethod<paths, M>> = MaybeOptionalInit<paths[P], M>;
type Reply<M extends Method, P extends PathsWithMethod<paths, M>, I> =
  paths[P][M] extends infer O extends Record<string, unknown> ? Required<FetchResponse<O, I, MediaType>>["data"] : never;
/** The options, optional when nothing in them is required. */
type InitArgs<I> = RequiredKeysOf<I> extends never ? [init?: I] : [init: I];

type Call<M extends Method> = <P extends PathsWithMethod<paths, M>, I extends Init<M, P>>(path: P, ...init: InitArgs<I>) => Promise<Reply<M, P, I>>;

export interface ApiClient extends OpenapiQueryClient<paths> {
  get: Call<"get">;
  post: Call<"post">;
  put: Call<"put">;
  patch: Call<"patch">;
  delete: Call<"delete">;
  /** The reply itself, unread, for streams and files. Throws AuthError on 401 and ApiError on other failures. */
  raw<M extends Method, P extends PathsWithMethod<paths, M>, I extends Init<M, P>>(method: M, path: P, ...init: InitArgs<I>): Promise<Response>;
}

/** FastAPI's "detail": a message, or a list of validation errors. */
function fmtDetail(detail: unknown): string {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((d: { loc?: (string | number)[]; msg?: string }) => {
        const where = (d.loc ?? []).slice(1).join(".");
        return where ? `${where}: ${d.msg}` : String(d.msg);
      })
      .join("; ");
  }
  return JSON.stringify(detail);
}

/** One path parameter: each "/"-separated part encoded, so a model ID's slash stays a slash (``{model_id:path}``). */
function encodeParam(value: unknown): string {
  return String(value)
    .split("/")
    .map((part) => (part === "." || part === ".." ? part.replace(/\./g, "%2E") : encodeURIComponent(part)))
    .join("/");
}

/** ``template``'s ``{name}``s filled from ``path``; a value left out is a mistake in the page, not a request. */
export function fillPath(template: string, path: Record<string, unknown> = {}): string {
  return template.replace(/\{(\w+)\}/g, (_, name: string) => {
    const value = path[name];
    if (value === undefined || value === null) throw new Error(`${template}: no value for {${name}}`);
    return encodeParam(value);
  });
}

/** A query as the client sends it: unset values left out, an array's values each under its key. */
export const queryString: (query: Record<string, unknown>) => string = createQuerySerializer();

/**
 * Sends ``getKey()`` as X-API-Key, and turns a failed reply into an error the
 * pages catch: AuthError on 401 (after ``onUnauthorized``, told whether a key
 * was sent), ApiError with the server's detail otherwise.
 */
function access(getKey: () => string, onUnauthorized: (hadKey: boolean) => void): Middleware {
  return {
    onRequest({ request }) {
      const key = getKey();
      if (key) request.headers.set("X-API-Key", key);
    },
    async onResponse({ request, response }) {
      if (response.status === 401) {
        onUnauthorized(request.headers.has("X-API-Key"));
        throw new AuthError("Not signed in");
      }
      if (!response.ok) {
        let detail: unknown = null;
        try {
          detail = (JSON.parse(await response.text()) as { detail?: unknown }).detail;
        } catch {
          /* not JSON */
        }
        throw new ApiError(detail ? fmtDetail(detail) : `HTTP ${response.status}`, response.status);
      }
      // No route answers with a page: one here came from something in between (a proxy, or the dev server's
      // fallback for a path it does not forward), and would otherwise reach the caller as if it were the reply.
      if (/^text\/html/i.test(response.headers.get("Content-Type") ?? "")) {
        throw new ApiError(`Expected an API reply, got a web page (HTTP ${response.status})`, response.status);
      }
    },
  };
}

/**
 * A client of the API: ``get``/``post``/... answer with the reply's body and
 * throw on a failure, and the hooks of openapi-react-query (``useQuery``,
 * ``queryOptions``...) load into the page's TanStack Query cache, keyed by
 * method, path and options. ``fixed`` headers go on every request (the
 * playground tags its traces with one); ``transport`` is for tests (an
 * origin, since outside a page a URL cannot be relative, and a fetch).
 */
export function createClient(
  getKey: () => string,
  onUnauthorized: (hadKey: boolean) => void,
  fixed: Record<string, string> = {},
  transport: { baseUrl?: string; fetch?: (request: Request) => Promise<Response> } = {},
): ApiClient {
  const client = createFetchClient<paths>({ headers: fixed, pathSerializer: fillPath, ...transport });
  client.use(access(getKey, onUnauthorized));

  // The access middleware has thrown for every failure, so a reply that comes back is the body asked for.
  const call =
    (method: Method) =>
    async (path: string, init?: object): Promise<unknown> =>
      (await client.request(method, path as never, init as never)).data;
  return {
    ...createQueryHooks(client),
    get: call("get"),
    post: call("post"),
    put: call("put"),
    patch: call("patch"),
    delete: call("delete"),
    raw: async (method: Method, path: string, init?: object) =>
      (await client.request(method, path as never, { ...init, parseAs: "stream" } as never)).response,
  } as ApiClient;
}
