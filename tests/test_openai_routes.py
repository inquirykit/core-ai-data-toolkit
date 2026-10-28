import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from api import app as app_module
from config import Config
from models.chat import ChatMessage, ReplyStream, completion_text, trim_messages
from models.generic.transformers_model import TransformersModel
from tests.fakes import FakeModel, registered, swapped


class TranscriptModel(FakeModel):
    """Answers like a causal LM: the reply, then more transcript."""

    calls = []

    async def predict(self, request):
        # The fields the route set, so tests can tell them from the contract's defaults.
        TranscriptModel.calls.append((request.text, request.model_dump(exclude_unset=True)))
        return {"text": " Hello there.\nUser: and I keep going"}


@pytest.fixture(scope="module")
def client():
    with swapped(TransformersModel, TranscriptModel), TestClient(app_module.app) as c:
        yield c


def chat(client, **overrides):
    payload = {"model": "chat/model", "messages": [{"role": "user", "content": "hi"}], **overrides}
    return client.post("/v1/chat/completions", json=payload)


def test_chat_completion_returns_only_the_reply(client):
    r = chat(client, messages=[
        {"role": "system", "content": "Be brief."},
        {"role": "user", "content": "hi"},
    ])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"] == {"role": "assistant", "content": "Hello there."}
    assert body["choices"][0]["finish_reason"] == "stop"

    prompt, _ = TranscriptModel.calls[-1]
    assert prompt == "System: Be brief.\nUser: hi\nAssistant:"


def test_streamed_completion_is_openai_sse(client):
    r = chat(client, stream=True)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/event-stream")

    lines = [line[len("data: "):] for line in r.text.splitlines() if line.startswith("data: ")]
    assert lines[-1] == "[DONE]"
    chunks = [json.loads(line) for line in lines[:-1]]
    assert "".join(c["choices"][0]["delta"].get("content", "") for c in chunks) == "Hello there."
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"


def test_tool_fields_and_null_content_are_accepted(client):
    r = chat(
        client,
        messages=[
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "t1", "type": "function"}]},
            {"role": "tool", "tool_call_id": "t1", "content": "42"},
        ],
        tools=[{"type": "function", "function": {"name": "x", "parameters": {}}}],
        stream_options={"include_usage": True},
    )
    assert r.status_code == 200, r.text
    prompt, kwargs = TranscriptModel.calls[-1]
    # The transcript (for models without a chat template) opens with the tool instruction.
    assert prompt.startswith("System: You can call these functions")
    assert prompt.endswith("\nUser: hi\nTool: 42\nAssistant:")
    assert kwargs["tools"] == [{"type": "function", "function": {"name": "x", "parameters": {}}}]
    # A tool call without a function is dropped; the tool turn keeps its id.
    assert kwargs["messages"][1:] == [{"role": "tool", "content": "42", "tool_call_id": "t1"}]


def test_temperature_zero_means_greedy(client):
    assert chat(client, temperature=0).status_code == 200
    _, kwargs = TranscriptModel.calls[-1]
    assert kwargs["do_sample"] is False
    assert "temperature" not in kwargs


def test_models_are_listed_with_their_task(client):
    r = client.get("/v1/models")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["object"] == "list"
    assert body["data"], "expected at least one model"
    for model in body["data"]:
        assert model["object"] == "model"
        assert model["id"] and model["task"]
    assert any(m["task"] == "text-generation" for m in body["data"])


def test_v1_routes_require_the_api_key_when_set(client, monkeypatch):
    monkeypatch.setattr(Config, "API_KEY", "secret")
    assert client.get("/v1/models").status_code == 401
    assert chat(client).status_code == 401
    assert client.get("/v1/models", headers={"Authorization": "Bearer secret"}).status_code == 200


def test_reply_cut_when_model_starts_next_turn_immediately():
    # Models may return the new text already stripped.
    assert completion_text("User: what else?\nAssistant: ok") == ""
    assert completion_text("Sure.\nUser: more") == "Sure."


