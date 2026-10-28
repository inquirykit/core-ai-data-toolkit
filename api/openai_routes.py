"""
OpenAI-compatible routes, so OpenAI clients (SDKs, chat front ends,
evaluation harnesses) can use the toolkit like any local LLM server.

Only the subset those clients need: GET /v1/models (plus non-standard ``task`` and ``status``
fields per model), POST /v1/chat/completions (plain and streamed),
POST /v1/completions (the legacy prompt form: token IDs in, ``logprobs`` and
``echo`` out, which evaluation harnesses such as lm-evaluation-harness
need), POST /v1/embeddings, POST /v1/rerank and POST /v1/audio/transcriptions.
Message content may be a string or a list of text, image_url and
input_audio parts.

Tools (``tools``, ``tool_choice``) and JSON replies (``response_format``:
json_object or json_schema) work with text-generation models: a chat
template that takes tools renders them, other models get an instruction,
and the reply is parsed for calls or JSON (models/toolcalls.py); a model
server does this itself. Decoding is not constrained, so a reply that is
not the JSON asked for (or does not match the schema) is a 502. A streamed
reply with tools or a response format arrives in one chunk at the end.

The model gets the chat twice, in the text-generation input
(models/tasks.py): as a plain transcript in ``text`` (which every
text-generation model understands) and as ``messages``, which a model can use
to apply its own chat template (TransformersModel does). Streamed replies
arrive as the model writes them when its predict() names ``on_text``
(TransformersModel does), else as one chunk.

Images (``image_url`` parts with base64 data URLs; remote URLs are refused)
and audio (``input_audio`` parts) make a chat a vision-language request, as
does a model registered for vision-language: those models get the chat with
its parts unchanged (OpenAI's shape) in ``messages``. A model that only does
text-generation refuses them. The reply
(the final chunk, when streamed) also carries a non-standard ``toolkit`` field
with the server-side time and whether the model had to be loaded first, and
``usage`` with the token counts when the model reports them.
"""

import base64
import json
import struct
import time
import uuid
from typing import Any, Dict, List, Literal, Optional, Union

import jsonschema
from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator

from config import Config
from models import registry, toolcalls
from models.chat import (
    STOP_MARKERS, ChatMessage, ReplyStream, build_prompt, completion_text, cut_at_stop, stop_list, trim_messages,
    vision_messages,
)
from errors import Refused, ServiceError, TooLarge
from . import deps, responses
from .streaming import Chunks, begin_stream, stream_events

router = APIRouter(prefix="/v1", dependencies=deps.CLIENT)

CHAT_TASK = "text-generation"
VISION_TASK = "vision-language"
DEFAULT_MAX_TOKENS = 256


class FunctionDefinition(BaseModel):
    model_config = {"extra": "ignore"}

    name: str = Field(..., min_length=1, max_length=256)
    description: Optional[str] = Field(None, max_length=Config.MAX_INPUT_CHARS)
    parameters: Optional[Dict[str, Any]] = None


class Tool(BaseModel):
    model_config = {"extra": "ignore"}

    type: Literal["function"]
    function: FunctionDefinition


