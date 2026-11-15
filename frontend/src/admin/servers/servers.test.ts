import { describe, expect, it } from "vitest";
import type { ModelEntry } from "../../shared/types";
import {
  backendForm,
  backendFromForm,
  backendLabel,
  emptyBackend,
  emptyEntry,
  entryForm,
  entryFromForm,
  HOSTED_PROVIDER_PRESETS,
  locationFromHash,
  PROVIDER_PRESETS,
  providerNameProblem,
  serversHash,
  shellJoin,
  shellSplit,
  validateEntry,
} from "./servers";

const entry: ModelEntry = {
  task: "vision-language",
  description: "Qwen3.5 4B",
  tags: ["chat", "image"],
  backends: [
    { provider: "ollama", upstream_model: "qwen3.5:4b" },
    { cmd: ["mlx_vlm.server", "--model", "mlx-community/Qwen3.5-4B-4bit", "--port", "{port}"], memory_gb: 3.5, env: { HF_HOME: "/data/hf" } },
    { hub: "Qwen/Qwen3.5-4B", extra: { chat_template_kwargs: { enable_thinking: false } } },
  ],
};

describe("the location in the hash", () => {
  it("reads the view", () => {
    expect(locationFromHash("#/servers")).toEqual({ view: "overview" });
    expect(locationFromHash("#/servers/new")).toEqual({ view: "new" });
    expect(locationFromHash("#/servers/new?dup=qwen3.5-4b")).toEqual({ view: "new", dup: "qwen3.5-4b" });
    expect(locationFromHash("#/servers/model/local%2Fchat")).toEqual({ view: "model", id: "local/chat" });
    expect(locationFromHash("#/enrichers/x")).toEqual({ view: "overview" });
  });

  it("round-trips, slashes in IDs included", () => {
    for (const loc of [{ view: "overview" }, { view: "new" }, { view: "new", dup: "a/b" }, { view: "model", id: "org/m:4b" }] as const) {
      expect(locationFromHash(serversHash(loc))).toEqual(loc);
    }
  });
});

describe("shell words", () => {
  it("split like shlex", () => {
    expect(shellSplit(`llama-server -hf a/b:Q4 --chat-template-kwargs '{"enable_thinking":false}' --port {port}`)).toEqual([
      "llama-server", "-hf", "a/b:Q4", "--chat-template-kwargs", '{"enable_thinking":false}', "--port", "{port}",
    ]);
    expect(shellSplit(`a "b c" d\\ e ''`)).toEqual(["a", "b c", "d e", ""]);
    expect(() => shellSplit("a 'b")).toThrow("unclosed");
  });

  it("join so they split back the same", () => {
    for (const words of [["a", "b c", "it's", "", "{port}", '{"x":1}'], ["mlx_lm.server", "--model", "org/m-4bit"]]) {
      expect(shellSplit(shellJoin(words))).toEqual(words);
    }
    expect(shellJoin(["mlx_lm.server", "--port", "{port}"])).toBe("mlx_lm.server --port {port}");
  });
});