def test_models_list_follows_allowed_models(client, monkeypatch):
    monkeypatch.setattr(Config, "ALLOWED_MODELS", ["distilgpt2", "Qwen/Qwen2-0.5B-Instruct"])
    data = client.get("/v1/models").json()["data"]
    assert {(m["id"], m["task"]) for m in data} == {
        ("distilgpt2", "text-generation"),
        # Outside the built-in catalog, still listed so it can be chosen.
        ("Qwen/Qwen2-0.5B-Instruct", "text-generation"),
    }
    native = client.get("/models/list").json()
    assert sorted(m for ids in native.values() for m in ids) == ["Qwen/Qwen2-0.5B-Instruct", "distilgpt2"]
    assert chat(client, model="gpt2").status_code == 403


def test_model_also_gets_the_chat_as_messages(client):
    assert chat(client, messages=[
        {"role": "system", "content": " Be brief. "},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": None},
    ]).status_code == 200
    _, kwargs = TranscriptModel.calls[-1]
    assert kwargs["messages"] == [{"role": "system", "content": "Be brief."}, {"role": "user", "content": "hi"}]


def test_reply_reports_server_time_and_cold_start(client):
    first = chat(client, model="chat/cold").json()["toolkit"]
    second = chat(client, model="chat/cold").json()["toolkit"]
    assert first["cold_start"] is True and second["cold_start"] is False
    assert second["processing_ms"] >= 0


def test_long_chats_drop_the_oldest_turns_but_keep_the_system_prompt(monkeypatch):
    monkeypatch.setattr(Config, "MAX_INPUT_CHARS", 10)
    msgs = [ChatMessage(role=r, content=c) for r, c in [
        ("system", "sys"), ("user", "aaaa"), ("assistant", "bbbb"), ("user", "cc"),
    ]]
    assert trim_messages(msgs) == [
        {"role": "system", "content": "sys"}, {"role": "assistant", "content": "bbbb"}, {"role": "user", "content": "cc"},
    ]
    # A single over-long turn keeps its end.
    assert trim_messages([ChatMessage(role="user", content="0123456789abc")]) == [{"role": "user", "content": "3456789abc"}]
    # An over-long system prompt is shortened too (keeping its start), so the turns always fit.
    msgs = [ChatMessage(role="system", content="0123456789abcdef"), ChatMessage(role="user", content="hi")]
    assert trim_messages(msgs) == [{"role": "system", "content": "01234567"}, {"role": "user", "content": "hi"}]


class StreamingModel(FakeModel):
    """Writes its reply in pieces through on_text, running on past the reply like a causal LM."""

    pieces = ["  Hel", "lo the", "re.\nUs", "er: and I keep going", " and going"]
    answers = []
    fail_after = None

    async def predict(self, request, on_text=None):
        for i, piece in enumerate(self.pieces):
            if i == self.fail_after:
                raise RuntimeError("GPU fell over")
            StreamingModel.answers.append(on_text(piece))
            # Let the route read the piece before the next one, as a real model's pace would.
            await asyncio.sleep(0.01)
        return {"text": "".join(self.pieces)}


class BrokenModel(FakeModel):
    async def load(self):
        raise RuntimeError("weights missing")


def stream_events(response):
    assert response.status_code == 200, response.text
    lines = [line[len("data: "):] for line in response.text.splitlines() if line.startswith("data: ")]
    assert lines[-1] == "[DONE]"
    return [json.loads(line) for line in lines[:-1]]


def streamed_text(events):
    return "".join(e["choices"][0]["delta"].get("content", "") for e in events if "choices" in e)