class JSONSchemaFormat(BaseModel):
    model_config = {"extra": "ignore", "populate_by_name": True}

    name: str = Field("reply", max_length=256)
    json_schema: Optional[Dict[str, Any]] = Field(None, alias="schema")

    @field_validator("json_schema")
    @classmethod
    def a_valid_schema(cls, schema: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """A schema that is no JSON Schema is the client's mistake (422), found before the model runs; otherwise the
        reply's check would blame the model (502)."""
        if schema is not None:
            try:
                jsonschema.validators.validator_for(schema).check_schema(schema)
            except jsonschema.SchemaError as e:
                raise ValueError(f"schema is not a valid JSON Schema: {e.message}") from None
        return schema


class ResponseFormat(BaseModel):
    model_config = {"extra": "ignore"}

    type: Literal["text", "json_object", "json_schema"]
    json_schema: Optional[JSONSchemaFormat] = None


def include_usage(stream_options: Optional[Dict[str, Any]]) -> bool:
    return bool(stream_options and stream_options.get("include_usage"))


class ChatCompletionRequest(BaseModel):
    # parallel_tool_calls, seed, user, etc. are accepted and ignored.
    model_config = {"extra": "ignore", "protected_namespaces": ()}

    model: str = Field(..., min_length=1, max_length=256)
    messages: List[ChatMessage] = Field(..., min_length=1)
    stream: bool = False
    # {"include_usage": true} adds OpenAI's closing chunk with the token counts.
    stream_options: Optional[Dict[str, Any]] = None
    temperature: Optional[float] = Field(None, ge=0, le=2)
    top_p: Optional[float] = Field(None, gt=0, le=1)
    max_tokens: Optional[int] = Field(None, ge=1, le=Config.MAX_GENERATION_LENGTH)
    stop: Optional[Union[str, List[str]]] = None
    # OpenAI's chat form: logprobs true, top_logprobs the alternatives per token.
    logprobs: Optional[bool] = None
    top_logprobs: Optional[int] = Field(None, ge=0, le=20)
    tools: Optional[List[Tool]] = Field(None, max_length=128)
    # "none", "auto", "required" or {"type": "function", "function": {"name": ...}}
    tool_choice: Optional[Union[Literal["none", "auto", "required"], Dict[str, Any]]] = None
    response_format: Optional[ResponseFormat] = None

    @field_validator("stop")
    @classmethod
    def valid_stop(cls, value: Any) -> Optional[List[str]]:
        return stop_list(value)

    @property
    def active_tools(self) -> Optional[List[Dict[str, Any]]]:
        """The tools the model may call: none with tool_choice "none"."""
        if not self.tools or self.tool_choice == "none":
            return None
        return [t.model_dump(exclude_none=True) for t in self.tools]

    @property
    def choice(self) -> Optional[str]:
        """tool_choice for the task input: "auto", "required" or a function name."""
        if self.active_tools is None:
            return None
        if isinstance(self.tool_choice, dict):
            name = (self.tool_choice.get("function") or {}).get("name")
            if not isinstance(name, str) or name not in toolcalls.tool_names(self.active_tools):
                raise Refused("tool_choice names a function that is not in tools")
            return name
        return self.tool_choice

    @property
    def json_schema(self) -> Optional[Dict[str, Any]]:
        """The schema a JSON reply must match ({} for any JSON), or None for a text reply."""
        if self.response_format is None or self.response_format.type == "text":
            return None
        if self.response_format.type == "json_object":
            return {}
        return (self.response_format.json_schema.json_schema if self.response_format.json_schema else None) or {}

    @property
    def structured(self) -> bool:
        return self.active_tools is not None or self.json_schema is not None


def chat_task(request: ChatCompletionRequest) -> str:
    """vision-language for models registered for it, and for chats with media to models nobody claims."""
    claimed = registry.claimed_tasks(request.model)
    has_media = any(m.has_media for m in request.messages)
    if VISION_TASK in claimed or (has_media and not claimed):
        return VISION_TASK
    if has_media:
        raise Refused(f"{request.model} does not take images or audio")
    return CHAT_TASK


def generation_input(request: ChatCompletionRequest, task: str = CHAT_TASK) -> Dict[str, Any]:
    """The text-generation (or vision-language) input for this chat."""
    tools, choice, json_schema = request.active_tools, request.choice, request.json_schema
    if task == VISION_TASK and request.structured:
        raise Refused("tools and response_format need a text-generation model")
    kwargs: Dict[str, Any] = {
        # The transcript carries the instruction itself: it is for models without a chat template.
        "text": build_prompt(request.messages, toolcalls.instructions(tools, choice, json_schema)),
        "messages": vision_messages(request.messages) if task == VISION_TASK else trim_messages(request.messages),
        "max_length": request.max_tokens or min(DEFAULT_MAX_TOKENS, Config.MAX_GENERATION_LENGTH),
        "top_p": request.top_p or 0.9,
    }
    if tools:
        kwargs["tools"] = tools
        if choice:
            kwargs["tool_choice"] = choice
    if json_schema is not None:
        kwargs["json_schema"] = json_schema
    if task == CHAT_TASK:
        # Vision models take neither; the route still cuts the reply at a stop string.
        if request.stop:
            kwargs["stop"] = request.stop
        if request.logprobs:
            kwargs["logprobs"] = request.top_logprobs or 0
    elif request.logprobs:
        raise Refused("logprobs need a text-generation model")
    return {**kwargs, **_sampling(request.temperature)}


def _sampling(temperature: Optional[float]) -> Dict[str, Any]:
    # OpenAI's temperature 0 means greedy; sampling with temperature 0 is invalid.
    if temperature == 0:
        return {"do_sample": False}
    return {"do_sample": True, "temperature": temperature or 0.7}


@router.get("/models", **responses.answering(responses.OpenAIModels))
async def list_models(manager: deps.Manager):
    """Every model the toolkit can serve, tagged with its task and whether it is loaded now.

    ``task`` and ``status`` ("loaded", "loading" or "unloaded") are not
    OpenAI fields. Without ALLOWED_MODELS, models loaded on request but not
    in the catalog (any Hub ID) are included too.
    """
    pairs = [(m, task) for task, ids in manager.get_available_models(Config.ALLOWED_MODELS).items() for m in ids]
    if not Config.ALLOWED_MODELS:
        pairs += [(info["model_id"], info["task"]) for info in manager.get_cache_status()["model_details"].values()]
    data = []
    for model_id, task in dict.fromkeys(pairs):
        data.append({
            "id": model_id, "object": "model", "created": 0, "owned_by": "ai-toolkit",
            "task": task, "status": manager.model_state(model_id, task),
        })
    return {"object": "list", "data": data}


def _toolkit_fields(output: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "processing_ms": round(output["processing_time"] * 1000, 1),
        "cold_start": output.get("cold_start", False),
        "backend": output.get("backend"),
    }


def usage_of(result: Any) -> Optional[Dict[str, int]]:
    """OpenAI's ``usage`` from the result's token counts, or None when the model reports none."""
    prompt = getattr(result, "prompt_tokens", None)
    completion = getattr(result, "completion_tokens", None)
    if prompt is None and completion is None:
        return None
    return {
        "prompt_tokens": prompt or 0,
        "completion_tokens": completion or 0,
        "total_tokens": (prompt or 0) + (completion or 0),
    }


def finish_reason(result: Any, max_length: Optional[int]) -> str:
    """"length" when the model used every token it was allowed, else "stop"."""
    done = getattr(result, "completion_tokens", None)
    return "length" if max_length and done is not None and done >= max_length else "stop"


def _closing_fields(output: Dict[str, Any]) -> Dict[str, Any]:
    """``toolkit`` and, when known, ``usage``: the reply's (or its last chunk's) extra fields."""
    fields: Dict[str, Any] = {"toolkit": _toolkit_fields(output)}
    usage = usage_of(output["result"])
    if usage is not None:
        fields["usage"] = usage
    return fields


def chat_logprobs(result: Any) -> Optional[Dict[str, Any]]:
    """The generated tokens' log probabilities in the chat completions shape."""
    logprobs = getattr(result, "logprobs", None)
    if logprobs is None:
        return None
    tops = logprobs.top_logprobs or [None] * len(logprobs.tokens)

    def entry(token: str, logprob: float) -> Dict[str, Any]:
        return {"token": token, "logprob": logprob, "bytes": list(token.encode())}

    content = []
    for token, logprob, top in zip(logprobs.tokens, logprobs.token_logprobs, tops):
        if logprob is None:  # the first echoed prompt token; chats never echo
            continue
        item = entry(token, logprob)
        item["top_logprobs"] = [entry(k, v) for k, v in (top or {}).items()]
        content.append(item)
    return {"content": content, "refusal": None}


def finish_reply(request: ChatCompletionRequest, output: Dict[str, Any], max_length: Optional[int] = None) -> Dict[str, Any]:
    """The reply's ``message`` and ``finish_reason`` from the model's result.

    Calls the model made (or wrote in its text, for models without native
    tool support) end the reply with "tool_calls"; a JSON reply is extracted
    and checked against the schema (502 when the model did not comply).
    """
    result = output["result"]
    text = completion_text(cut_at_stop(result.text, request.stop))
    calls = [c.model_dump() for c in result.tool_calls] if result.tool_calls else []
    tools = request.active_tools
    if tools and not calls:
        text, found = toolcalls.parse_tool_calls(text, tools)
        calls = [{"function": {"name": name, "arguments": json.dumps(arguments)}} for name, arguments in found]
    extra = {"logprobs": chat_logprobs(result)} if request.logprobs else {}
    if calls:
        for call in calls:
            call["id"] = call.get("id") or f"call_{uuid.uuid4().hex[:24]}"
            call["type"] = "function"
        message = {"role": "assistant", "content": text or None, "tool_calls": calls}
        return {"message": message, "finish_reason": "tool_calls", **extra}
    schema = request.json_schema
    if schema is not None:
        value = toolcalls.extract_json(text)
        if value is None:
            raise ServiceError("The model did not reply with JSON", status=502)
        problem = toolcalls.validate_json(value, schema)
        if problem:
            raise ServiceError(f"The model's JSON does not match the schema: {problem}", status=502)
        text = value
    return {"message": {"role": "assistant", "content": text}, "finish_reason": finish_reason(result, max_length), **extra}


@router.post("/chat/completions")
async def chat_completions(manager: deps.Manager, request: ChatCompletionRequest):
    task = chat_task(request)
    chat_input = generation_input(request, task)
    max_length = chat_input["max_length"]
    completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())

    if not request.stream:
        output = await manager.run_inference(request.model, task, chat_input)
        return {
            "id": completion_id,
            "object": "chat.completion",
            "created": created,
            "model": request.model,
            "choices": [{"index": 0, **finish_reply(request, output, max_length)}],
            **_closing_fields(output),
        }

    chunks = Chunks(completion_id, "chat.completion.chunk", created, request.model, include_usage(request.stream_options))

    def chunk(delta: Dict[str, Any], finish: Optional[str] = None, **extra: Any) -> str:
        return chunks.event({"delta": delta, "finish_reason": finish}, **extra)

    def closing(output: Dict[str, Any], finish: str):
        return chunks.closing({"delta": {}, "finish_reason": finish}, _closing_fields(output))

    if request.structured:
        # Calls and JSON can only be told from the whole reply: stream it as one chunk at the end.
        output = await manager.run_inference(request.model, task, chat_input)
        reply = finish_reply(request, output, max_length)

        async def once():
            yield chunk({"role": "assistant", "content": ""})
            message = reply["message"]
            if message.get("content"):
                yield chunk({"content": message["content"]})
            if message.get("tool_calls"):
                yield chunk({"tool_calls": [{"index": i, **call} for i, call in enumerate(message["tool_calls"])]})
            for line in closing(output, reply["finish_reason"]):
                yield line
            yield "data: [DONE]\n\n"

        return StreamingResponse(once(), media_type="text/event-stream")

    started = await begin_stream(manager, request.model, task, chat_input)

    async def events():
        yield chunk({"role": "assistant", "content": ""})
        async for line in stream_events(
            started,
            ReplyStream(STOP_MARKERS + tuple(request.stop or ())),
            lambda delta: chunk({"content": delta}),
            lambda output: closing(output, finish_reason(output["result"], max_length)),
        ):
            yield line

    return StreamingResponse(events(), media_type="text/event-stream")


