"""
Token counts, stop strings and log probabilities on /v1/chat/completions, and
the legacy /v1/completions endpoint evaluation harnesses use.
"""

import json

import pytest
from fastapi.testclient import TestClient

from api import app as app_module
from config import Config
from models.chat import ReplyStream, cut_at_stop, stop_list
from models.generic.transformers_model import TransformersModel
from tests.fakes import FakeModel, registered, swapped


class CountingModel(FakeModel):
    """Echoes the prompt, counts tokens as words, and makes up log probabilities when asked."""

    requests = []

    async def predict(self, request, on_text=None):
        CountingModel.requests.append(request)
        if request.tokens is not None:
            prompt = [f"t{i}" for i in request.tokens]
        else:
            prompt = request.text.split()
        new = ["echo:"] + prompt
        if request.max_length is not None:
            new = new[: request.max_length]
        if on_text is not None:
            for word in new:
                on_text(word + " ")
        result = {"text": " ".join(new), "prompt_tokens": len(prompt), "completion_tokens": len(new)}
        if request.logprobs is not None:
            tokens = (prompt if request.echo else []) + new
            logprobs = [None if request.echo and i == 0 else -0.1 * (i + 1) for i in range(len(tokens))]
            offsets, at = [], 0
            for token in tokens:
                offsets.append(at)
                at += len(token)
            result["logprobs"] = {
                "tokens": tokens,
                "token_logprobs": logprobs,
                "top_logprobs": None if not request.logprobs else [
                    None if lp is None else {tok: lp, "other": lp - 1} for tok, lp in zip(tokens, logprobs)
                ],
                "text_offset": offsets,
            }
        return result


@pytest.fixture(scope="module")
def client():
    with swapped(TransformersModel, CountingModel), TestClient(app_module.app) as c:
        yield c


def chat(client, **overrides):
    payload = {"model": "count/model", "messages": [{"role": "user", "content": "hi there"}], **overrides}
    return client.post("/v1/chat/completions", json=payload)


def complete(client, **overrides):
    return client.post("/v1/completions", json={"model": "count/model", "prompt": "hi there", **overrides})


def events(response):
    assert response.status_code == 200, response.text
    lines = [line[len("data: "):] for line in response.text.splitlines() if line.startswith("data: ")]
    assert lines[-1] == "[DONE]"
    return [json.loads(line) for line in lines[:-1]]


# ----------------------------------------------------------------- chat usage
def test_chat_reports_usage_and_length(client):
    r = chat(client)
    assert r.status_code == 200, r.text
    body = r.json()
    # The transcript "User: hi there\nAssistant:" is 4 words; the reply echoes them plus "echo:".
    assert body["usage"] == {"prompt_tokens": 4, "completion_tokens": 5, "total_tokens": 9}
    assert body["choices"][0]["finish_reason"] == "stop"

    body = chat(client, max_tokens=2).json()
    assert body["usage"]["completion_tokens"] == 2
    assert body["choices"][0]["finish_reason"] == "length"


def test_chat_stream_ends_with_usage(client):
    chunks = events(chat(client, stream=True))
    last = chunks[-1]
    assert last["choices"][0]["finish_reason"] == "stop"
    assert last["usage"]["completion_tokens"] == 5 and "toolkit" in last
    assert all("usage" not in c for c in chunks[:-1])

    chunks = events(chat(client, stream=True, stream_options={"include_usage": True}))
    # OpenAI's shape: a closing chunk with no choices carries the usage.
    assert chunks[-1]["choices"] == [] and chunks[-1]["usage"]["total_tokens"] == 9
    assert chunks[-2]["choices"][0]["finish_reason"] == "stop"


def test_models_without_counts_report_no_usage(client):
    with registered(FakeModel, task="text-generation", match="plain:*"):
        body = chat(client, model="plain:m").json()
    assert "usage" not in body and body["choices"][0]["finish_reason"] == "stop"


def test_stop_strings_cut_the_reply_and_reach_the_model(client):
    body = chat(client, stop="there").json()
    assert body["choices"][0]["message"]["content"] == "echo: User: hi"
    assert CountingModel.requests[-1].stop == ["there"]
    assert chat(client, stop=["a"] * 9).status_code == 422

    chunks = events(chat(client, stream=True, stop=["hi"]))
    text = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks)
    assert text == "echo: User:"


def test_chat_logprobs_in_the_chat_shape(client):
    body = chat(client, logprobs=True, top_logprobs=1, max_tokens=2).json()
    content = body["choices"][0]["logprobs"]["content"]
    assert [c["token"] for c in content] == ["echo:", "User:"]
    assert content[0]["logprob"] == pytest.approx(-0.1) and content[0]["bytes"] == list(b"echo:")
    assert content[0]["top_logprobs"][0]["token"] == "echo:"
    assert CountingModel.requests[-1].logprobs == 1
    assert "logprobs" not in chat(client).json()["choices"][0]


def test_logprobs_need_a_text_generation_model(client):
    with registered(FakeModel, task="vision-language", match="vis:*"):
        assert chat(client, model="vis:m", logprobs=True).status_code == 400