def test_stream_arrives_piece_by_piece_and_stops_at_the_next_turn(client):
    StreamingModel.answers = []
    with registered(StreamingModel, task="text-generation", match="stream:*"):
        events = stream_events(chat(client, model="stream:a", stream=True))
    deltas = [e["choices"][0]["delta"].get("content") for e in events]
    assert deltas[0] == "" and events[0]["choices"][0]["delta"]["role"] == "assistant"
    assert len([d for d in deltas if d]) > 1, "expected several content chunks"
    assert streamed_text(events) == "Hello there."
    assert events[-1]["choices"][0]["finish_reason"] == "stop"
    assert events[-1]["toolkit"]["cold_start"] is True
    # Once the reply ended, the model was told to stop.
    assert StreamingModel.answers[0] is True and StreamingModel.answers[-1] is False


def test_stream_load_failure_is_an_http_error(client):
    with registered(BrokenModel, task="text-generation", match="broken:*"):
        r = chat(client, model="broken:a", stream=True)
    assert r.status_code == 500
    assert r.json()["detail"] == "Internal error (RuntimeError)"


def test_stream_failure_mid_reply_is_an_error_event(client, monkeypatch):
    monkeypatch.setattr(StreamingModel, "fail_after", 2)
    with registered(StreamingModel, task="text-generation", match="stream:*"):
        events = stream_events(chat(client, model="stream:b", stream=True))
    assert streamed_text(events) == "Hello the"
    assert events[-1] == {"error": {"message": "Internal error (RuntimeError)", "type": "server_error"}}


def test_text_parts_are_joined_and_unknown_parts_refused(client):
    r = chat(client, messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}, {"type": "text", "text": "there"}]}])
    assert r.status_code == 200, r.text
    prompt, _ = TranscriptModel.calls[-1]
    assert prompt == "User: hi\nthere\nAssistant:"
    r = chat(client, messages=[{"role": "user", "content": [{"type": "file", "file": {"file_id": "f"}}]}])
    assert r.status_code == 422
    assert "content parts must be one of" in r.text


class VisionModel(FakeModel):
    """Records the vision-language input it gets."""

    requests = []

    async def predict(self, request):
        VisionModel.requests.append(request)
        return {"text": "A cat."}


IMAGE = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA", "detail": "low"}}
AUDIO = {"type": "input_audio", "input_audio": {"data": "AAAA", "format": "wav"}}


@pytest.fixture
def vision():
    VisionModel.requests = []
    with registered(VisionModel, task="vision-language", match="vision:*"):
        yield


def test_images_and_audio_reach_a_vision_model_as_sent(client, vision):
    r = chat(client, model="vision:m", messages=[
        {"role": "system", "content": "Be brief."},
        {"role": "user", "content": [{"type": "text", "text": "what is this?"}, IMAGE, AUDIO]},
    ])
    assert r.status_code == 200, r.text
    assert r.json()["choices"][0]["message"]["content"] == "A cat."
    request = VisionModel.requests[-1]
    assert request.text == "System: Be brief.\nUser: what is this?\nAssistant:"
    turns = [t.model_dump(exclude_none=True) for t in request.messages]
    assert turns == [
        {"role": "system", "content": "Be brief."},
        {"role": "user", "content": [
            {"type": "text", "text": "what is this?"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
            {"type": "input_audio", "input_audio": {"data": "AAAA", "format": "wav"}},
        ]},
    ]


def test_a_vision_model_also_takes_plain_text_chats(client, vision):
    assert chat(client, model="vision:m").status_code == 200
    assert VisionModel.requests[-1].messages[0].content == "hi"


def test_media_for_a_text_only_model_is_refused(client):
    r = chat(client, model="Qwen/Qwen2.5-0.5B-Instruct", messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}, IMAGE]}])
    assert r.status_code == 400
    assert "does not take images" in r.json()["detail"]


@pytest.mark.parametrize("part", [
    {"type": "image_url", "image_url": {"url": "https://example.com/cat.png"}},
    {"type": "image_url", "image_url": {"url": "data:image/png;base64,not base64!"}},
    {"type": "input_audio", "input_audio": {"data": "AAAA", "format": "flac"}},
])
def test_remote_or_broken_media_is_a_client_error(client, vision, part):
    r = chat(client, model="vision:m", messages=[{"role": "user", "content": [part]}])
    assert r.status_code == 400, r.text
    assert VisionModel.requests == []


