import { useEffect, useRef, useState, type ComponentType, type FormEvent } from "react";
import { CircleAlert, Loader2, Play } from "lucide-react";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useApi, AuthError, type InferenceResponse } from "../api";
import { CodeBlock, Disclosure } from "../components";
import { message } from "../errors";
import { fmtMs } from "../format";
import { errorText } from "../query";
import type { Built } from "./build";
import * as inputs from "./inputs";
import * as outputs from "./outputs";
import type { TasksResponse } from "./schema";

interface TaskIO {
  Input: ComponentType<inputs.InputProps>;
  Output: ComponentType<outputs.OutputProps>;
}

// Each built-in task's input and output. Other tasks get a form made from
// their schema (or raw JSON without one) and a generic output.
const IO: Record<string, TaskIO> = {
  "text-generation": { Input: inputs.TextGenerationInput, Output: outputs.TextOutput },
  "vision-language": { Input: inputs.VisionInput, Output: outputs.TextOutput },
  "text-classification": { Input: inputs.ClassificationInput, Output: outputs.ClassificationOutput },
  "zero-shot-classification": { Input: inputs.ZeroShotInput, Output: outputs.ClassificationOutput },
  "question-answering": { Input: inputs.QuestionAnsweringInput, Output: outputs.QuestionAnsweringOutput },
  embedding: { Input: inputs.EmbeddingInput, Output: outputs.EmbeddingOutput },
  "speech-to-text": { Input: inputs.SpeechInput, Output: outputs.TranscriptOutput },
  decision: { Input: inputs.DecisionInput, Output: outputs.DecisionOutput },
  "tabular-prediction": { Input: inputs.TabularInput, Output: outputs.PredictionOutput },
};

function ioFor(task: string, hasContract: boolean): TaskIO {
  return IO[task] ?? { Input: hasContract ? inputs.SchemaFormInput : inputs.JsonInput, Output: outputs.GenericOutput };
}


// Raw responses can be large (embeddings); show the start.
const RAW_LIMIT = 20000;

/**
 * Send ``task`` requests to ``modelId`` through POST /inference: the task's
 * input (checked against its contract and limits before sending), and its
 * result drawn for reading, with the raw response a click away.
 */
export function TryPanel(props: { modelId: string; task: string; loaded: boolean; onRan?(): void }) {
  const client = useApi();
  const { modelId, task } = props;
  // GET /tasks changes only with a restart: asked once per page, every panel sharing the reply.
  const contracts = client.useQuery("get", "/tasks", undefined, { staleTime: Infinity, select: (d): TasksResponse => d });
  const [built, setBuilt] = useState<Built>({ input: null, problem: "" });
  const [running, setRunning] = useState(false);
  const [error, setError] = useState("");
  const [ran, setRan] = useState<{ input: unknown; response: InferenceResponse; ms: number } | null>(null);

  const output = useRef<HTMLElement>(null);
  // A long input (a decision's questions) pushes the result out of view: bring it in.
  useEffect(() => {
    output.current?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }, [ran]);

  if (errorText(contracts.error)) return <p className="text-sm text-critical-text">Could not read the task contracts: {errorText(contracts.error)}</p>;
  const tasks = contracts.data;
  if (!tasks) {
    return (
      <p className="flex items-center gap-2 text-sm text-muted-foreground">
        <Loader2 className="size-4 animate-spin" aria-hidden="true" />
        Loading the task's inputs…
      </p>
    );
  }
  const info = tasks.tasks[task];
  const io = ioFor(task, Boolean(info?.input));

  async function run(event: FormEvent) {
    event.preventDefault();
    if (built.problem) return;
    setRunning(true);
    setError("");
    const started = performance.now();
    const input = built.input;
    try {
      const response = await client.post("/inference", { body: { model_id: modelId, task, input } });
      setRan({ input, response, ms: performance.now() - started });
    } catch (e) {
      if (!(e instanceof AuthError)) setError(message(e));
    } finally {
      setRunning(false);
      props.onRan?.();
    }
  }

  const raw = ran ? JSON.stringify(ran.response, null, 2) : "";
  return (
    <div className="grid gap-4">
      {/* noValidate: the input's own checks (built.problem) decide, with a message; the browser's would block silently. */}
      <form className="grid gap-4" onSubmit={run} noValidate>
        {/* Keyed, so another model or task starts from a fresh form. */}
        <io.Input key={`${task}\n${modelId}`} ctx={{ task, modelId, schema: info?.input ?? null, limits: tasks.limits }} onChange={setBuilt} />
        <div className="flex flex-wrap items-center gap-2.5 pt-0.5">
          <Button type="submit" disabled={running || Boolean(built.problem)}>
            {running ? <Loader2 className="animate-spin" aria-hidden="true" /> : <Play aria-hidden="true" />}
            {running ? "Running…" : "Run"}
          </Button>
          <span className="text-sm text-muted-foreground" aria-live="polite">
            {built.problem || (running ? (props.loaded ? "" : "Loading the model first…") : props.loaded ? "" : "The first run also loads the model.")}
          </span>
        </div>
      </form>

      {error && (
        <Alert variant="destructive" className="animate-in duration-300 fade-in-0">
          <CircleAlert aria-hidden="true" />
          <AlertDescription className="wrap-anywhere">{error}</AlertDescription>
        </Alert>
      )}

      {ran && (
        <section
          ref={output}
          className="grid animate-in gap-3 border-t pt-4 duration-300 fade-in-0 slide-in-from-bottom-2"
          aria-label="Result"
        >
          <div className="flex flex-wrap items-baseline gap-x-2.5 gap-y-1.5 text-sm">
            <h4 className="text-sm font-semibold">Result</h4>
            <span className="text-xs text-muted-foreground tabular-nums">
              {fmtMs(ran.response.processing_time * 1000)} on the server · {fmtMs(ran.ms)} in all
            </span>
            {ran.response.cold_start && (
              <Badge variant="outline" className="font-normal" title="This request loaded the model first, so its time includes the load">
                cold load
              </Badge>
            )}
          </div>
          <io.Output result={ran.response.result} input={ran.input} />
          <Disclosure label="Raw response">
            <CodeBlock className="max-h-80">{raw.length > RAW_LIMIT ? `${raw.slice(0, RAW_LIMIT)}\n… ${raw.length - RAW_LIMIT} more characters` : raw}</CodeBlock>
          </Disclosure>
        </section>
      )}
    </div>
  );
}
