"""
What each task takes and returns.

A task's contract is a pair of Pydantic models: what ``predict()`` receives
and what it must return. Every model registered for a task follows the same
contract, so models are interchangeable and clients see one shape per task.
The manager validates the input before loading the model, and
``BaseModel.run()`` validates the output; ``POST /inference`` takes the input
model as its ``input`` and returns the output model as its ``result``, and
``GET /tasks`` serves both as JSON Schema.

The built-in tasks are defined here. A model file can add a task with
``define_task()`` (see models/families/custom/custom_model.py). A task registered
without a contract still works, but its input and result are passed through
unchecked.
"""

import base64
import binascii
import re
from dataclasses import dataclass
from typing import Annotated, Any, Dict, Iterable, List, Literal, Optional, Tuple, Union, get_args

from pydantic import Base64Bytes, BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

from core.config import Config
from core.errors import TooLarge


class TaskInput(BaseModel):
    """Base for input models: unknown fields are an error, not silently ignored."""

    model_config = ConfigDict(extra="forbid")


class TaskOutput(BaseModel):
    """Base for output models: fields a model adds beyond the contract are dropped."""

    model_config = ConfigDict(extra="ignore")


Text = Field(..., min_length=1, max_length=Config.MAX_INPUT_CHARS)


# Where the playground and the model browser file a task: one tab each (frontend/src/shared/categories.ts draws
# them). Chat also borrows the tasks whose models chat, wherever they live.
Category = Literal["chat", "text", "vision", "audio", "decision", "prediction", "other"]
CATEGORIES: Tuple[str, ...] = get_args(Category)
# The tasks the Chat tab talks to through /v1/chat/completions: no other task may make chat its tab.
CHAT_TASKS = ("text-generation", "vision-language")
# Labels a zero-shot request may name: each is read with the text in a forward pass of its own.
MAX_LABELS = 32


class ContractViolation(RuntimeError):
    """A model returned something its task's output model rejects (a server bug, not a client error)."""


@dataclass(frozen=True)
class TaskContract:
    name: str
    input: type
    output: type
    description: str = ""
    # The Hub's pipeline tag for models of this task (the model browser's Hub search filters on it).
    hub_tag: Optional[str] = None
    # The task's tab, and its short name for people (the task's name when it has none).
    category: Category = "other"
    label: str = ""
    # The task this one is the same job for, done by another kind of model (zero-shot classification for
    # text-classification): it shares that task's tab and its entry in the playground's task switch.
    variant_of: Optional[str] = None

    def parse_input(self, data: Any) -> TaskInput:
        """Validate a request; a pydantic ValidationError is a ValueError, so routes answer 400."""
        return self.input.model_validate(data)

    def parse_output(self, data: Any) -> TaskOutput:
        try:
            return self.output.model_validate(data)
        except ValueError as e:
            raise ContractViolation(f"{self.name} result does not match {self.output.__name__}: {e}") from e

    def describe(self) -> Dict[str, Any]:
        return {
            "description": self.description,
            **place(self.name),
            "variant_of": self.variant_of,
            "input": self.input.model_json_schema(),
            "output": self.output.model_json_schema(),
        }


_contracts: Dict[str, TaskContract] = {}


def define_task(
    name: str,
    input: type,
    output: type,
    description: str = "",
    hub_tag: Optional[str] = None,
    category: Optional[Category] = None,
    label: str = "",
    variant_of: Optional[str] = None,
) -> TaskContract:
    """Give ``name`` a contract, filed under ``category`` (Other when unset) as ``label``.

    A variant of another task (``variant_of``: the same job, done by another kind of model) goes wherever that
    task goes, so it names no category. Whether that task exists is checked once every model file is in
    (``verify``), so a model file may vary a task another one defines. Defining a task again replaces its contract.
    """
    if not (issubclass(input, BaseModel) and issubclass(output, BaseModel)):
        raise TypeError("input and output must be pydantic models")
    if variant_of is not None and (category is not None or variant_of == name):
        raise ValueError(f"{name} varies another task and goes in its tab: give it no category")
    category = category or "other"
    if category not in CATEGORIES:
        raise ValueError(f"category must be one of {', '.join(CATEGORIES)}, not {category!r}")
    if category == "chat" and name not in CHAT_TASKS:
        raise ValueError(f"the chat tab holds the tasks /v1/chat/completions serves ({', '.join(CHAT_TASKS)}); "
                         f"give {name} another one")
    contract = TaskContract(name, input, output, description, hub_tag, category, label or name, variant_of)
    _contracts[name] = contract
    return contract