@pytest.mark.parametrize("pieces, expected", [
    (["Sure", ".", "\n", "Us", "er:", " more"], ["Sure", ".", "", "", "", ""]),
    # "\nUs" could start "\nUser:", so it waits until "eful" shows it does not.
    (["\n\n  Hi", " there", "\nUs", "eful"], ["Hi", " there", "", "\nUseful"]),
    (["User: what?"], [""]),
])
def test_reply_stream_holds_back_possible_markers(pieces, expected):
    stream = ReplyStream()
    deltas = [stream.feed(p) for p in pieces]
    deltas[-1] += stream.finish()
    assert deltas == expected
    whole = "".join(pieces)
    assert "".join(deltas) == completion_text(whole)


class EmbedModel(FakeModel):
    async def predict(self, request):
        return {"embeddings": [[float(len(t)), 0.5] for t in request.texts], "tokens": 7}


class SpeechModel(FakeModel):
    requests = []

    async def predict(self, request):
        SpeechModel.requests.append(request)
        return {"text": f" heard {len(request.audio)} bytes ", "language": request.language}


def test_embeddings_for_a_string_or_a_list(client):
    with registered(EmbedModel, task="embedding", match="embed:*"):
        one = client.post("/v1/embeddings", json={"model": "embed:m", "input": "hello"})
        many = client.post("/v1/embeddings", json={"model": "embed:m", "input": ["a", "bcd"], "user": "u1"})
    assert one.status_code == 200, one.text
    body = one.json()
    assert body["object"] == "list" and body["model"] == "embed:m"
    assert body["data"] == [{"object": "embedding", "index": 0, "embedding": [5.0, 0.5]}]
    assert body["usage"] == {"prompt_tokens": 7, "total_tokens": 7}
    assert [d["embedding"] for d in many.json()["data"]] == [[1.0, 0.5], [3.0, 0.5]]


def test_embeddings_as_base64_floats(client):
    import base64
    import struct

    with registered(EmbedModel, task="embedding", match="embed:*"):
        r = client.post("/v1/embeddings", json={"model": "embed:m", "input": "hi", "encoding_format": "base64"})
    raw = base64.b64decode(r.json()["data"][0]["embedding"])
    assert struct.unpack("<2f", raw) == (2.0, 0.5)


@pytest.mark.parametrize("payload, status", [
    ({"model": "embed:m", "input": "hi", "dimensions": 64}, 400),
    ({"model": "embed:m", "input": []}, 400),
    ({"model": "embed:m", "input": [[1, 2, 3]]}, 422),
    ({"model": "Qwen/Qwen2.5-0.5B-Instruct", "input": "hi"}, 400),  # listed for text-generation only
])
def test_embedding_request_errors(client, payload, status):
    with registered(EmbedModel, task="embedding", match="embed:*"):
        assert client.post("/v1/embeddings", json=payload).status_code == status


def test_transcription_json_and_text(client):
    SpeechModel.requests = []
    files = {"file": ("talk.mp3", b"ID3 fake mp3 bytes", "audio/mpeg")}
    with registered(SpeechModel, task="speech-to-text", match="stt:*"):
        r = client.post("/v1/audio/transcriptions", data={"model": "stt:m", "language": "en"}, files=files)
        assert r.status_code == 200, r.text
        assert r.json()["text"] == "heard 18 bytes"
        request = SpeechModel.requests[-1]
        assert (request.filename, request.language, request.audio) == ("talk.mp3", "en", b"ID3 fake mp3 bytes")

        r = client.post("/v1/audio/transcriptions", data={"model": "stt:m", "response_format": "text"}, files=files)
        assert r.headers["content-type"].startswith("text/plain") and r.text == "heard 18 bytes"
        bad = client.post("/v1/audio/transcriptions", data={"model": "stt:m", "response_format": "srt"}, files=files)
        assert bad.status_code == 422


