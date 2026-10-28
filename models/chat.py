"""
Chats over the text-generation contract (models/tasks.py): OpenAI's message
format, how a chat becomes the task's input (its turns, trimmed to
MAX_INPUT_CHARS, and a plain transcript every causal LM can continue), and
how the generated text is cut back to the reply, at once or as it streams.
The /v1 routes (api/openai_routes.py) and the playground use them; nothing
here knows about HTTP.
"""

import json
from typing import Any, Dict, List, Optional, Sequence, Union

from pydantic import BaseModel, Field, field_validator

from config import Config

PART_TYPES = ("text", "image_url", "input_audio")
ROLE_LABELS = {"system": "System", "user": "User", "assistant": "Assistant", "tool": "Tool"}
# A causal LM keeps writing the transcript; its reply ends where the next turn starts.
STOP_MARKERS = tuple(f"\n{label}:" for label in ROLE_LABELS.values())
MAX_STOP = 8


class ChatMessage(BaseModel):
    # Clients send other keys too (refusal, audio); keep them out of the way.
    model_config = {"extra": "ignore"}

    role: str
    # Assistant turns that only called tools carry null content. A list
    # holds image or audio parts; text-only lists become one string.
    content: Optional[Union[str, List[Dict[str, Any]]]] = None
    # An assistant turn's calls, and the tool turn answering one.
    tool_calls: Optional[List[Dict[str, Any]]] = None
    tool_call_id: Optional[str] = Field(None, max_length=128)
    name: Optional[str] = Field(None, max_length=256)

    @field_validator("content", mode="before")
    @classmethod
    def join_text_parts(cls, value: Any) -> Any:
        """OpenAI's list form, [{"type": "text", "text": ...}, ...], as one string when it is all text."""
        if not isinstance(value, list):
            return value
        for part in value:
            if not (isinstance(part, dict) and part.get("type") in PART_TYPES):
                raise ValueError(f"content parts must be one of {', '.join(PART_TYPES)}")
            if part["type"] == "text" and not isinstance(part.get("text"), str):
                raise ValueError("a text part needs text")
        if all(part["type"] == "text" for part in value):
            return "\n".join(part["text"] for part in value)
        return value

    @property
    def text(self) -> str:
        """The turn's text, without its media."""
        if isinstance(self.content, list):
            return "\n".join(p["text"] for p in self.content if p["type"] == "text")
        return self.content or ""

    @property
    def has_media(self) -> bool:
        return isinstance(self.content, list)

    @property
    def calls(self) -> List[Dict[str, Any]]:
        """The turn's function calls in the contract's shape (calls without a function name are dropped)."""
        calls = []
        for call in self.tool_calls or ():
            function = call.get("function") if isinstance(call, dict) else None
            if isinstance(function, dict) and function.get("name"):
                arguments = function.get("arguments")
                if not isinstance(arguments, str):
                    arguments = json.dumps(arguments if arguments is not None else {})
                calls.append({
                    "id": str(call.get("id") or ""), "type": "function",
                    "function": {"name": function["name"], "arguments": arguments},
                })
        return calls

    def transcript(self) -> str:
        """The turn as a line of the plain transcript: its text, then its calls in <tool_call> tags."""
        parts = [self.text.strip()] if self.text.strip() else []
        for call in self.calls:
            try:
                arguments = json.loads(call["function"]["arguments"])
            except ValueError:
                arguments = call["function"]["arguments"]
            parts.append("<tool_call>" + json.dumps({"name": call["function"]["name"], "arguments": arguments}) + "</tool_call>")
        return " ".join(parts)


def stop_list(value: Optional[Union[str, List[str]]]) -> Optional[List[str]]:
    """OpenAI's ``stop`` (a string or a list) as the contract's list, or None."""
    if value is None or value == "" or value == []:
        return None
    stops = [value] if isinstance(value, str) else list(value)
    if len(stops) > MAX_STOP or any(not isinstance(s, str) or not s or len(s) > 256 for s in stops):
        raise ValueError(f"stop takes up to {MAX_STOP} strings of 1 to 256 characters")
    return stops


def _turn(m: ChatMessage) -> Dict[str, Any]:
    """A message as a chat turn of the text-generation input."""
    turn: Dict[str, Any] = {"role": m.role, "content": m.text.strip()}
    if m.calls:
        turn["tool_calls"] = m.calls
    if m.tool_call_id:
        turn["tool_call_id"] = m.tool_call_id
    if m.name:
        turn["name"] = m.name
    return turn


