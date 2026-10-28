import type { ApiClient, Schemas } from "../shared/api";

/** One chunk of a streamed /v1/chat/completions reply. */
export interface ChatChunk {
  choices?: { delta?: { content?: string } }[];
  /** Non-standard: on the last chunk, the server time and whether the model had to load first. */
  toolkit?: { processing_ms: number; cold_start: boolean };
  /** On the last chunk, when the model counts tokens. */
  usage?: { prompt_tokens: number; completion_tokens: number; total_tokens: number };
}

/** The part of POST /v1/chat/completions the chat sends. */
export type ChatRequest = Required<Pick<Schemas["ChatCompletionRequest"], "model" | "messages" | "temperature" | "max_tokens">>;

/**
 * Splits server-sent events as text arrives, in pieces that may cut an event
 * anywhere. ``push`` returns the ``data:`` payloads of every event completed so far.
 */
export function sseParser(): { push(text: string): string[] } {
  let buffer = "";
  return {
    push(text) {
      buffer += text;
      const data: string[] = [];
      let end;
      while ((end = buffer.indexOf("\n\n")) >= 0) {
        const event = buffer.slice(0, end);
        buffer = buffer.slice(end + 2);
        for (const line of event.split("\n")) {
          if (line.startsWith("data: ")) data.push(line.slice("data: ".length));
        }
      }
      return data;
    },
  };
}

/**
 * POSTs a streamed chat completion and calls ``onChunk`` with each chunk.
 * Errors before the stream starts come back as HTTP errors (thrown by the
 * client), later ones as an error event, thrown here.
 */
export async function streamChat(
  client: ApiClient,
  request: ChatRequest,
  signal: AbortSignal,
  onChunk: (chunk: ChatChunk) => void,
): Promise<void> {
  const response = await client.raw("post", "/v1/chat/completions", { body: { ...request, stream: true }, signal });
  const reader = response.body!.pipeThrough(new TextDecoderStream()).getReader();
  const parser = sseParser();
  for (;;) {
    const { value, done } = await reader.read();
    if (done) return;
    for (const data of parser.push(value)) {
      if (data === "[DONE]") return;
      const message = JSON.parse(data) as ChatChunk & { error?: { message: string } };
      if (message.error) throw new Error(message.error.message);
      onChunk(message);
    }
  }
}