def test_transcription_upload_limit(client, monkeypatch):
    monkeypatch.setattr(Config, "MAX_AUDIO_BYTES", 10)
    with registered(SpeechModel, task="speech-to-text", match="stt:*"):
        r = client.post("/v1/audio/transcriptions", data={"model": "stt:m"},
                        files={"file": ("a.wav", b"x" * 11, "audio/wav")})
    assert r.status_code == 413


def test_models_show_whether_they_are_loaded(client, monkeypatch):
    monkeypatch.setattr(Config, "ALLOWED_MODELS", ["embed:status"])

    def status():
        data = client.get("/v1/models").json()["data"]
        return {(m["id"], m["task"]): m["status"] for m in data}

    with registered(EmbedModel, task="embedding", ids=["embed:status"]):
        assert status()[("embed:status", "embedding")] == "unloaded"
        client.post("/v1/embeddings", json={"model": "embed:status", "input": "hi"})
        assert status()[("embed:status", "embedding")] == "loaded"


# ------------------------------------------------------------ tools and JSON
class ToolModel(FakeModel):
    """Answers a tool call when given tools, JSON (fenced, with chatter) when asked for it."""

    async def predict(self, request):
        ToolModel.last_request = request
        if request.tools and request.tool_choice != "none":
            return {"text": 'Checking. <tool_call>{"name": "lookup", "arguments": {"q": "hi"}}</tool_call>'}
        if request.json_schema is not None:
            return {"text": 'Sure:\n```json\n{"answer": 42}\n```'}
        return {"text": "plain"}


class NativeToolModel(FakeModel):
    """A model (server) that returns structured tool calls itself."""

    async def predict(self, request):
        return {"text": "", "tool_calls": [{"id": "srv_1", "function": {"name": "lookup", "arguments": '{"q": 1}'}}]}


TOOLS = [{"type": "function", "function": {"name": "lookup", "description": "Look up", "parameters": {"type": "object"}}}]


@pytest.fixture
def tool_client(client):
    with registered(ToolModel, task="text-generation", match="tools/*"), \
            registered(NativeToolModel, task="text-generation", match="native/*"):
        yield client


