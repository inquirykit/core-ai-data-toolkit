"""
Requests to an OpenAI-compatible server, written by hand over httpx.

An OpenAIClient is one loaded model's connection to the server that answers
it (ServerModel in models/model_server.py makes one when the model loads).
Chat tasks (text-generation, vision-language) go to the server's
/v1/chat/completions (tools and a JSON schema included, as the server's own
``tools`` and ``response_format``), or to /v1/completions when a request asks
for token IDs, log probabilities or echo; embedding goes to /v1/embeddings,
rerank to /v1/rerank and speech-to-text to /v1/audio/transcriptions. A
streamed chat is read from the server's events as they arrive, its tool calls
put back together from their fragments. The answers become the task
contracts of models/tasks.py, with the server's token counts.
"""

import base64
import mimetypes
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .tasks import EmbeddingOutput, GeneratedText, RerankOutput, ToolCall, Transcript


class OpenAIClient:
    """One model's requests to its server at ``base_url``: ``model`` is the name the server knows it by, and
    ``extra`` the backend's own fields, sent with every chat and completion."""

    def __init__(self, base_url: str, model: str, extra: Dict[str, Any], request_timeout: float):
        import httpx

        self.http = httpx.AsyncClient(base_url=base_url, timeout=httpx.Timeout(request_timeout, connect=10.0))
        self.model = model
        self.extra = extra

    @property
    def base_url(self) -> str:
        return str(self.http.base_url)

    async def aclose(self) -> None:
        await self.http.aclose()

    async def embed(self, request: Any) -> EmbeddingOutput:
        response = await self.http.post("/v1/embeddings", json={
            "model": self.model, "input": request.texts, "encoding_format": "float",
        })
        await _raise_for_status(response)
        body = response.json()
        data = sorted(body["data"], key=lambda item: item.get("index", 0))
        return EmbeddingOutput(
            embeddings=[item["embedding"] for item in data],
            tokens=(body.get("usage") or {}).get("prompt_tokens") or 0,
        )

    async def rerank(self, request: Any) -> RerankOutput:
        payload: Dict[str, Any] = {"model": self.model, "query": request.query, "documents": request.documents}
        if request.top_n is not None:
            payload["top_n"] = request.top_n
        response = await self.http.post("/v1/rerank", json=payload)
        await _raise_for_status(response)
        body = response.json()
        results = sorted(body.get("results") or [], key=lambda r: -float(r.get("relevance_score", 0)))
        return RerankOutput(
            results=[{"index": r["index"], "score": r.get("relevance_score", 0)} for r in results],
            tokens=(body.get("usage") or {}).get("total_tokens") or 0,
        )

    async def transcribe(self, request: Any) -> Transcript:
        form = {"model": self.model, "response_format": "json", "temperature": str(request.temperature)}
        if request.language:
            form["language"] = request.language
        if request.prompt:
            form["prompt"] = request.prompt
        response = await self.http.post(
            "/v1/audio/transcriptions", data=form, files={"file": (request.filename, request.audio)},
        )
        await _raise_for_status(response)
        body = response.json()
        return Transcript(text=body.get("text", "").strip(), language=body.get("language") or request.language)

    async def complete(self, request: Any) -> GeneratedText:
        """The server's /v1/completions: token IDs, log probabilities and echo, for evaluation harnesses."""
        payload: Dict[str, Any] = {
            "model": self.model,
            "prompt": request.tokens if request.tokens is not None else request.text,
            "top_p": request.top_p,
            "temperature": request.temperature if request.do_sample else 0.0,
            "echo": request.echo,
        }
        if request.max_length is not None:
            payload["max_tokens"] = request.max_length
        if request.logprobs is not None:
            payload["logprobs"] = request.logprobs
        if request.stop:
            payload["stop"] = request.stop
        response = await self.http.post("/v1/completions", json={**self.extra, **payload})
        await _raise_for_status(response)
        body = response.json()
        choice = body["choices"][0]
        usage = body.get("usage") or {}
        text = choice.get("text") or ""
        logprobs = choice.get("logprobs")
        if request.echo and logprobs and logprobs.get("tokens"):
            # The server echoed the prompt in the text; the contract's text is the new text only.
            prompt_count = usage.get("prompt_tokens") or 0
            prompt_text = "".join(logprobs["tokens"][:prompt_count])
            if text.startswith(prompt_text):
                text = text[len(prompt_text):]
        elif request.echo and request.tokens is None and text.startswith(request.text):
            text = text[len(request.text):]
        return GeneratedText(
            text=text,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            logprobs=logprobs or None,
        )

    async def chat(self, request: Any, on_text: Optional[Callable[[str], Any]]) -> GeneratedText:
        if getattr(request, "logprobs", None) is not None or getattr(request, "echo", False) \
                or getattr(request, "tokens", None) is not None:
            return await self.complete(request)
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": _messages(request),
            "top_p": request.top_p,
            "temperature": request.temperature if request.do_sample else 0.0,
            "stream": on_text is not None,
        }
        if request.max_length:
            payload["max_tokens"] = request.max_length
        if getattr(request, "stop", None):
            payload["stop"] = request.stop
        if on_text is not None:
            # vLLM, llama.cpp and MLX send the token counts in a last chunk when asked.
            payload["stream_options"] = {"include_usage": True}
        # The text-generation input's tools and JSON schema (vision-language has none).
        tools = getattr(request, "tools", None)
        tool_choice = getattr(request, "tool_choice", None)
        if tools and tool_choice != "none":
            payload["tools"] = tools
            if tool_choice in ("auto", "required"):
                payload["tool_choice"] = tool_choice
            elif tool_choice:
                payload["tool_choice"] = {"type": "function", "function": {"name": tool_choice}}
        json_schema = getattr(request, "json_schema", None)
        if json_schema:
            payload["response_format"] = {"type": "json_schema", "json_schema": {"name": "reply", "schema": json_schema}}
        elif json_schema is not None:
            payload["response_format"] = {"type": "json_object"}
        # The backend's own fields (a thinking switch, say); the request's win.
        payload = {**self.extra, **payload}
        if on_text is None:
            response = await self.http.post("/v1/chat/completions", json=payload)
            await _raise_for_status(response)
            body = response.json()
            message = body["choices"][0]["message"]
            return GeneratedText(
                text=message.get("content") or "", tool_calls=_tool_calls(message.get("tool_calls")),
                **_usage_fields(body.get("usage")),
            )

        import json

        text = ""
        usage: Optional[Dict[str, Any]] = None
        calls: Dict[int, Dict[str, Any]] = {}
        # Leaving this block early (on_text said stop) closes the connection,
        # which makes the server stop generating.
        async with self.http.stream("POST", "/v1/chat/completions", json=payload) as response:
            await _raise_for_status(response)
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[len("data:"):].strip()
                if data == "[DONE]":
                    break
                event = json.loads(data)
                usage = event.get("usage") or usage
                choices = event.get("choices") or [{}]
                delta = choices[0].get("delta") or {}
                # Tool calls stream as fragments per call index.
                for fragment in delta.get("tool_calls") or ():
                    call = calls.setdefault(fragment.get("index", 0), {"id": "", "name": "", "arguments": ""})
                    call["id"] = fragment.get("id") or call["id"]
                    function = fragment.get("function") or {}
                    call["name"] += function.get("name") or ""
                    call["arguments"] += function.get("arguments") or ""
                piece = delta.get("content")
                if piece:
                    text += piece
                    if on_text(piece) is False:
                        break
        tool_calls = [
            {"id": c["id"], "function": {"name": c["name"], "arguments": c["arguments"] or "{}"}}
            for _, c in sorted(calls.items())
        ]
        return GeneratedText(text=text, tool_calls=_tool_calls(tool_calls), **_usage_fields(usage))