# ---------------------------------------------------------------- completions
def test_completion_of_a_text_prompt(client):
    r = complete(client, max_tokens=5)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["object"] == "text_completion" and body["id"].startswith("cmpl-")
    assert body["choices"] == [{"index": 0, "text": "echo: hi there", "logprobs": None, "finish_reason": "stop"}]
    assert body["usage"] == {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}
    request = CountingModel.requests[-1]
    assert request.text == "hi there" and request.max_length == 5 and request.messages is None

    assert complete(client).json()["choices"][0]["finish_reason"] == "stop"
    assert CountingModel.requests[-1].max_length == 16, "OpenAI's default"
    assert complete(client, max_tokens=1).json()["choices"][0]["finish_reason"] == "length"


def test_completion_of_several_prompts(client):
    body = complete(client, prompt=["a b", "c"], max_tokens=4).json()
    assert [c["index"] for c in body["choices"]] == [0, 1]
    assert [c["text"] for c in body["choices"]] == ["echo: a b", "echo: c"]
    assert body["usage"] == {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8}


def test_completion_scores_token_ids_with_echo_and_logprobs(client):
    # lm-evaluation-harness's loglikelihood request: the context and continuation as token IDs, no new tokens.
    body = complete(client, prompt=[7, 8, 9], echo=True, logprobs=1, max_tokens=0).json()
    request = CountingModel.requests[-1]
    assert request.tokens == [7, 8, 9] and request.echo is True and request.logprobs == 1 and request.max_length == 0
    choice = body["choices"][0]
    assert choice["text"] == "t7t8t9", "the echoed prompt, from the tokens' text"
    assert choice["logprobs"]["tokens"] == ["t7", "t8", "t9"]
    assert choice["logprobs"]["token_logprobs"][0] is None
    assert choice["logprobs"]["top_logprobs"][1] == {"t8": pytest.approx(-0.2), "other": pytest.approx(-1.2)}
    assert choice["logprobs"]["text_offset"] == [0, 2, 4]
    assert body["usage"] == {"prompt_tokens": 3, "completion_tokens": 0, "total_tokens": 3}

    body = complete(client, prompt=[[1, 2], [3]], max_tokens=1, logprobs=0).json()
    assert [c["text"] for c in body["choices"]] == ["echo:", "echo:"]
    assert body["choices"][0]["logprobs"]["top_logprobs"] is None

    # Echoing a text prompt puts it before the completion.
    choice = complete(client, echo=True, max_tokens=1).json()["choices"][0]
    assert choice["text"] == "hi thereecho:"


def test_completion_stop_and_stream(client):
    assert complete(client, stop=" there").json()["choices"][0]["text"] == "echo: hi"

    chunks = events(complete(client, stream=True, max_tokens=3, stream_options={"include_usage": True}))
    assert chunks[0]["object"] == "text_completion"
    assert "".join(c["choices"][0]["text"] for c in chunks if c["choices"]) == "echo: hi there "
    assert chunks[-2]["choices"][0]["finish_reason"] == "length" and "toolkit" in chunks[-2]
    assert chunks[-1]["choices"] == [] and chunks[-1]["usage"]["completion_tokens"] == 3

    chunks = events(complete(client, stream=True, stop=["there"]))
    assert "".join(c["choices"][0]["text"] for c in chunks if c["choices"]) == "echo: hi "


def test_completion_refusals(client, monkeypatch):
    assert complete(client, n=2).status_code == 400
    assert complete(client, suffix="!").status_code == 400
    assert complete(client, stream=True, logprobs=1).status_code == 400
    assert complete(client, stream=True, prompt=["a", "b"]).status_code == 400
    monkeypatch.setattr(Config, "MAX_COMPLETION_PROMPTS", 1)
    assert complete(client, prompt=["a", "b"]).status_code == 400
    monkeypatch.setattr(Config, "MAX_PROMPT_TOKENS", 2)
    assert complete(client, prompt=[1, 2, 3]).status_code == 400
    assert complete(client, logprobs=21).status_code == 422
    monkeypatch.setattr(Config, "ALLOWED_MODELS", ["other/model"])
    assert complete(client).status_code == 403


# -------------------------------------------------------------------- helpers
def test_stop_list_and_cut():
    assert stop_list(None) is None and stop_list("") is None and stop_list([]) is None
    assert stop_list("x") == ["x"] and stop_list(["a", "b"]) == ["a", "b"]
    with pytest.raises(ValueError):
        stop_list([""])
    assert cut_at_stop("one two three", ["three", "two"]) == "one "
    assert cut_at_stop("one", None) == "one"


def test_reply_stream_with_stop_strings_keeps_text_as_generated():
    stream = ReplyStream(("END",), strip=False)
    deltas = [stream.feed(p) for p in ["  hello ", "wor", "ld E", "ND more"]]
    deltas[-1] += stream.finish()
    assert deltas == ["  hello ", "wor", "ld ", ""]
    assert stream.ended

    plain = ReplyStream((), strip=False)
    assert plain.feed("a\nUser: b") == "a\nUser: b", "no transcript markers for raw completions"
    assert plain.finish() == ""
