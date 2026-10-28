"""
A stand-in OpenAI-compatible model server for tests/test_model_server.py.

    python fake_model_server.py --port 1234 [--fail]

It answers /v1/chat/completions with "echo: <last user text>" (streamed
word by word when asked; a call of the first tool when given tools, and a
JSON object when given a response_format), /v1/embeddings, /v1/rerank and
/v1/audio/transcriptions, and supports vLLM's /sleep and /wake_up and
Ollama's unload (/api/generate with keep_alive 0). A chat's top_k shows in
the reply. A user message "crash" makes it exit mid-request; --fail makes it
exit at once.
"""

import argparse
import email
import email.policy
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE = {"asleep": False, "requests": 0, "unloaded": []}


def last_user_text(messages):
    content = [m for m in messages if m["role"] == "user"][-1]["content"]
    if isinstance(content, str):
        return content
    kinds = [p["type"] for p in content if p["type"] != "text"]
    text = " ".join(p["text"] for p in content if p["type"] == "text")
    return f"{text} [{', '.join(kinds)}]" if kinds else text


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _json(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/v1/models":
            return self._json(200, {"data": [{"id": "fake"}], "pid": os.getpid(), **STATE})
        self._json(404, {"error": "not found"})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path == "/v1/audio/transcriptions":
            return self._transcribe(raw)
        body = json.loads(raw or b"{}")
        if self.path.startswith("/sleep"):
            STATE["asleep"] = True
            return self._json(200, {})
        if self.path == "/wake_up":
            STATE["asleep"] = False
            return self._json(200, {})
        if self.path == "/api/generate" and body.get("keep_alive") == 0:
            STATE["unloaded"].append(body["model"])
            return self._json(200, {"done": True})
        if self.path == "/v1/embeddings":
            return self._json(200, {
                "data": [{"index": i, "embedding": [float(len(t)), 1.0]} for i, t in reversed(list(enumerate(body["input"])))],
                "usage": {"prompt_tokens": 3},
            })
        if self.path == "/v1/rerank":
            # Longer documents score higher; answered in request order, so the client must sort.
            results = [{"index": i, "relevance_score": float(len(d))} for i, d in enumerate(body["documents"])]
            if body.get("top_n"):
                results = sorted(results, key=lambda r: -r["relevance_score"])[: body["top_n"]]
            return self._json(200, {"results": results, "usage": {"total_tokens": 5}})
        if self.path == "/v1/completions":
            return self._complete(body)
        if self.path != "/v1/chat/completions":
            return self._json(404, {"error": "not found"})
        if STATE["asleep"]:
            return self._json(503, {"error": "asleep"})
        STATE["requests"] += 1
        text = last_user_text(body["messages"])
        if text == "crash":
            os._exit(3)
        if text == "bad":
            return self._json(400, {"error": "bad request"})
        reply = f"echo: {text} (model={body['model']}, max_tokens={body.get('max_tokens')})"
        if "top_k" in body:
            reply += f" top_k={body['top_k']}"
        if body.get("tools"):
            name = body["tools"][0]["function"]["name"]
            call = {"id": "call_fake", "type": "function", "function": {"name": name, "arguments": json.dumps({"q": text})}}
            message = {"role": "assistant", "content": None, "tool_calls": [call]}
            if not body.get("stream"):
                return self._json(200, {"choices": [{"message": message, "finish_reason": "tool_calls"}]})
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            # Arguments arrive in fragments, as vLLM sends them.
            head = {"index": 0, "id": "call_fake", "type": "function", "function": {"name": name, "arguments": ""}}
            for fragment in (head, {"index": 0, "function": {"arguments": call["function"]["arguments"]}}):
                self.wfile.write(f"data: {json.dumps({'choices': [{'delta': {'tool_calls': [fragment]}}]})}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            return
        if body.get("response_format"):
            reply = json.dumps({"echo": text, "format": body["response_format"]["type"]})
        usage = {"prompt_tokens": 3, "completion_tokens": len(reply.split(" ")), "total_tokens": 3 + len(reply.split(" "))}
        if not body.get("stream"):
            return self._json(200, {"choices": [{"message": {"role": "assistant", "content": reply}}], "usage": usage})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for word in reply.split(" "):
            chunk = {"choices": [{"delta": {"content": word + " "}}]}
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
        if (body.get("stream_options") or {}).get("include_usage"):
            # As vLLM does: a last chunk with no choices and the usage.
            self.wfile.write(f"data: {json.dumps({'choices': [], 'usage': usage})}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")

    def _complete(self, body):
        """/v1/completions, vLLM style: tokens carry their own spaces, so joining them gives the text.

        A text prompt's tokens are its words; a token-ID prompt's are 't<id>'. The
        completion is " echo:" and the first prompt token, with made-up log probabilities.
        """
        import re

        prompt = body.get("prompt", "")
        if isinstance(prompt, list):
            prompt_tokens = [f"t{i}" if n == 0 else f" t{i}" for n, i in enumerate(prompt)]
        else:
            prompt_tokens = re.findall(r"\s*\S+", prompt)
        new = ([" echo:"] + [" " + prompt_tokens[0].strip()] if prompt_tokens else [" echo:"])[: body.get("max_tokens", 16)]
        tokens = (prompt_tokens if body.get("echo") else []) + new
        choice = {"index": 0, "text": "".join(tokens), "finish_reason": "stop", "logprobs": None}
        if body.get("logprobs") is not None:
            logprobs = [None if (body.get("echo") and i == 0) else -0.5 * (i + 1) for i in range(len(tokens))]
            top = [None if lp is None else {tok: lp, "other": lp - 1} for tok, lp in zip(tokens, logprobs)]
            offsets, at = [], 0
            for tok in tokens:
                offsets.append(at)
                at += len(tok)
            choice["logprobs"] = {"tokens": tokens, "token_logprobs": logprobs, "top_logprobs": top, "text_offset": offsets}
        usage = {"prompt_tokens": len(prompt_tokens), "completion_tokens": len(new), "total_tokens": len(prompt_tokens) + len(new)}
        self._json(200, {"choices": [choice], "usage": usage})

    def _transcribe(self, raw):
        # Multipart form: parse it with the email package (cgi is gone).
        message = email.message_from_bytes(
            f"Content-Type: {self.headers['Content-Type']}\r\n\r\n".encode() + raw, policy=email.policy.HTTP)
        fields = {part.get_param("name", header="content-disposition"): part for part in message.iter_parts()}
        audio = fields["file"].get_content()
        text = f"{len(audio)} bytes of {fields['file'].get_filename()} for {fields['model'].get_content()}"
        language = fields["language"].get_content() if "language" in fields else None
        self._json(200, {"text": f" {text} ", "language": language})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--fail", action="store_true")
    args = parser.parse_args()
    if args.fail:
        sys.exit(7)
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