# --------------------------------------------------------------- completions
Prompt = Union[str, List[int]]


class CompletionRequest(BaseModel):
    """The legacy completions request: a prompt (or several), as text or token IDs."""

    # seed, best_of, user, presence_penalty, etc. are accepted and ignored.
    model_config = {"extra": "ignore", "protected_namespaces": ()}

    model: str = Field(..., min_length=1, max_length=256)
    prompt: Union[str, List[str], List[int], List[List[int]]] = ""
    max_tokens: Optional[int] = Field(None, ge=0, le=Config.MAX_GENERATION_LENGTH)
    temperature: Optional[float] = Field(None, ge=0, le=2)
    top_p: Optional[float] = Field(None, gt=0, le=1)
    n: int = Field(1, ge=1)
    stream: bool = False
    stream_options: Optional[Dict[str, Any]] = None
    # Alternatives per token (0: the chosen token's log probability alone); unset: none.
    logprobs: Optional[int] = Field(None, ge=0, le=20)
    echo: bool = False
    stop: Optional[Union[str, List[str]]] = None
    suffix: Optional[str] = None

    @field_validator("stop")
    @classmethod
    def valid_stop(cls, value: Any) -> Optional[List[str]]:
        return stop_list(value)

    @property
    def prompts(self) -> List[Prompt]:
        """One entry per prompt: a string or a list of token IDs."""
        value = self.prompt
        if isinstance(value, str):
            return [value]
        if not value:
            return [""]
        if isinstance(value[0], int):
            return [list(value)]  # one prompt as token IDs
        return [list(p) if isinstance(p, list) else p for p in value]


