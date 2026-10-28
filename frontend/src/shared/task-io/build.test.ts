import { describe, expect, it } from "vitest";
import {
  answerSpan,
  cosine,
  decisionInput,
  decisionSummary,
  defaultGeneration,
  embeddingInput,
  imageProblem,
  questionAnsweringInput,
  rankedScores,
  similarityMatrix,
  speechInput,
  textGenerationInput,
  visionInput,
  type DraftQuestion,
} from "./build";
import type { JSONSchema, TaskLimits } from "./schema";

const limits: TaskLimits = {
  max_input_chars: 20,
  max_generation_length: 1024,
  max_media_bytes: 1000,
  max_media_parts: 8,
  max_audio_bytes: 100,
  max_embedding_inputs: 3,
};
const schema: JSONSchema = {
  properties: {
    max_length: { anyOf: [{ type: "integer", minimum: 1, maximum: 1024 }, { type: "null" }] },
    temperature: { type: "number", exclusiveMinimum: 0, maximum: 5, default: 0.7 },
    top_p: { type: "number", exclusiveMinimum: 0, maximum: 1, default: 0.9 },
    do_sample: { type: "boolean", default: true },
  },
};
const settings = defaultGeneration(schema);

describe("generation inputs", () => {
  it("send the prompt with checked settings", () => {
    expect(settings).toEqual({ maxLength: "128", temperature: "0.7", topP: "0.9", sample: true });
    expect(textGenerationInput("Hi", settings, schema, limits)).toEqual({
      input: { text: "Hi", max_length: 128, temperature: 0.7, top_p: 0.9, do_sample: true },
      problem: "",
    });
    expect(textGenerationInput("", settings, schema, limits).problem).toMatch(/empty/);
    expect(textGenerationInput("x".repeat(21), settings, schema, limits).problem).toMatch(/longer than 20/);
    expect(textGenerationInput("Hi", { ...settings, maxLength: "5000" }, schema, limits).problem).toMatch(/at most 1024/);
  });

  it("leave sampling settings out when not sampling, and max tokens out when empty", () => {
    const greedy = { ...settings, sample: false, temperature: "", maxLength: "" };
    expect(textGenerationInput("Hi", greedy, schema, limits).input).toEqual({ text: "Hi", do_sample: false });
  });

  it("put an image in a chat part", () => {
    const built = visionInput("What is it?", "data:image/png;base64,AAAA", settings, schema, limits);
    expect(built.input).toMatchObject({
      text: "What is it?",
      messages: [{ role: "user", content: [{ type: "text", text: "What is it?" }, { type: "image_url", image_url: { url: "data:image/png;base64,AAAA" } }] }],
    });
    expect(visionInput("Hi", null, settings, schema, limits).input).not.toHaveProperty("messages");
  });
});

describe("other inputs", () => {
  it("embed one text per line", () => {
    expect(embeddingInput("a\n\n b \n", limits).input).toEqual({ texts: ["a", "b"] });
    expect(embeddingInput("a\nb\nc\nd", limits).problem).toMatch(/At most 3/);
    expect(embeddingInput(" \n", limits).problem).toMatch(/at least one/);
  });

  it("check the question and the context", () => {
    expect(questionAnsweringInput("Who?", "Bob did.", limits).input).toEqual({ question: "Who?", context: "Bob did." });
    expect(questionAnsweringInput("Who?", "", limits).problem).toMatch(/context is empty/);
  });

  it("send audio as base64 with its name", () => {
    const audio = { name: "a.wav", url: "data:audio/wav;base64,UklG", bytes: 50 };
    expect(speechInput(audio, " en ", "", limits).input).toEqual({ audio: "UklG", filename: "a.wav", language: "en" });
    expect(speechInput({ ...audio, bytes: 500 }, "", "", limits).problem).toMatch(/larger than 100 B/);
    expect(speechInput(null, "", "", limits).problem).toMatch(/Choose/);
  });

  it("check image type and size", () => {
    expect(imageProblem({ type: "image/png", size: 10 }, ["png", "jpeg"], 1000)).toBe("");
    expect(imageProblem({ type: "image/gif", size: 10 }, ["png", "jpeg"], 1000)).toMatch(/PNG, JPEG/);
    expect(imageProblem({ type: "image/png", size: 5000 }, ["png"], 1000)).toMatch(/larger than 5 KB|larger than 1000 B/);
  });
});

