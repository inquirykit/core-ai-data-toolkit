import { describe, expect, it } from "vitest";
import type { ModelEntry } from "../shared/types";
import {
  backendForm,
  backendFromForm,
  backendLabel,
  emptyBackend,
  emptyEntry,
  entryForm,
  entryFromForm,
  locationFromHash,
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
});

describe("the provider's name", () => {
  it("is lower case letters, digits, _ and -", () => {
    expect(providerNameProblem("ollama")).toBe("");
    expect(providerNameProblem("lm-studio_2")).toBe("");
    expect(providerNameProblem("LM Studio")).toMatch(/lower case/);
    expect(providerNameProblem("")).toMatch(/lower case/);
  });
});

describe("labels", () => {
  it("name a backend in a few words", () => {
    expect(entry.backends.map(backendLabel)).toEqual(["ollama", "mlx_vlm.server", "in-process Qwen/Qwen3.5-4B"]);
    expect(backendLabel({ url: "http://gpu-box:8000" })).toBe("gpu-box:8000");
  });
});