DEFAULT_COMPLETION_TOKENS = 16  # OpenAI's default for this endpoint


def completion_input(request: CompletionRequest, prompt: Prompt) -> Dict[str, Any]:
    """The text-generation input for one prompt of a completions request."""
    kwargs: Dict[str, Any] = {
        "max_length": DEFAULT_COMPLETION_TOKENS if request.max_tokens is None else request.max_tokens,
        "top_p": request.top_p or 0.9,
        "echo": request.echo,
        **_sampling(request.temperature),
    }
    if isinstance(prompt, str):
        kwargs["text"] = prompt[-Config.MAX_INPUT_CHARS:]
    else:
        if len(prompt) > Config.MAX_PROMPT_TOKENS:
            raise Refused(f"a prompt may hold at most {Config.MAX_PROMPT_TOKENS} tokens")
        kwargs["tokens"] = prompt
    if request.logprobs is not None:
        kwargs["logprobs"] = request.logprobs
    elif request.echo and not isinstance(prompt, str):
        # The echoed text of a token prompt comes from the tokens' text, which the model only reports with logprobs.
        kwargs["logprobs"] = 0
    if request.stop:
        kwargs["stop"] = request.stop
    return kwargs


def completion_choice(request: CompletionRequest, prompt: Prompt, output: Dict[str, Any], index: int,
                      max_length: Optional[int]) -> Dict[str, Any]:
    """One ``choices`` entry: the text (after the prompt, with echo), its logprobs and why it ended."""
    result = output["result"]
    text = cut_at_stop(result.text, request.stop)
    logprobs = result.logprobs
    if request.echo:
        if isinstance(prompt, str):
            text = prompt + text
        elif logprobs is not None:
            text = "".join(logprobs.tokens[: result.prompt_tokens or 0]) + text
    choice: Dict[str, Any] = {"index": index, "text": text, "logprobs": None, "finish_reason": finish_reason(result, max_length)}
    if request.logprobs is not None and logprobs is not None:
        choice["logprobs"] = {
            "tokens": logprobs.tokens,
            "token_logprobs": logprobs.token_logprobs,
            "top_logprobs": logprobs.top_logprobs if request.logprobs else None,
            "text_offset": logprobs.text_offset,
        }
    return choice


