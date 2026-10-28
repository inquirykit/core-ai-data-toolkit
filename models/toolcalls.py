"""
Tool calls and JSON replies from text-generation models.

Models with a chat template that takes tools (Qwen, Granite, Llama 3,
Mistral, SmolLM, ...) are given the tools in that format and answer in the
format the template taught them. Other models, and models asked for JSON,
get an instruction (``instructions()``) folded into the system prompt. Either
way the reply is text, and ``parse_tool_calls()`` finds the calls in it,
whichever of the common formats the model used; ``extract_json()`` finds a
JSON reply. Model servers (vLLM, llama.cpp) parse their own replies.

Nothing here constrains decoding: a model that ignores the instruction
produces no call, or JSON that does not match (``validate_json()`` says so).
"""

import json
import re
from typing import Any, Dict, List, Optional, Tuple

# Qwen / Hermes: <tool_call>{...}</tool_call>. Granite: <|tool_call|>[{...}]
# or <|tool_call|>{...}. Mistral: [TOOL_CALLS][{...}]. Llama 3 (and models
# following the instruction): a bare {"name": ..., "arguments": {...}}.
# Qwen3-Coder: <tool_call><function=name><parameter=key>value</parameter>...</function></tool_call>.
_TAGGED = re.compile(
    r"<tool_call>\s*(?P<a>.+?)\s*</tool_call>"
    r"|<\|tool_call\|>\s*(?P<b>[\[{].*?)(?=<\|tool_call\|>|\Z)"
    r"|\[TOOL_CALLS\]\s*(?P<c>[\[{].*?)(?=\[TOOL_CALLS\]|\Z)",
    re.DOTALL,
)
_FENCE = re.compile(r"^\s*```[a-zA-Z0-9_-]*\s*\n(.*?)\n?\s*```\s*$", re.DOTALL)
_XML_FUNCTION = re.compile(r"<function=([^>\s]+)>(.*?)</function>", re.DOTALL)
_XML_PARAMETER = re.compile(r"<parameter=([^>\s]+)>\n?(.*?)\n?</parameter>", re.DOTALL)
_ARGUMENT_KEYS = ("arguments", "parameters", "args", "input")


def tool_names(tools: Optional[List[Dict[str, Any]]]) -> List[str]:
    return [t["function"]["name"] for t in tools or ()]


def instructions(
    tools: Optional[List[Dict[str, Any]]],
    tool_choice: Optional[str],
    json_schema: Optional[Dict[str, Any]],
    listed: bool = True,
) -> str:
    """The system-prompt text asking for tool calls or JSON; empty when neither was requested.

    ``listed=False`` leaves out the functions themselves (and how to call
    them), for a chat template that renders them.
    """
    parts = []
    if tools and tool_choice != "none":
        if listed:
            functions = "\n".join(json.dumps(t["function"], ensure_ascii=False) for t in tools)
            parts.append(
                "You can call these functions (JSON Schema per function):\n"
                f"{functions}\n"
                "To call one, reply with only <tool_call>{\"name\": \"<function name>\", "
                "\"arguments\": {<arguments>}}</tool_call>, one per call. "
                "The function result arrives in a tool message."
            )
        if tool_choice == "required":
            parts.append("You must call at least one function now.")
        elif tool_choice and tool_choice != "auto":
            parts.append(f"You must call the function {tool_choice} now.")
    if json_schema is not None:
        if json_schema:
            parts.append(
                "Reply with a single JSON value that matches this JSON Schema, and nothing else:\n"
                + json.dumps(json_schema, ensure_ascii=False)
            )
        else:
            parts.append("Reply with a single JSON object and nothing else.")
    return "\n\n".join(parts)


def template_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """``messages`` as chat templates take them: tool call arguments as objects, not JSON strings."""
    turns = []
    for m in messages:
        turn = dict(m)
        if turn.get("tool_calls"):
            calls = []
            for call in turn["tool_calls"]:
                function = dict(call.get("function") or {})
                if isinstance(function.get("arguments"), str):
                    try:
                        function["arguments"] = json.loads(function["arguments"])
                    except ValueError:
                        function["arguments"] = {}
                calls.append({**call, "type": "function", "function": function})
            turn["tool_calls"] = calls
        turns.append(turn)
    return turns


