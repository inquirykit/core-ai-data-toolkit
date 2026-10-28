"""
What a trace keeps of a request: its input and output, made safe to store.

``compact`` cuts long strings to TRACE_MAX_CHARS, replaces media (bytes and
data URLs) with their size and hash, and caps long lists; each string is
bounded, not the trace as a whole. ``compacted`` tells whether a value was
changed so, since a dataset example made from it would not be what the
client sent. ``text_of`` flattens a value to the words in it, for the
full-text index and for previews. (How a request is tagged is
models/events.py's request_context.)
"""

import hashlib
import json
import re
from typing import Any, Dict, List

DATA_URL = re.compile(r"^data:([\w.+/-]+);base64,", re.I)
# A data URL shorter than this is kept as it is.
MEDIA_MIN_CHARS = 512
MAX_LIST_ITEMS = 256
# How compact marks what it cut: the end of a cut string, and the last item of a capped list.
CUT = re.compile(r"… \[\d+ more chars\]$")
MORE_ITEMS = re.compile(r"^… \d+ more items$")
# Keys whose values are vectors, never words.
VECTOR_KEYS = {"embedding", "embeddings", "vector"}


def _media(raw: bytes, kind: str) -> Dict[str, Any]:
    return {"$media": {"kind": kind, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}}


def compact(value: Any, max_chars: int = 8000) -> Any:
    """``value`` as JSON-ready data no bigger than a trace should be."""
    if hasattr(value, "model_dump"):
        value = value.model_dump()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return _media(bytes(value), "bytes")
    if isinstance(value, str):
        url = DATA_URL.match(value) if len(value) >= MEDIA_MIN_CHARS else None
        if url:
            return _media(value[url.end():].encode(), url.group(1))
        if len(value) > max_chars:
            return f"{value[:max_chars]}… [{len(value) - max_chars} more chars]"
        return value
    if isinstance(value, dict):
        return {str(k): compact(v, max_chars) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
        out = [compact(v, max_chars) for v in items[:MAX_LIST_ITEMS]]
        if len(items) > MAX_LIST_ITEMS:
            out.append(f"… {len(items) - MAX_LIST_ITEMS} more items")
        return out
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)


def compacted(value: Any) -> bool:
    """Whether ``compact`` cut a string, capped a list or replaced media anywhere in ``value``."""
    if isinstance(value, str):
        return bool(CUT.search(value))
    if isinstance(value, dict):
        return "$media" in value or any(compacted(v) for v in value.values())
    if isinstance(value, list):
        return bool(value and isinstance(value[-1], str) and MORE_ITEMS.match(value[-1])) or any(compacted(v) for v in value)
    return False


def text_of(value: Any, limit: int = 20000) -> str:
    """The words in ``value``: its strings, joined, without vectors or media."""
    parts: List[str] = []
    size = 0

    def walk(v: Any) -> None:
        nonlocal size
        if size >= limit:
            return
        if isinstance(v, str):
            parts.append(v)
            size += len(v) + 1
        elif isinstance(v, dict):
            if "$media" in v:
                return
            for k, item in v.items():
                if k not in VECTOR_KEYS:
                    walk(item)
        elif isinstance(v, (list, tuple)):
            for item in v:
                walk(item)

    walk(value)
    return " ".join(parts)[:limit]


def input_text(value: Any) -> str:
    """The part of an input a reader would call "the prompt": the last message, or the text field, or all of it."""
    if isinstance(value, dict):
        messages = value.get("messages")
        if isinstance(messages, list) and messages:
            last = messages[-1]
            if isinstance(last, dict):
                content = last.get("content")
                if isinstance(content, str):
                    return content
                return text_of(content)
        for key in ("text", "query", "question", "prompt"):
            if isinstance(value.get(key), str):
                return value[key]
    return text_of(value)


def output_text(value: Any) -> str:
    """What a model answered, as text: its text field, else the whole result as JSON."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("text", "answer", "transcript", "label"):
            if isinstance(value.get(key), str):
                return value[key]
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)