def _add_usage(total: Dict[str, int], result: Any) -> None:
    usage = usage_of(result)
    if usage is not None:
        for key, value in usage.items():
            total[key] = total.get(key, 0) + value


@router.post("/completions")
async def completions(manager: deps.Manager, request: CompletionRequest):
    """OpenAI's legacy completions: text or token IDs in, text and (with ``logprobs``) per-token log probabilities out.

    With ``echo`` the prompt's tokens are scored too, so a harness can measure
    the likelihood of each answer to a multiple-choice question. Several
    prompts run one after another and come back as one choice each. A
    text-generation model serves it: in-process models through generate(),
    model servers through their own /v1/completions.
    """
    if request.n != 1:
        raise Refused("n must be 1")
    if request.suffix:
        raise Refused("suffix is not supported")
    prompts = request.prompts
    if len(prompts) > Config.MAX_COMPLETION_PROMPTS:
        raise Refused(f"at most {Config.MAX_COMPLETION_PROMPTS} prompts per request")
    if request.stream and (len(prompts) > 1 or request.logprobs is not None or request.echo):
        raise Refused("stream takes one prompt, without logprobs or echo")
    completion_id = f"cmpl-{uuid.uuid4().hex}"
    created = int(time.time())
    inputs = [completion_input(request, prompt) for prompt in prompts]

    if request.stream:
        task_input = inputs[0]

        chunks = Chunks(completion_id, "text_completion", created, request.model, include_usage(request.stream_options))

        def chunk(text: str, finish: Optional[str] = None) -> str:
            return chunks.event({"text": text, "logprobs": None, "finish_reason": finish})

        def closing(output: Dict[str, Any]):
            finish = finish_reason(output["result"], task_input["max_length"])
            return chunks.closing({"text": "", "logprobs": None, "finish_reason": finish}, _closing_fields(output))

        started = await begin_stream(manager, request.model, CHAT_TASK, task_input)
        stream = stream_events(started, ReplyStream(request.stop or (), strip=False), chunk, closing)
        return StreamingResponse(stream, media_type="text/event-stream")

    choices: List[Dict[str, Any]] = []
    usage: Dict[str, int] = {}
    output: Dict[str, Any] = {}
    for index, (prompt, task_input) in enumerate(zip(prompts, inputs)):
        output = await manager.run_inference(request.model, CHAT_TASK, task_input)
        choices.append(completion_choice(request, prompt, output, index, task_input["max_length"]))
        _add_usage(usage, output["result"])
    reply: Dict[str, Any] = {
        "id": completion_id,
        "object": "text_completion",
        "created": created,
        "model": request.model,
        "choices": choices,
        "toolkit": _toolkit_fields(output),
    }
    if usage:
        reply["usage"] = usage
    return reply


