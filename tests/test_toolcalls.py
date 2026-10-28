import json

import pytest

from models import toolcalls

TOOLS = [
    {"type": "function", "function": {"name": "get_weather", "parameters": {"type": "object"}}},
    {"type": "function", "function": {"name": "search", "parameters": {"type": "object"}}},
]


@pytest.mark.parametrize("text", [
    # Qwen / Hermes
    'Let me check.\n<tool_call>\n{"name": "get_weather", "arguments": {"city": "Oslo"}}\n</tool_call>',
    # Granite
    '<|tool_call|>[{"name": "get_weather", "arguments": {"city": "Oslo"}}]',
    # Mistral
    '[TOOL_CALLS][{"name": "get_weather", "arguments": {"city": "Oslo"}}]',
    # Llama 3 and models following the instruction: the whole reply is the call
    '{"name": "get_weather", "parameters": {"city": "Oslo"}}',
    # Qwen3-Coder (and the Qwen3-VL template): XML-style inside the tag
    'I will check.\n<tool_call>\n<function=get_weather>\n<parameter=city>\nOslo\n</parameter>\n</function>\n</tool_call>',
    '```json\n{"name": "get_weather", "arguments": "{\\"city\\": \\"Oslo\\"}"}\n```',
])
def test_tool_call_formats_are_parsed(text):
    rest, calls = toolcalls.parse_tool_calls(text, TOOLS)
    assert calls == [("get_weather", {"city": "Oslo"})]
    assert "tool_call" not in rest and "Oslo" not in rest


def test_several_tagged_calls_and_the_text_around_them():
    text = ('Sure. <tool_call>{"name": "search", "arguments": {"q": "a"}}</tool_call>'
            '<tool_call>{"name": "get_weather", "arguments": {}}</tool_call> Done.')
    rest, calls = toolcalls.parse_tool_calls(text, TOOLS)
    assert calls == [("search", {"q": "a"}), ("get_weather", {})]
    assert rest == "Sure.  Done."


def test_xml_parameters_keep_json_values_and_plain_strings():
    text = ('<tool_call><function=search><parameter=q>a "quoted" one</parameter>'
            '<parameter=limit>5</parameter><parameter=tags>["x", "y"]</parameter></function></tool_call>')
    assert toolcalls.parse_tool_calls(text, TOOLS)[1] == [("search", {"q": 'a "quoted" one', "limit": 5, "tags": ["x", "y"]})]


def test_unknown_functions_and_plain_json_are_not_calls():
    assert toolcalls.parse_tool_calls('{"name": "rm_rf", "arguments": {}}', TOOLS) == ('{"name": "rm_rf", "arguments": {}}', [])
    assert toolcalls.parse_tool_calls('{"city": "Oslo"}', TOOLS) == ('{"city": "Oslo"}', [])
    assert toolcalls.parse_tool_calls("<tool_call>not json</tool_call>", TOOLS) == ("<tool_call>not json</tool_call>", [])
    # Without a tool list, any well-formed call counts.
    assert toolcalls.parse_tool_calls('{"name": "anything", "arguments": {}}')[1] == [("anything", {})]


@pytest.mark.parametrize("text, expected", [
    ('{"a": 1}', {"a": 1}),
    ('```json\n{"a": 1}\n```', {"a": 1}),
    ('Here you go:\n{"a": [1, 2]}\nAnything else?', {"a": [1, 2]}),
    ("[1, 2]", [1, 2]),
])
def test_json_is_extracted_from_a_reply(text, expected):
    assert json.loads(toolcalls.extract_json(text)) == expected


def test_no_json_means_none():
    assert toolcalls.extract_json("I cannot do that.") is None
    assert toolcalls.extract_json("{broken") is None


def test_json_is_validated_against_the_schema():
    schema = {"type": "object", "properties": {"a": {"type": "integer"}}, "required": ["a"]}
    assert toolcalls.validate_json('{"a": 1}', schema) is None
    assert "required" in toolcalls.validate_json("{}", schema)
    assert toolcalls.validate_json('{"a": "x"}', {}) is None  # any JSON


def test_instructions_cover_tools_choice_and_schema():
    text = toolcalls.instructions(TOOLS, "search", {"type": "object"})
    assert '"name": "get_weather"' in text and "<tool_call>" in text
    assert "You must call the function search now." in text
    assert text.endswith('{"type": "object"}')
    assert toolcalls.instructions(TOOLS, "none", None) == ""
    assert toolcalls.instructions(None, None, {}) == "Reply with a single JSON object and nothing else."
    # listed=False: a template renders the tools; only the choice is spelled out.
    assert toolcalls.instructions(TOOLS, "required", None, listed=False) == "You must call at least one function now."
    assert toolcalls.instructions(TOOLS, "auto", None, listed=False) == ""


def test_instructions_join_the_system_turn():
    turns = [{"role": "system", "content": "Be brief."}, {"role": "user", "content": "hi"}]
    out = toolcalls.with_instructions(turns, "Reply in JSON.")
    assert out[0] == {"role": "system", "content": "Be brief.\n\nReply in JSON."}
    assert turns[0]["content"] == "Be brief.", "the caller's turns must not change"
    assert toolcalls.with_instructions(turns[1:], "Reply in JSON.")[0] == {"role": "system", "content": "Reply in JSON."}
    assert toolcalls.with_instructions(turns, "") is turns