def get(name: str) -> Optional[TaskContract]:
    """The task's contract, or None if it has none."""
    return _contracts.get(name)


def defined() -> Dict[str, TaskContract]:
    """Every contract defined so far, by task name."""
    return dict(_contracts)


def place(name: str) -> Dict[str, str]:
    """Where the playground and the model browser show a task: its tab (``category``), its ``label``, and the
    entry it shares in the tab's task switch (``group``: the task it varies, else itself; ``group_label``).

    A task without a contract is Other, under its name; a variant whose task is missing, or is a variant
    itself, stands alone (``verify`` reports it).
    """
    contract = _contracts.get(name)
    if contract is None:
        return {"category": "other", "label": name, "group": name, "group_label": name}
    parent = _parent(contract)
    if parent is None:
        return {"category": contract.category, "label": contract.label, "group": name, "group_label": contract.label}
    return {"category": parent.category, "label": contract.label, "group": parent.name, "group_label": parent.label}


def _parent(contract: TaskContract) -> Optional[TaskContract]:
    """The task ``contract`` varies; None when it varies none, or one without a contract or that is a variant itself."""
    parent = _contracts.get(contract.variant_of) if contract.variant_of else None
    return parent if parent is not None and parent.variant_of is None else None


def describe(name: str) -> Dict[str, Any]:
    """GET /tasks' entry for a task: its contract's JSON Schemas (null without one), its place, and variant_of."""
    contract = _contracts.get(name)
    if contract is not None:
        return contract.describe()
    return {"description": "", **place(name), "variant_of": None, "input": None, "output": None}


def verify() -> List[str]:
    """What is wrong with the variants: a task one varies that has no contract, or is a variant itself."""
    problems = []
    for contract in _contracts.values():
        if contract.variant_of and _parent(contract) is None:
            parent = _contracts.get(contract.variant_of)
            why = "has no contract" if parent is None else f"is itself a variant of {parent.variant_of}"
            problems.append(f"{contract.name} varies {contract.variant_of}, which {why}")
    return problems


def ordered(names: Iterable[str]) -> List[str]:
    """``names`` with the tasks that have a contract first, in the order they were defined, then the rest by name."""
    names = set(names)
    defined = [name for name in _contracts if name in names]
    return defined + sorted(names - set(defined))


def hub_tags() -> Dict[str, str]:
    """Each task that has one -> its Hub pipeline tag."""
    return {name: c.hub_tag for name, c in _contracts.items() if c.hub_tag}


# ------------------------------------------------------------ shared parts
class GenerationParams(TaskInput):
    max_length: Optional[int] = Field(
        None,
        ge=0,
        le=Config.MAX_GENERATION_LENGTH,
        description="Maximum number of new tokens (prompt not included). Unset: the model's default; "
        "0 generates nothing (with echo and logprobs: score the prompt only)",
    )
    temperature: float = Field(0.7, gt=0.0, le=5.0)
    top_p: float = Field(0.9, gt=0.0, le=1.0)
    do_sample: bool = True

    def sampling(self) -> Dict[str, Any]:
        """generate()'s sampling arguments: temperature and top_p only when sampling (greedy ignores them)."""
        if self.do_sample:
            return {"do_sample": True, "temperature": self.temperature, "top_p": self.top_p}
        return {"do_sample": False}