def _messages(request: Any) -> List[Dict[str, Any]]:
    if request.messages:
        return [turn.model_dump(exclude_none=True) for turn in request.messages]
    content: Any = request.text
    image = getattr(request, "image", None)
    if image:
        mime = mimetypes.guess_type(image)[0] or "image/png"
        data = base64.b64encode(Path(image).read_bytes()).decode()
        content = [
            {"type": "text", "text": request.text},
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}},
        ]
    return [{"role": "user", "content": content}]


async def _raise_for_status(response: Any) -> None:
    if response.status_code < 400:
        return
    body = (await response.aread()).decode(errors="replace")[:500]
    message = f"model server answered {response.status_code}: {body}"
    # The server refused this request (a client error); anything else is ours.
    if 400 <= response.status_code < 500:
        raise ValueError(message)
    raise RuntimeError(message)


def _usage_fields(usage: Optional[Dict[str, Any]]) -> Dict[str, Optional[int]]:
    """The contract's token counts from a server's ``usage`` (missing counts stay unknown)."""
    usage = usage or {}

    def count(name: str) -> Optional[int]:
        value = usage.get(name)
        return int(value) if isinstance(value, (int, float)) and value >= 0 else None

    return {"prompt_tokens": count("prompt_tokens"), "completion_tokens": count("completion_tokens")}


def _tool_calls(raw: Optional[List[Dict[str, Any]]]) -> Optional[List[ToolCall]]:
    """The server's tool calls as the contract's, or None when it made none."""
    calls = []
    for call in raw or ():
        function = call.get("function") or {}
        if function.get("name"):
            calls.append(ToolCall(
                id=call.get("id") or "", function={"name": function["name"], "arguments": function.get("arguments") or "{}"}
            ))
    return calls or None