describe("decisions", () => {
  const q = (extra: Partial<DraftQuestion>): DraftQuestion => ({ name: "q", type: "choice", instructions: "", options: "a\nb", ...extra });

  it("turn drafts into vegaml questions", () => {
    const built = decisionInput("state", null, [
      q({ name: "team" }),
      q({ name: "urgent", type: "boolean", instructions: "Is it urgent?" }),
      q({ name: "level", type: "score", options: "low\nhigh" }),
    ], limits);
    expect(built.problem).toBe("");
    expect(built.input).toEqual({
      state: "state",
      questions: {
        team: { type: "choice", instructions: "", criteria: { a: null, b: null } },
        urgent: { type: "boolean", instructions: "Is it urgent?", criteria: null },
        level: { type: "score", instructions: "", criteria: ["low", "high"] },
      },
    });
  });

  it("send an image in place of the state", () => {
    const image = { name: "x.png", url: "data:image/png;base64,QUJD", bytes: 3 };
    expect(decisionInput("", image, [q({})], limits).input).toEqual({ image: "QUJD", questions: { q: { type: "choice", instructions: "", criteria: { a: null, b: null } } } });
  });

  it("say what a draft is missing", () => {
    expect(decisionInput("s", null, [], limits).problem).toMatch(/Add a question/);
    expect(decisionInput("s", null, [q({ name: "" })], limits).problem).toMatch(/needs a name/);
    expect(decisionInput("s", null, [q({}), q({})], limits).problem).toMatch(/Two questions/);
    expect(decisionInput("s", null, [q({ type: "boolean" })], limits).problem).toMatch(/needs instructions/);
    expect(decisionInput("s", null, [q({ options: "" })], limits).problem).toMatch(/needs options/);
    expect(decisionInput("s", null, [q({ type: "score", options: Array(11).fill("x").join("\n") })], limits).problem).toMatch(/1 to 10/);
    expect(decisionInput("", null, [q({})], limits).problem).toMatch(/state is empty/);
  });

  it("read answers for display", () => {
    expect(decisionSummary({ choice: "b", probs: { a: 0.2, b: 0.8 }, prediction_set: ["b", "a"] })).toEqual({
      headline: "b",
      probs: [["b", 0.8], ["a", 0.2]],
      flags: ["could be any of: b, a"],
    });
    expect(decisionSummary({ boolean: 0.3, probs: { False: 0.7, True: 0.3 }, abstain: true }).headline).toBe("false");
    expect(decisionSummary({ expected_score: 1.234, probs: {} }).headline).toBe("score 1.23");
  });
});

describe("output maths", () => {
  it("ranks scores", () => {
    expect(rankedScores({ NEG: 0.1, POS: 0.9 })).toEqual([["POS", 0.9], ["NEG", 0.1]]);
  });

  it("finds the answer in the context", () => {
    expect(answerSpan("Paris is the capital.", "paris")).toEqual(["", "Paris", " is the capital."]);
    expect(answerSpan("Paris", "Rome")).toBeNull();
  });

  it("compares vectors", () => {
    expect(cosine([1, 0], [0, 1])).toBe(0);
    expect(cosine([1, 1], [2, 2])).toBeCloseTo(1);
    expect(cosine([0, 0], [1, 1])).toBe(0);
    expect(similarityMatrix([[1, 0], [0, 1]])).toEqual([[1, 0], [0, 1]]);
  });
});