def with_instructions(messages: List[Dict[str, Any]], text: str) -> List[Dict[str, Any]]:
    """``messages`` with ``text`` added to the system turn (a new first turn if there is none)."""
    if not text:
        return messages
    turns = [dict(m) for m in messages]
    system = next((m for m in turns if m.get("role") == "system"), None)
    if system is None:
        turns.insert(0, {"role": "system", "content": text})
    else:
        system["content"] = f"{system['content']}\n\n{text}".strip()
    return turns


def _loads(text: str) -> Any:
    """json.loads that also takes a fenced block or trailing text after the value."""
    fenced = _FENCE.match(text)
    if fenced:
        text = fenced.group(1)
    text = text.strip()
    try:
        return json.loads(text)
    except ValueError:
        value, _ = json.JSONDecoder().raw_decode(text)
        return value


def _as_calls(value: Any, known: Optional[List[str]]) -> List[Tuple[str, Dict[str, Any]]]:
    """(name, arguments) pairs from a parsed call object or list of them; [] if it is not one."""
    items = value if isinstance(value, list) else [value]
    calls = []
    for item in items:
        if not isinstance(item, dict):
            return []
        name = item.get("name")
        if not isinstance(name, str) or (known is not None and name not in known):
            return []
        arguments = next((item[k] for k in _ARGUMENT_KEYS if k in item), {})
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except ValueError:
                return []
        if not isinstance(arguments, dict):
            return []
        calls.append((name, arguments))
    return calls


def _xml_calls(raw: str, known: Optional[List[str]]) -> List[Tuple[str, Dict[str, Any]]]:
    """Calls in Qwen3-Coder's <function=name><parameter=key>value</parameter></function> form."""
    calls = []
    for name, body in _XML_FUNCTION.findall(raw):
        if known is not None and name not in known:
            return []
        arguments: Dict[str, Any] = {}
        for key, value in _XML_PARAMETER.findall(body):
            try:
                arguments[key] = json.loads(value)
            except ValueError:
                arguments[key] = value
        calls.append((name, arguments))
    return calls


def parse_tool_calls(
    text: str, tools: Optional[List[Dict[str, Any]]] = None
) -> Tuple[str, List[Tuple[str, Dict[str, Any]]]]:
    """(the text without its calls, the calls as (name, arguments)).

    With ``tools``, only calls to one of them count. Tagged calls are taken
    wherever they are; a bare JSON call must be the whole reply.
    """
    known = tool_names(tools) if tools else None
    calls: List[Tuple[str, Dict[str, Any]]] = []

    def take(match: "re.Match[str]") -> str:
        raw = match.group("a") or match.group("b") or match.group("c")
        try:
            found = _as_calls(_loads(raw), known)
        except ValueError:
            found = _xml_calls(raw, known)
        if not found:
            return match.group(0)
        calls.extend(found)
        return ""

    rest = _TAGGED.sub(take, text)
    if not calls:
        try:
            calls = _as_calls(_loads(text), known)
        except ValueError:
            calls = []
        if calls:
            rest = ""
    return rest.strip(), calls


def extract_json(text: str) -> Optional[str]:
    """The JSON value in a reply (the reply itself, a fenced block, or the first object or array in it), re-serialized."""
    candidates = [text]
    start = min((i for i in (text.find("{"), text.find("[")) if i >= 0), default=-1)
    if start > 0:
        candidates.append(text[start:])
    for candidate in candidates:
        try:
            return json.dumps(_loads(candidate), ensure_ascii=False)
        except ValueError:
            continue
    return None


def validate_json(value: str, schema: Optional[Dict[str, Any]]) -> Optional[str]:
    """Why ``value`` does not match ``schema``, or None if it does (or there is no schema)."""
    if not schema:
        return None
    try:
        import jsonschema
    except ImportError:  # the check is best effort without it
        return None
    try:
        jsonschema.validate(json.loads(value), schema)
    except jsonschema.SchemaError as e:
        return f"invalid schema: {e.message}"
    except jsonschema.ValidationError as e:
        return e.message
    return None