# ---------------------------------------------------------------- embeddings
EMBEDDING_TASK = "embedding"


class EmbeddingRequest(BaseModel):
    # user and other fields clients send are accepted and ignored.
    model_config = {"extra": "ignore", "protected_namespaces": ()}

    model: str = Field(..., min_length=1, max_length=256)
    # Token-ID arrays, which OpenAI also takes, are not supported.
    input: Union[str, List[str]]
    encoding_format: Literal["float", "base64"] = "float"
    dimensions: Optional[int] = Field(None, ge=1)


@router.post("/embeddings")
async def embeddings(manager: deps.Manager, request: EmbeddingRequest):
    """One vector per input, L2-normalized (see the embedding task)."""
    if request.dimensions is not None:
        raise Refused("dimensions is not supported")
    texts = [request.input] if isinstance(request.input, str) else request.input
    output = await manager.run_inference(request.model, EMBEDDING_TASK, {"texts": texts})
    result = output["result"]

    def encode(vector: List[float]) -> Any:
        if request.encoding_format == "float":
            return vector
        # OpenAI's base64 form: little-endian float32.
        return base64.b64encode(struct.pack(f"<{len(vector)}f", *vector)).decode()

    return {
        "object": "list",
        "data": [{"object": "embedding", "index": i, "embedding": encode(v)} for i, v in enumerate(result.embeddings)],
        "model": request.model,
        "usage": {"prompt_tokens": result.tokens, "total_tokens": result.tokens},
        "toolkit": _toolkit_fields(output),
    }


# -------------------------------------------------------------------- rerank
RERANK_TASK = "rerank"


class RerankRequest(BaseModel):
    """Cohere's and Jina's rerank request (vLLM's too): documents as strings or {"text": ...}."""

    model_config = {"extra": "ignore", "protected_namespaces": ()}

    model: str = Field(..., min_length=1, max_length=256)
    query: str = Field(..., min_length=1, max_length=Config.MAX_INPUT_CHARS)
    documents: List[Union[str, Dict[str, Any]]] = Field(..., min_length=1, max_length=Config.MAX_EMBEDDING_INPUTS)
    top_n: Optional[int] = Field(None, ge=1)
    return_documents: bool = False


@router.post("/rerank")
async def rerank(manager: deps.Manager, request: RerankRequest):
    """Score the documents by relevance to the query, best first (see the rerank task)."""
    texts = [d if isinstance(d, str) else str(d.get("text") or "") for d in request.documents]
    rerank_input = {"query": request.query, "documents": texts, "top_n": request.top_n}
    output = await manager.run_inference(request.model, RERANK_TASK, rerank_input)
    result = output["result"]
    results = []
    for item in result.results:
        entry: Dict[str, Any] = {"index": item.index, "relevance_score": item.score}
        if request.return_documents:
            entry["document"] = {"text": texts[item.index]}
        results.append(entry)
    return {
        "id": f"rerank-{uuid.uuid4().hex}",
        "model": request.model,
        "results": results,
        "usage": {"total_tokens": result.tokens},
        "toolkit": _toolkit_fields(output),
    }


# ------------------------------------------------------------ transcriptions
SPEECH_TASK = "speech-to-text"


@router.post("/audio/transcriptions")
async def transcriptions(
    manager: deps.Manager,
    file: UploadFile = File(..., description="The audio: WAV, MP3, FLAC, OGG, ..."),
    model: str = Form(..., min_length=1, max_length=256),
    language: Optional[str] = Form(None, max_length=16),
    prompt: Optional[str] = Form(None),
    response_format: Literal["json", "text"] = Form("json"),
    temperature: float = Form(0.0, ge=0.0, le=1.0),
):
    """OpenAI's transcription endpoint (json and text formats)."""
    audio = await file.read(Config.MAX_AUDIO_BYTES + 1)
    if len(audio) > Config.MAX_AUDIO_BYTES:
        raise TooLarge("Audio is larger than MAX_AUDIO_BYTES")
    speech_input = {
        # The speech-to-text input takes base64, like every /inference input.
        "audio": base64.b64encode(audio).decode(),
        "filename": (file.filename or "audio")[:256],
        "language": language or None,
        "prompt": prompt or None,
        "temperature": temperature,
    }
    output = await manager.run_inference(model, SPEECH_TASK, speech_input)
    text = output["result"].text.strip()
    if response_format == "text":
        return PlainTextResponse(text)
    return {"text": text, "toolkit": _toolkit_fields(output)}