describe("the entry form", () => {
  it("round-trips an entry, giving only what was given", () => {
    const form = entryForm(entry, "qwen3.5-4b");
    expect(form.backends.map((b) => b.kind)).toEqual(["provider", "cmd", "hub"]);
    expect(form.backends[1].cmd).toBe("mlx_vlm.server --model mlx-community/Qwen3.5-4B-4bit --port {port}");
    expect(form.tags).toBe("chat, image");
    expect(validateEntry(form, ["ollama"])).toEqual([]);
    expect(entryFromForm(form)).toEqual(entry);
  });

  it("drops what the backend's kind does not take", () => {
    const form = { ...backendForm(entry.backends[1]), kind: "url" as const, url: "http://gpu:8000" };
    expect(backendFromForm(form)).toEqual({ url: "http://gpu:8000", memory_gb: 3.5 });
  });

  it("starts a new entry on the first provider", () => {
    expect(emptyEntry("ollama").backends).toEqual([emptyBackend("provider", "ollama")]);
    expect(emptyEntry().backends[0].kind).toBe("url");
  });

  it("says what stops it being saved", () => {
    const form = entryForm(entry, "bad id");
    form.backends[0].provider = "nope";
    form.backends[1].cmd = "a 'b";
    form.backends[1].max_concurrency = "0.5";
    form.backends[2].extra = "[1]";
    form.backends[2].on_evict = "sleep";
    expect(validateEntry(form, ["ollama"])).toEqual([
      "The model ID is letters, digits and ._:/@+-, up to 200",
      "Backend 1: there is no provider named nope",
      "Backend 2: The command has an unclosed '",
      "Backend 2: Concurrency is a whole number from 1 to 1024",
      "Backend 3: sleep on evict is for commands (vLLM)",
      "Backend 3: Extra fields are a JSON object",
    ]);
    expect(validateEntry({ ...emptyEntry(), model_id: "m", backends: [] }, [])).toEqual(["Add at least one backend"]);
  });

  it("keeps a hub backend's load settings, and only a hub backend's", () => {
    const hub: ModelEntry = { task: "rerank", backends: [{ hub: "BAAI/bge-reranker-v2-m3", revision: "953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e", max_length: 512 }] };
    const form = entryForm(hub, "bge-rerank");
    expect(validateEntry(form, [])).toEqual([]);
    expect(entryFromForm(form)).toEqual(hub);
    // Another kind drops them.
    expect(backendFromForm({ ...form.backends[0], kind: "url", url: "http://gpu:8000" })).toEqual({ url: "http://gpu:8000" });
    form.backends[0].revision = "-main";
    form.backends[0].max_length = "0";
    expect(validateEntry(form, [])).toEqual([
      "Backend 1: the revision is a branch, tag or commit",
      "Backend 1: Max length is a whole number from 1 to 1048576",
    ]);
  });
});

describe("the provider's name", () => {
  it("is lower case letters, digits, _ and -", () => {
    expect(providerNameProblem("ollama")).toBe("");
    expect(providerNameProblem("lm-studio_2")).toBe("");
    expect(providerNameProblem("LM Studio")).toMatch(/lower case/);
    expect(providerNameProblem("")).toMatch(/lower case/);
  });
});

describe("the provider presets", () => {
  it("each have a name the API takes, their own, and a base URL without /v1", () => {
    const names = PROVIDER_PRESETS.map(([, name]) => name);
    expect(names.map(providerNameProblem).filter(Boolean)).toEqual([]);
    expect(new Set(names).size).toBe(names.length);
    // The client adds /v1/... to the URL itself.
    for (const [, , settings] of PROVIDER_PRESETS) expect(settings.url).toMatch(/^http:\/\/127\.0\.0\.1:\d+$/);
  });

  it("ask only Ollama to unload a model, the one server that understands it", () => {
    expect(PROVIDER_PRESETS.filter(([, , s]) => s.on_evict === "unload").map(([, name]) => name)).toEqual(["ollama"]);
  });
});

describe("the hosted provider presets", () => {
  it("have names of their own and send their key over https, to a URL without /v1", () => {
    const names = [...PROVIDER_PRESETS, ...HOSTED_PROVIDER_PRESETS].map(([, name]) => name);
    expect(names.map(providerNameProblem).filter(Boolean)).toEqual([]);
    expect(new Set(names).size).toBe(names.length);
    for (const [, , settings] of HOSTED_PROVIDER_PRESETS) {
      expect(settings.url).toMatch(/^https:\/\//);
      expect(settings.url).not.toMatch(/\/v1\/?$/);
    }
  });
});

describe("labels", () => {
  it("name a backend in a few words", () => {
    expect(entry.backends.map(backendLabel)).toEqual(["ollama", "mlx_vlm.server", "in-process Qwen/Qwen3.5-4B"]);
    expect(backendLabel({ url: "http://gpu-box:8000" })).toBe("gpu-box:8000");
  });
});