def test_tool_calls_in_the_reply_end_it_with_tool_calls(tool_client):
    r = chat(tool_client, model="tools/m", tools=TOOLS)
    assert r.status_code == 200, r.text
    choice = r.json()["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    [call] = choice["message"]["tool_calls"]
    assert call["id"].startswith("call_") and call["type"] == "function"
    assert call["function"] == {"name": "lookup", "arguments": '{"q": "hi"}'}
    assert choice["message"]["content"] == "Checking."
    # The task input carried the tools and the choice.
    assert ToolModel.last_request.tools == TOOLS
    assert ToolModel.last_request.tool_choice is None


def test_native_tool_calls_are_passed_through(tool_client):
    choice = chat(tool_client, model="native/m", tools=TOOLS).json()["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["content"] is None
    assert choice["message"]["tool_calls"] == [
        {"id": "srv_1", "type": "function", "function": {"name": "lookup", "arguments": '{"q": 1}'}},
    ]


def test_tool_choice_none_and_a_named_function(tool_client):
    r = chat(tool_client, model="tools/m", tools=TOOLS, tool_choice="none")
    assert r.json()["choices"][0]["message"]["content"] == "plain"
    assert ToolModel.last_request.tools is None
    r = chat(tool_client, model="tools/m", tools=TOOLS, tool_choice={"type": "function", "function": {"name": "lookup"}})
    assert r.json()["choices"][0]["finish_reason"] == "tool_calls"
    assert ToolModel.last_request.tool_choice == "lookup"
    r = chat(tool_client, model="tools/m", tools=TOOLS, tool_choice={"type": "function", "function": {"name": "nope"}})
    assert r.status_code == 400


def test_json_replies_are_extracted_and_checked(tool_client):
    r = chat(tool_client, model="tools/m", response_format={"type": "json_object"})
    assert r.status_code == 200, r.text
    assert r.json()["choices"][0]["message"]["content"] == '{"answer": 42}'
    assert ToolModel.last_request.json_schema == {}

    schema = {"type": "object", "properties": {"answer": {"type": "integer"}}, "required": ["answer"]}
    r = chat(tool_client, model="tools/m", response_format={"type": "json_schema", "json_schema": {"name": "a", "schema": schema}})
    assert r.status_code == 200, r.text
    assert ToolModel.last_request.json_schema == schema

    wrong = {"type": "object", "properties": {"answer": {"type": "string"}}}
    r = chat(tool_client, model="tools/m", response_format={"type": "json_schema", "json_schema": {"name": "a", "schema": wrong}})
    assert r.status_code == 502
    assert "does not match" in r.json()["detail"]


def test_a_reply_without_json_is_a_502(tool_client):
    # TranscriptModel answers prose whatever it is asked.
    r = chat(tool_client, response_format={"type": "json_object"})
    assert r.status_code == 502
    assert "did not reply with JSON" in r.json()["detail"]


def test_structured_replies_stream_as_one_chunk(tool_client):
    r = chat(tool_client, model="tools/m", tools=TOOLS, stream=True)
    assert r.status_code == 200, r.text
    lines = [line[len("data: "):] for line in r.text.splitlines() if line.startswith("data: ")]
    chunks = [json.loads(line) for line in lines[:-1]]
    assert lines[-1] == "[DONE]"
    deltas = [c["choices"][0]["delta"] for c in chunks]
    assert deltas[0] == {"role": "assistant", "content": ""}
    assert deltas[1] == {"content": "Checking."}
    [call] = deltas[2]["tool_calls"]
    assert call["index"] == 0 and call["function"]["name"] == "lookup"
    assert chunks[-1]["choices"][0]["finish_reason"] == "tool_calls"
    assert "toolkit" in chunks[-1]


def test_tools_need_a_text_generation_model(client):
    image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}
    r = chat(client, model="any/model", messages=[{"role": "user", "content": [image]}], tools=TOOLS)
    assert r.status_code == 400


def test_assistant_tool_calls_and_tool_turns_reach_the_model(tool_client):
    r = chat(tool_client, model="tools/m", tools=TOOLS, tool_choice="none", messages=[
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "lookup", "arguments": {"q": "hi"}}},
        ]},
        {"role": "tool", "tool_call_id": "c1", "name": "lookup", "content": "42"},
    ])
    assert r.status_code == 200, r.text
    turns = [t.model_dump(exclude_none=True) for t in ToolModel.last_request.messages]
    assert turns[1] == {"role": "assistant", "content": "", "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "lookup", "arguments": '{"q": "hi"}'}},
    ]}
    assert turns[2] == {"role": "tool", "content": "42", "tool_call_id": "c1", "name": "lookup"}
    # The transcript shows the call as text.
    assert '<tool_call>{"name": "lookup", "arguments": {"q": "hi"}}</tool_call>' in ToolModel.last_request.text


# ---------------------------------------------------------------------- rerank
def test_rerank_scores_documents_best_first(client, monkeypatch):
    with registered(FakeModel, task="rerank", ids=["rerank:m"]):
        r = client.post("/v1/rerank", json={
            "model": "rerank:m", "query": "q", "documents": ["short", {"text": "the longest one"}, "medium one"],
            "top_n": 2, "return_documents": True,
        })
        assert r.status_code == 200, r.text
        body = r.json()
        assert [x["index"] for x in body["results"]] == [1, 2]
        assert body["results"][0]["document"] == {"text": "the longest one"}
        assert body["results"][0]["relevance_score"] > body["results"][1]["relevance_score"]
        assert body["usage"] == {"total_tokens": 7}
        assert FakeModel.last_request.top_n == 2

        r = client.post("/v1/rerank", json={"model": "rerank:m", "query": "q", "documents": ["a"]})
        assert "document" not in r.json()["results"][0]
        assert client.post("/v1/rerank", json={"model": "rerank:m", "query": "q", "documents": []}).status_code == 422