def trim_messages(messages: List[ChatMessage]) -> List[Dict[str, Any]]:
    """The turns with text (or calls), dropping the oldest non-system ones until they fit MAX_INPUT_CHARS."""
    turns = [_turn(m) for m in messages if m.text.strip() or m.calls]
    budget = Config.MAX_INPUT_CHARS
    size = sum(len(t["content"]) for t in turns)
    while size > budget:
        oldest = next((i for i, t in enumerate(turns) if t["role"] != "system"), None)
        if oldest is not None and oldest != len(turns) - 1:
            size -= len(turns.pop(oldest)["content"])
            continue
        # Only system turns and the latest turn are left: shorten the longest,
        # keeping the start of a system prompt and the end of the latest turn.
        longest = max(turns, key=lambda t: len(t["content"]))
        cut = min(size - budget, len(longest["content"]))
        if longest["role"] == "system":
            longest["content"] = longest["content"][: len(longest["content"]) - cut]
        else:
            longest["content"] = longest["content"][cut:]
        size -= cut
    return turns


def build_prompt(messages: List[ChatMessage], guide: str = "") -> str:
    """Flatten the chat into a transcript that ends on the assistant's turn.

    ``guide`` (the tool or JSON instruction) opens it as a system line.
    """
    lines = [f"System: {guide}"] if guide else []
    lines += [
        f"{ROLE_LABELS.get(m.role, m.role.title())}: {m.transcript()}"
        for m in messages
        if m.transcript()
    ]
    lines.append("Assistant:")
    # Keep the end of an over-long chat; the text-generation input caps text at MAX_INPUT_CHARS.
    return "\n".join(lines)[-Config.MAX_INPUT_CHARS:]


def completion_text(text: str) -> str:
    """The reply alone: the generated text up to where the model starts the next turn."""
    # Some models return the reply already stripped, so a turn that starts
    # immediately has no leading newline; add one so the markers still match.
    text = "\n" + text
    for marker in STOP_MARKERS:
        text = text.split(marker, 1)[0]
    return text.strip()


def cut_at_stop(text: str, stop: Optional[List[str]]) -> str:
    """The text before the first of the client's stop strings."""
    for marker in stop or ():
        text = text.split(marker, 1)[0]
    return text


def _held_back(text: str, markers: Sequence[str]) -> int:
    """Length of the longest end of ``text`` that could be the start of a marker."""
    return max((k for m in markers for k in range(1, len(m)) if text.endswith(m[:k])), default=0)


class ReplyStream:
    """Turns generated text, piece by piece, into reply deltas.

    Same rules as completion_text(): surrounding whitespace is dropped and the
    reply ends where the model starts the next turn (or at one of the client's
    stop strings). Text that could be the start of a marker is held back until
    the next piece settles it. For /v1/completions, ``strip=False`` keeps the
    text as generated and ``markers`` are the stop strings alone.
    """

    def __init__(self, markers: Sequence[str] = STOP_MARKERS, strip: bool = True) -> None:
        self.markers = tuple(markers)
        self.strip = strip
        # As in completion_text(), so a turn that starts at once is caught.
        self.text = "\n" if strip else ""
        self.sent = ""
        self.ended = False

    def feed(self, piece: str) -> str:
        if self.ended:
            return ""
        self.text += piece
        cuts = [i for i in (self.text.find(m) for m in self.markers) if i >= 0]
        if cuts:
            self.ended = True
            return self._advance(min(cuts))
        return self._advance(len(self.text) - _held_back(self.text, self.markers))

    def finish(self) -> str:
        """Whatever was held back, once generation is over."""
        return "" if self.ended else self._advance(len(self.text))

    def _advance(self, end: int) -> str:
        reply = self.text[:end].strip() if self.strip else self.text[:end]
        delta = reply[len(self.sent):]
        self.sent = reply
        return delta


def vision_messages(messages: List[ChatMessage]) -> List[Dict[str, Any]]:
    """The turns with content, parts kept, for the vision-language input."""
    turns = []
    for m in messages:
        if m.has_media:
            turns.append({"role": m.role, "content": m.content})
        elif m.text.strip():
            turns.append({"role": m.role, "content": m.text.strip()})
    return turns