class FunctionCall(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = Field(..., min_length=1, max_length=256)
    arguments: str = Field("{}", max_length=Config.MAX_INPUT_CHARS, description="The arguments as a JSON string")


class ToolCall(BaseModel):
    """A function call in OpenAI's shape, in a reply or in an earlier assistant turn."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field("", max_length=128)
    type: Literal["function"] = "function"
    function: FunctionCall


class ChatTurn(TaskInput):
    role: str = Field(..., min_length=1, max_length=32)
    content: str = Field(..., max_length=Config.MAX_INPUT_CHARS)
    # An assistant turn that called tools, and the tool turn answering one.
    tool_calls: Optional[List[ToolCall]] = None
    tool_call_id: Optional[str] = Field(None, max_length=128)
    name: Optional[str] = Field(None, max_length=256)


class TokenLogprobs(BaseModel):
    """Per-token log probabilities in OpenAI's completions shape: the prompt's tokens first when echoed."""

    model_config = ConfigDict(extra="ignore")

    tokens: List[str] = Field(..., description="Each token as text")
    token_logprobs: List[Optional[float]] = Field(..., description="Log probability of each token; null for the first prompt token")
    top_logprobs: Optional[List[Optional[Dict[str, float]]]] = Field(
        None, description="The most likely tokens at each position, when the request asked for them"
    )
    text_offset: List[int] = Field(..., description="Character offset of each token in the echoed prompt plus the text")


class GeneratedText(TaskOutput):
    text: str = Field(..., description="The new text only, never the prompt")
    tool_calls: Optional[List[ToolCall]] = Field(
        None, description="Calls the model made instead of (or besides) answering, when it was given tools"
    )
    prompt_tokens: Optional[int] = Field(None, ge=0, description="Tokens in the prompt, when the model counts them")
    completion_tokens: Optional[int] = Field(None, ge=0, description="Tokens generated, when the model counts them")
    logprobs: Optional[TokenLogprobs] = Field(None, description="Token log probabilities, when the request asked for them")


# --------------------------------------------------------- text-generation
class TextGenerationInput(GenerationParams):
    text: str = Field("", max_length=Config.MAX_INPUT_CHARS, description="The prompt (or ``tokens``)")
    tokens: Optional[List[int]] = Field(
        None,
        max_length=Config.MAX_PROMPT_TOKENS,
        description="The prompt as token IDs in place of text, as evaluation harnesses send it",
    )
    messages: Optional[List[ChatTurn]] = Field(
        None,
        description="The same prompt as a chat. Models with a chat template use it in place of text",
    )
    logprobs: Optional[int] = Field(
        None,
        ge=0,
        le=20,
        description="Return each generated token's log probability, with this many most likely alternatives (0: none)",
    )
    echo: bool = Field(False, description="With logprobs: score the prompt's tokens too")
    stop: Optional[List[str]] = Field(
        None, max_length=8, description="Stop generating at the first of these strings; the text ends before it"
    )
    tools: Optional[List[Dict[str, Any]]] = Field(
        None,
        max_length=128,
        description="Functions the model may call, in OpenAI's shape "
        '({"type": "function", "function": {"name", "description", "parameters"}}). '
        "Models with a chat template that takes tools get them in that format; others get them as an instruction",
    )
    tool_choice: Optional[str] = Field(
        None, max_length=256, description='"auto" (default), "none", "required", or the name of the function to call'
    )
    json_schema: Optional[Dict[str, Any]] = Field(
        None, description="Ask for a JSON reply matching this JSON Schema ({} for any JSON object)"
    )

    @model_validator(mode="after")
    def has_a_prompt(self) -> "TextGenerationInput":
        if not self.text and not self.tokens and not self.messages:
            raise ValueError("text, tokens or messages is required")
        return self

    @field_validator("stop")
    @classmethod
    def stop_strings(cls, value: Optional[List[str]]) -> Optional[List[str]]:
        if value is not None and any(not s or len(s) > 256 for s in value):
            raise ValueError("each stop string must be 1 to 256 characters")
        return value or None

    @field_validator("tools")
    @classmethod
    def function_tools(cls, value: Optional[List[Dict[str, Any]]]) -> Optional[List[Dict[str, Any]]]:
        for tool in value or ():
            function = tool.get("function") if tool.get("type") == "function" else None
            if not isinstance(function, dict) or not isinstance(function.get("name"), str) or not function["name"]:
                raise ValueError('each tool must be {"type": "function", "function": {"name": ..., ...}}')
        return value


define_task(
    "text-generation",
    TextGenerationInput,
    GeneratedText,
    "Continue a prompt, or reply to a chat",
    hub_tag="text-generation",
    category="chat",
    label="Text generation",
)


# ----------------------------------------------------- text-classification
class TextClassificationInput(TaskInput):
    text: str = Text


class TextClassificationOutput(TaskOutput):
    label: str
    confidence: float = Field(..., ge=0.0, le=1.0, description="Probability of label")
    scores: Dict[str, float] = Field(..., description="Probability of every label, keyed like label")

    @model_validator(mode="after")
    def label_is_scored(self):
        if self.label not in self.scores:
            raise ValueError(f"label {self.label!r} is missing from scores")
        return self


define_task(
    "text-classification",
    TextClassificationInput,
    TextClassificationOutput,
    "Label a text, with a probability for every label",
    hub_tag="text-classification",
    category="text",
    label="Classification",
)


# ------------------------------------------------ zero-shot-classification
class ZeroShotClassificationInput(TaskInput):
    text: str = Text
    labels: List[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]] = Field(
        ..., min_length=1, max_length=MAX_LABELS, description="The labels to choose from"
    )
    multi_label: bool = Field(
        False, description="Score each label on its own, so several can fit; unset: the labels share the probability"
    )
    hypothesis: str = Field(
        "This text is about {}.", max_length=500, description="How each label is put to the model; {} is the label"
    )

    @field_validator("labels")
    @classmethod
    def distinct(cls, value: List[str]) -> List[str]:
        if len(set(value)) < len(value):
            raise ValueError("labels must differ")
        return value

    @field_validator("hypothesis")
    @classmethod
    def names_the_label(cls, value: str) -> str:
        if value.count("{}") != 1:
            raise ValueError("hypothesis must hold {} once, where the label goes")
        return value


define_task(
    "zero-shot-classification",
    ZeroShotClassificationInput,
    TextClassificationOutput,
    "Label a text with labels named in the request, with a probability for each (no training)",
    hub_tag="zero-shot-classification",
    label="Your labels",
    variant_of="text-classification",
)


# ------------------------------------------------------ question-answering
class QuestionAnsweringInput(TaskInput):
    question: str = Text
    context: str = Field(..., min_length=1, max_length=Config.MAX_INPUT_CHARS, description="Passage to answer from")


class QuestionAnsweringOutput(TaskOutput):
    answer: str = Field(..., description="Span copied from the context")
    confidence: float = Field(..., ge=0.0, le=1.0)
    start_position: int = Field(..., ge=0, description="Token index where the answer starts")
    end_position: int = Field(..., ge=0, description="Token index where the answer ends (inclusive)")


define_task(
    "question-answering",
    QuestionAnsweringInput,
    QuestionAnsweringOutput,
    "Extract the answer to a question from a passage",
    hub_tag="question-answering",
    category="text",
    label="Question answering",
)


# --------------------------------------------------------- vision-language
# Chat content parts in OpenAI's shape, so a chat reaches model servers
# unchanged and Transformers processors read it as is. Media must be inline
# (base64): the server never fetches a URL a client names.
_IMAGE_DATA_URL = re.compile(r"^data:image/(png|jpeg|webp|gif);base64,(.*)$", re.DOTALL)


class MediaTooLarge(TooLarge):
    """An inline image or audio item exceeds MAX_MEDIA_BYTES (a ValueError, so a validator turns it into one)."""


def _checked_base64(data: str, what: str) -> str:
    if len(data) > Config.MAX_MEDIA_BYTES * 4 // 3 + 4:
        raise MediaTooLarge(f"{what} is larger than MAX_MEDIA_BYTES")
    try:
        base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError(f"{what} is not valid base64") from None
    return data


def image_data_url(url: str, formats: Tuple[str, ...] = ("png", "jpeg", "webp", "gif")) -> str:
    """The base64 payload of an inline image data URL in one of ``formats``, checked.

    Raises MediaTooLarge past MAX_MEDIA_BYTES, else ValueError for anything
    that is not a well-formed data URL of an accepted format.
    """
    match = _IMAGE_DATA_URL.match(url)
    if not match or match.group(1) not in formats:
        names = ", ".join(f.upper() for f in formats)
        raise ValueError(f"image_url must be a base64 data URL ({names}); remote URLs are not fetched")
    return _checked_base64(match.group(2), "image")


def data_url_bytes(url: str) -> bytes:
    """The bytes of an image data URL (already checked by ImageURL)."""
    return base64.b64decode(_IMAGE_DATA_URL.match(url).group(2))


class _Part(BaseModel):
    # Clients add fields of their own (detail, cache_control); ignore them.
    model_config = ConfigDict(extra="ignore")


class TextPart(_Part):
    type: Literal["text"]
    text: str = Field(..., max_length=Config.MAX_INPUT_CHARS)


class ImageURL(_Part):
    url: str = Field(..., description="A data:image/(png|jpeg|webp|gif);base64,... URL")

    @field_validator("url")
    @classmethod
    def inline_image(cls, value: str) -> str:
        image_data_url(value)
        return value


class ImagePart(_Part):
    type: Literal["image_url"]
    image_url: ImageURL


class InputAudio(_Part):
    data: str = Field(..., description="Base64 audio")
    format: Literal["wav", "mp3"]

    @field_validator("data")
    @classmethod
    def valid_base64(cls, value: str) -> str:
        return _checked_base64(value, "audio")


class AudioPart(_Part):
    type: Literal["input_audio"]
    input_audio: InputAudio


ContentPart = Annotated[Union[TextPart, ImagePart, AudioPart], Field(discriminator="type")]


class MultimodalTurn(TaskInput):
    role: str = Field(..., min_length=1, max_length=32)
    content: Union[str, List[ContentPart]]


class VisionLanguageInput(GenerationParams):
    text: str = Text
    image: Optional[str] = Field(None, max_length=1024, description="Image path inside IMAGE_DIR")
    messages: Optional[List[MultimodalTurn]] = Field(
        None,
        description="The prompt as a chat with image and audio parts (OpenAI's shape); used in place of text and image",
    )

    @field_validator("image")
    @classmethod
    def inside_image_dir(cls, value: Optional[str]) -> Optional[str]:
        """Only files inside IMAGE_DIR, whichever route the request came through."""
        if value is None:
            return None
        base = Config.IMAGE_DIR
        path = (base / value).resolve()
        if not path.is_relative_to(base) or not path.is_file():
            raise ValueError("image must be an existing file inside IMAGE_DIR")
        return str(path)

    @field_validator("messages")
    @classmethod
    def few_media_parts(cls, value: Optional[List[MultimodalTurn]]) -> Optional[List[MultimodalTurn]]:
        media = sum(1 for turn in value or () if not isinstance(turn.content, str)
                    for part in turn.content if part.type != "text")
        if media > Config.MAX_MEDIA_PARTS:
            raise ValueError(f"at most {Config.MAX_MEDIA_PARTS} images and audio clips per request")
        return value


define_task(
    "vision-language",
    VisionLanguageInput,
    GeneratedText,
    "Answer a prompt or a chat about images (and, for models that take it, audio)",
    hub_tag="image-text-to-text",
    category="vision",
    label="Vision-language",
)


# --------------------------------------------------------------- embedding
class EmbeddingInput(TaskInput):
    texts: List[Annotated[str, Field(min_length=1, max_length=Config.MAX_INPUT_CHARS)]] = Field(
        ..., min_length=1, max_length=Config.MAX_EMBEDDING_INPUTS
    )


class EmbeddingOutput(TaskOutput):
    embeddings: List[List[float]] = Field(..., description="One vector per text, in order")
    tokens: int = Field(0, ge=0, description="Tokens read, for usage")


define_task(
    "embedding",
    EmbeddingInput,
    EmbeddingOutput,
    "Turn texts into vectors for search and similarity",
    hub_tag="feature-extraction",
    category="text",
    label="Embedding",
)


# ------------------------------------------------------------------ rerank
class RerankInput(TaskInput):
    query: str = Text
    documents: List[Annotated[str, Field(min_length=1, max_length=Config.MAX_INPUT_CHARS)]] = Field(
        ..., min_length=1, max_length=Config.MAX_EMBEDDING_INPUTS
    )
    top_n: Optional[int] = Field(None, ge=1, description="Return only the best n; unset: every document")


class RerankResult(TaskOutput):
    index: int = Field(..., ge=0, description="Position of the document in the request")
    score: float = Field(..., description="Relevance to the query; higher is better")


class RerankOutput(TaskOutput):
    results: List[RerankResult] = Field(..., description="Best first")
    tokens: int = Field(0, ge=0, description="Tokens read, for usage")


define_task(
    "rerank",
    RerankInput,
    RerankOutput,
    "Score documents by relevance to a query (a cross-encoder reads each pair)",
    hub_tag="text-ranking",
    category="text",
    label="Rerank",
)


# ---------------------------------------------------------- speech-to-text
class SpeechToTextInput(TaskInput):
    audio: Base64Bytes = Field(..., description="The audio file (WAV, MP3, FLAC, OGG, ...), base64")
    filename: str = Field("audio", max_length=256, description="Its name; servers may read the format from it")
    language: Optional[str] = Field(None, max_length=16, description="ISO-639-1 code, e.g. en; unset: detected")
    prompt: Optional[str] = Field(None, max_length=Config.MAX_INPUT_CHARS, description="Text that guides spelling and style")
    temperature: float = Field(0.0, ge=0.0, le=1.0)

    @field_validator("audio")
    @classmethod
    def not_too_large(cls, value: bytes) -> bytes:
        if len(value) > Config.MAX_AUDIO_BYTES:
            raise ValueError("audio is larger than MAX_AUDIO_BYTES")
        return value


class Transcript(TaskOutput):
    text: str
    language: Optional[str] = None


define_task(
    "speech-to-text",
    SpeechToTextInput,
    Transcript,
    "Transcribe speech",
    hub_tag="automatic-speech-recognition",
    category="audio",
    label="Speech to text",
)


# ---------------------------------------------------------------- decision
class VegaQuestion(TaskInput):
    """One question in vegaml's format (/v1/decisions converts Perplexity's)."""

    type: Literal["boolean", "choice", "score"]
    instructions: str = Field("", max_length=Config.MAX_INPUT_CHARS)
    criteria: Optional[Union[Dict[str, Optional[str]], List[str]]] = None


class DecisionInput(TaskInput):
    state: Optional[Union[str, Dict[str, Any], List[Any]]] = Field(None, description="What to decide about")
    image: Optional[Base64Bytes] = Field(None, description="A PNG, JPEG or WebP image in place of state")
    questions: Dict[str, VegaQuestion] = Field(..., min_length=1, max_length=128)

    @model_validator(mode="after")
    def state_or_image(self):
        if (self.state is None) == (self.image is None):
            raise ValueError("give either state or image")
        return self


class DecisionOutput(TaskOutput):
    answers: Dict[str, Dict[str, Any]] = Field(..., description="vegaml's answer per question name")
    n_tokens: int = Field(0, ge=0)


define_task(
    "decision",
    DecisionInput,
    DecisionOutput,
    "Answer typed questions about a state with probabilities (no text is generated)",
    category="decision",
    label="Decision",
)
