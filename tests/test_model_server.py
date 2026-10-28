import asyncio
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from models import memory, model_server, registry
from models.manager import ModelManager
from models.model_server import ServerModel, parse_specs
from models.server_specs import Provider, parse_entry
from tests.fakes import FakeModel, registered

FAKE = str(Path(__file__).resolve().parent / "fake_model_server.py")
CMD = (sys.executable, FAKE, "--port", "{port}")


@pytest.fixture(autouse=True)
def clean_registry():
    registry.discover()
    saved_entries, saved_specs = list(registry._entries), dict(model_server._specs)
    saved_providers = dict(model_server._providers)
    saved_tasks = ServerModel.__dict__.get("tasks")
    yield
    registry._entries[:] = saved_entries
    model_server._specs.clear()
    model_server._specs.update(saved_specs)
    model_server._providers.clear()
    model_server._providers.update(saved_providers)
    model_server._failed.clear()
    if saved_tasks is None:
        ServerModel.__dict__.get("tasks") is not None and delattr(ServerModel, "tasks")
    else:
        ServerModel.tasks = saved_tasks


def serve(**specs):
    """Serve each model ID by its entry (flat, or with backends), started from the fake server by default."""
    def entry(kw):
        if "backends" in kw:
            return {"task": "text-generation", **kw}
        return {"task": "text-generation", "cmd": list(CMD), "startup_timeout": 20, **kw}

    model_server.register_specs({model_id: parse_entry(model_id, entry(kw)) for model_id, kw in specs.items()})


def chat(text, **kw):
    return {"text": text, "messages": [{"role": "user", "content": text}], "max_length": 8, **kw}


def start_fake(*args):
    """The fake server as one that runs elsewhere (Ollama, say): its port and process, once it answers."""
    port = model_server._free_port()
    proc = subprocess.Popen([sys.executable, FAKE, "--port", str(port), *args])
    for _ in range(100):
        try:
            httpx.get(f"http://127.0.0.1:{port}/v1/models", timeout=1)
            break
        except httpx.HTTPError:
            time.sleep(0.05)
    return port, proc


def provider(name, port, **kw):
    model_server.register_specs({}, {name: Provider(url=f"http://127.0.0.1:{port}", **kw)})


STARTED = {"cmd": list(CMD), "startup_timeout": 20}
# The label a cmd backend of the fake server gets: its program's name.
PYTHON = Path(sys.executable).name


def run(scenario):
    async def wrapped():
        try:
            return await scenario()
        finally:
            await model_server.stop_all()
    return asyncio.run(wrapped())


def test_specs_are_validated():
    specs = parse_specs({"models": {
        "a": {"task": "vision-language", "cmd": "mlx_vlm.server --port {port}", "memory_gb": 3},
        "b": {"task": "text-generation", "url": "http://gpu:8000"},
    }})
    [a], [b] = specs["a"].backends, specs["b"].backends
    assert a.cmd == ("mlx_vlm.server", "--port", "{port}") and a.kind == "cmd" and a.memory_gb == 3
    assert b.kind == "url" and b.url == "http://gpu:8000"
    for bad in (
        {"task": "text-classification", "url": "http://x"},
        {"task": "text-generation"},
        {"task": "text-generation", "cmd": "x", "url": "http://x"},
        {"task": "text-generation", "url": "http://x", "colour": "red"},
        {"task": "text-generation", "url": "http://x", "on_evict": "pause"},
    ):
        with pytest.raises(ValueError):
            parse_specs({"models": {"m": bad}})


def test_server_starts_on_demand_answers_streams_and_stops():
    serve(**{"local/chat": {"upstream_model": "org/weights", "memory_gb": 2}})

    async def scenario():
        mgr = ModelManager(device="cpu", max_memory_gb=10)
        out = await mgr.run_inference("local/chat", "text-generation", chat("hi"))
        assert out["cold_start"] is True
        assert out["result"].text == "echo: hi (model=org/weights, max_tokens=8)"

        pieces = []
        out = await mgr.run_inference("local/chat", "text-generation", chat("again"), on_text=pieces.append)
        assert out["cold_start"] is False
        assert "".join(pieces) == out["result"].text == "echo: again (model=org/weights, max_tokens=8) "

        status = mgr.get_cache_status()
        assert status["memory_used_gb"] == pytest.approx(2)
        assert status["model_details"]["local/chat:text-generation"]["kind"] == "server"

        process = mgr.cache.get_model("local/chat:text-generation").process
        await mgr.unload_model("local/chat", "text-generation")
        assert process.returncode is not None

    run(scenario)


def test_server_refusal_is_a_client_error():
    serve(**{"local/chat": {}})

    async def scenario():
        mgr = ModelManager(device="cpu")
        with pytest.raises(ValueError, match="answered 400"):
            await mgr.run_inference("local/chat", "text-generation", chat("bad"))

    run(scenario)


def test_crashed_server_is_dropped_and_restarted():
    serve(**{"local/chat": {}})

    async def scenario():
        mgr = ModelManager(device="cpu")
        await mgr.run_inference("local/chat", "text-generation", chat("hi"))
        first = mgr.cache.get_model("local/chat:text-generation").process.pid
        with pytest.raises(httpx.HTTPError):
            await mgr.run_inference("local/chat", "text-generation", chat("crash"))
        # Give the exit a moment to be noticed, as a later request would.
        for _ in range(50):
            if mgr.get_cache_status()["cache_size"] == 0:
                break
            await asyncio.sleep(0.05)
        out = await mgr.run_inference("local/chat", "text-generation", chat("hi"))
        assert out["cold_start"] is True
        assert mgr.cache.get_model("local/chat:text-generation").process.pid != first

    run(scenario)


def test_server_that_fails_to_start_is_reported():
    serve(**{"local/broken": {"cmd": (*CMD, "--fail")}})

    async def scenario():
        mgr = ModelManager(device="cpu")
        with pytest.raises(RuntimeError, match="exited with code 7"):
            await mgr.run_inference("local/broken", "text-generation", chat("hi"))
        assert mgr.get_cache_status()["cache_size"] == 0
        # Its only backend is not skipped afterwards: the next request tries it again.
        with pytest.raises(RuntimeError, match="exited with code 7"):
            await mgr.run_inference("local/broken", "text-generation", chat("hi"))

    run(scenario)


def test_sleep_mode_keeps_the_process_and_wakes_it():
    serve(**{"local/sleepy": {"on_evict": "sleep"}, "local/other": {}})

    async def scenario():
        mgr = ModelManager(device="cpu", max_models=1)
        await mgr.run_inference("local/sleepy", "text-generation", chat("hi"))
        process = mgr.cache.get_model("local/sleepy:text-generation").process
        # Loading another model evicts it: it sleeps instead of exiting.
        await mgr.run_inference("local/other", "text-generation", chat("hi"))
        assert process.returncode is None and "local/sleepy" in model_server._sleeping
        out = await mgr.run_inference("local/sleepy", "text-generation", chat("back"))
        assert out["result"].text.startswith("echo: back")
        assert mgr.cache.get_model("local/sleepy:text-generation").process is process
        await model_server.stop_all()
        assert process.returncode is not None

    run(scenario)


def test_url_server_is_used_but_never_stopped():
    port, proc = start_fake()
    try:
        model_server.register_specs({"remote/chat": parse_entry(
            "remote/chat", {"task": "vision-language", "url": f"http://127.0.0.1:{port}"})})

        async def scenario():
            mgr = ModelManager(device="cpu")
            out = await mgr.run_inference("remote/chat", "vision-language", {
                "text": "x",
                "messages": [{"role": "user", "content": [
                    {"type": "text", "text": "what?"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
                ]}],
            })
            assert out["result"].text.startswith("echo: what? [image_url]")
            assert mgr.get_cache_status()["memory_used_gb"] == 0
            await mgr.clear_all_models()

        run(scenario)
        assert proc.poll() is None
    finally:
        proc.kill()


def test_configured_servers_are_allowed_and_listed(monkeypatch):
    from api import deps
    from config import Config

    serve(**{"local/chat": {}})
    monkeypatch.setattr(Config, "ALLOWED_MODELS", ["gpt2"])
    deps.check_model_allowed("local/chat")
    with pytest.raises(Exception):
        deps.check_model_allowed("other/model")
    assert registry.resolve("local/chat", "text-generation") is ServerModel
    assert "local/chat" in registry.catalog()["text-generation"]


def test_estimate_falls_back_to_the_weights(monkeypatch):
    monkeypatch.setattr(memory, "estimate_bytes", lambda model_id, *a: 123 if model_id == "org/weights" else None)
    serve(**{"local/chat": {"upstream_model": "org/weights"}})
    model = ServerModel("local/chat", "text-generation", "cpu", {})
    assert asyncio.run(model.estimate_bytes()) == 123


def test_streamed_image_chat_through_the_api():
    import json

    from fastapi.testclient import TestClient

    from api import app as app_module

    serve(**{"local/vlm": {"task": "vision-language"}})
    with TestClient(app_module.app) as client:
        r = client.post("/v1/chat/completions", json={
            "model": "local/vlm",
            "stream": True,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": "what?"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
            ]}],
        })
        assert r.status_code == 200, r.text
        chunks = [json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data: {")]
        text = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks)
        assert text.startswith("echo: what? [image_url]")
        assert chunks[-1]["toolkit"]["cold_start"] is True
    # Leaving the client shuts the app down, which stops the server.
    assert model_server._processes == {}


def test_embeddings_and_transcriptions_go_to_their_server():
    serve(**{"local/embed": {"task": "embedding"}, "local/stt": {"task": "speech-to-text", "upstream_model": "whisper-1"}})

    async def scenario():
        mgr = ModelManager(device="cpu")
        out = await mgr.run_inference("local/embed", "embedding", {"texts": ["a", "bcd"]})
        # Put back in input order, whatever order the server answered in.
        assert out["result"].embeddings == [[1.0, 1.0], [3.0, 1.0]]
        assert out["result"].tokens == 3
        import base64
        out = await mgr.run_inference("local/stt", "speech-to-text", {
            "audio": base64.b64encode(b"RIFF1234").decode(), "filename": "a.wav", "language": "de",
        })
        assert out["result"].text == "8 bytes of a.wav for whisper-1"
        assert out["result"].language == "de"

    run(scenario)


def test_toml_description_and_tags_reach_the_catalog():
    specs = parse_specs({"models": {
        "local/chat": {"task": "text-generation", "url": "http://x", "description": "Remote chat", "tags": ["chat"]},
    }})
    model_server.register_specs(specs)
    assert registry.cards()["text-generation"]["local/chat"] == registry.ModelCard("local/chat", "Remote chat", ("chat",))
    # A server that runs elsewhere loads no files here; a managed one loads its upstream model's.
    assert ServerModel.weights_source("local/chat") is None
    serve(**{"mlx/chat": {"upstream_model": "org/weights"}})
    assert ServerModel.weights_source("mlx/chat") == "org/weights"


def test_tools_and_json_mode_go_to_the_server():
    serve(**{"local/chat": {}})
    tools = [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}]

    async def scenario():
        mgr = ModelManager(device="cpu")
        out = await mgr.run_inference("local/chat", "text-generation", chat("hi", tools=tools, tool_choice="required"))
        [call] = out["result"].tool_calls
        assert (call.id, call.function.name, call.function.arguments) == ("call_fake", "lookup", '{"q": "hi"}')

        # Streamed: the call arrives in fragments and is put back together.
        pieces = []
        out = await mgr.run_inference("local/chat", "text-generation", chat("hi", tools=tools), on_text=pieces.append)
        assert pieces == [] and out["result"].tool_calls[0].function.arguments == '{"q": "hi"}'

        out = await mgr.run_inference("local/chat", "text-generation", chat("hi", json_schema={"type": "object"}))
        assert out["result"].text == '{"echo": "hi", "format": "json_schema"}'
        out = await mgr.run_inference("local/chat", "text-generation", chat("hi", json_schema={}))
        assert out["result"].text == '{"echo": "hi", "format": "json_object"}'

    run(scenario)


def test_rerank_goes_to_its_server():
    serve(**{"local/rerank": {"task": "rerank"}})

    async def scenario():
        mgr = ModelManager(device="cpu")
        out = await mgr.run_inference("local/rerank", "rerank", {"query": "q", "documents": ["a", "ccc", "bb"]})
        # Sorted best first, whatever order the server answered in.
        assert [(r.index, r.score) for r in out["result"].results] == [(1, 3.0), (2, 2.0), (0, 1.0)]
        assert out["result"].tokens == 5

    run(scenario)


def test_server_reports_token_counts():
    serve(**{"local/chat": {}})

    async def scenario():
        mgr = ModelManager(device="cpu")
        out = await mgr.run_inference("local/chat", "text-generation", chat("hi"))
        assert out["result"].prompt_tokens == 3
        assert out["result"].completion_tokens == len(out["result"].text.split(" "))

        pieces = []
        out = await mgr.run_inference("local/chat", "text-generation", chat("again"), on_text=pieces.append)
        # The counts come in the closing chunk the server sends when asked for usage.
        assert out["result"].prompt_tokens == 3 and out["result"].completion_tokens == len(pieces)

    run(scenario)


def test_server_completions_score_tokens_with_logprobs_and_echo():
    serve(**{"local/chat": {}})

    async def scenario():
        mgr = ModelManager(device="cpu")
        # lm-evaluation-harness's shape: token IDs, echo, no new tokens.
        out = await mgr.run_inference("local/chat", "text-generation", {"tokens": [1, 2, 3], "echo": True, "logprobs": 1, "max_length": 0})
        result = out["result"]
        assert result.text == "", "the contract's text is the new text only"
        assert result.prompt_tokens == 3 and result.completion_tokens == 0
        assert result.logprobs.tokens == ["t1", " t2", " t3"]
        assert result.logprobs.token_logprobs[0] is None and result.logprobs.top_logprobs[1]["other"] == -2.0

        out = await mgr.run_inference("local/chat", "text-generation", {"text": "hi there", "echo": True, "logprobs": 0, "max_length": 2})
        assert out["result"].text == " echo: hi"
        assert out["result"].logprobs.tokens == ["hi", " there", " echo:", " hi"]

        out = await mgr.run_inference("local/chat", "text-generation", {"text": "hi there", "logprobs": 0, "max_length": 2})
        assert out["result"].text == " echo: hi" and out["result"].logprobs.tokens == [" echo:", " hi"]

    run(scenario)


# ------------------------------------------------------------------ backends
def test_the_example_file_is_valid():
    import tomllib

    from models.server_specs import parse_layer

    example = Path(__file__).resolve().parent.parent / "model_servers.example.toml"
    layer = parse_layer(tomllib.loads(example.read_text()))
    assert layer.problems == {} and set(layer.providers) == {"ollama", "lmstudio"}
    assert [b.label() for b in layer.models["qwen3.5-4b"].backends] == ["ollama", "lmstudio", "mlx_vlm.server"]


def test_flat_and_listed_backends_parse_alike():
    flat = parse_entry("m", {"task": "text-generation", "url": "http://x:1/", "upstream_model": "u"})
    listed = parse_entry("m", {"task": "text-generation", "backends": [{"url": "http://x:1", "upstream_model": "u"}]})
    assert flat == listed and flat.backends[0].url == "http://x:1"
    for bad, message in (
        ({"task": "text-generation", "url": "http://x", "backends": [{"url": "http://y"}]}, "go on each backend"),
        ({"task": "text-generation", "backends": [{"url": "http://x", "hub": "org/m"}]}, "not url and hub"),
        ({"task": "text-generation", "backends": [{"hub": "org/m", "on_evict": "sleep"}]}, "for cmd backends"),
        ({"task": "text-generation", "backends": [{"url": "http://x", "env": {"A": "1"}}]}, "env is for cmd"),
        ({"task": "text-generation", "backends": []}, "at least 1"),
    ):
        with pytest.raises(ValueError, match=message):
            parse_entry("m", bad)


def test_provider_that_is_down_falls_back_to_the_started_server():
    provider("ollama", model_server._free_port())
    serve(**{"local/vlm": {"backends": [{"provider": "ollama", "upstream_model": "fake"}, {**STARTED, "memory_gb": 1}]}})

    async def scenario():
        mgr = ModelManager(device="cpu", max_memory_gb=10)
        out = await mgr.run_inference("local/vlm", "text-generation", chat("hi"))
        assert out["backend"] == f"{PYTHON}: local/vlm"
        status = mgr.get_cache_status()
        assert status["memory_used_gb"] == pytest.approx(1)
        assert status["model_details"]["local/vlm:text-generation"]["backend"] == out["backend"]

    run(scenario)


def test_provider_that_lists_the_model_wins_and_counts_no_memory():
    port, proc = start_fake()
    try:
        provider("ollama", port)
        serve(**{"local/chat": {"backends": [{"provider": "ollama", "upstream_model": "fake"}, {**STARTED, "memory_gb": 3}]}})

        async def scenario():
            mgr = ModelManager(device="cpu", max_memory_gb=10)
            out = await mgr.run_inference("local/chat", "text-generation", chat("hi"))
            assert out["backend"] == "ollama: fake"
            assert out["result"].text.startswith("echo: hi (model=fake,")
            assert mgr.get_cache_status()["memory_used_gb"] == 0
            assert mgr.cache.get_model("local/chat:text-generation").process is None

        run(scenario)
    finally:
        proc.kill()


def test_server_that_does_not_list_the_model_is_passed_over():
    port, proc = start_fake()
    try:
        serve(**{"local/chat": {"backends": [{"url": f"http://127.0.0.1:{port}", "upstream_model": "other"}, STARTED]}})

        async def scenario():
            probes, chosen = await model_server.probe(model_server._specs["local/chat"])
            assert [p.state for p in probes] == ["unlisted", "ok"] and chosen == 1
            assert probes[0].detail == "answers, but does not list other (it lists fake)"
            out = await ModelManager(device="cpu").run_inference("local/chat", "text-generation", chat("hi"))
            assert out["backend"] == f"{PYTHON}: local/chat"

        run(scenario)
    finally:
        proc.kill()


def test_server_that_fails_to_start_moves_on_to_the_next_backend():
    serve(**{"local/chat": {"backends": [
        {**STARTED, "cmd": [*CMD, "--fail"]}, {**STARTED, "upstream_model": "second"},
    ]}})

    async def scenario():
        mgr = ModelManager(device="cpu")
        out = await mgr.run_inference("local/chat", "text-generation", chat("hi"))
        assert "model=second" in out["result"].text
        # The failed one is skipped for a while, and says why.
        probes, chosen = await model_server.probe(model_server._specs["local/chat"])
        assert chosen == 1 and probes[0].state == "unavailable" and "exited with code 7" in probes[0].detail

    run(scenario)


def test_server_that_goes_away_is_replaced_and_the_request_retried():
    port, proc = start_fake()
    try:
        provider("ollama", port)
        serve(**{"local/chat": {"backends": [{"provider": "ollama", "upstream_model": "fake"}, STARTED]}})

        async def scenario():
            mgr = ModelManager(device="cpu")
            out = await mgr.run_inference("local/chat", "text-generation", chat("hi"))
            assert out["backend"] == "ollama: fake"
            proc.kill()
            proc.wait()
            # Answered all the same: the instance on Ollama is dropped, and the request runs on the next backend.
            out = await mgr.run_inference("local/chat", "text-generation", chat("again"))
            assert out["backend"] == f"{PYTHON}: local/chat" and out["cold_start"] is True
            assert out["result"].text.startswith("echo: again")

        run(scenario)
    finally:
        proc.kill()


def test_hub_backend_runs_the_model_in_process():
    with registered(FakeModel, task="text-generation", ids=["org/small"]):
        serve(**{"local/chat": {"backends": [{"url": f"http://127.0.0.1:{model_server._free_port()}"}, {"hub": "org/small"}]}})

        async def scenario():
            mgr = ModelManager(device="cpu")
            out = await mgr.run_inference("local/chat", "text-generation", chat("hi"))
            assert out["result"].text == "echo: hi" and out["backend"] == "in-process: org/small"
            details = mgr.get_cache_status()["model_details"]["local/chat:text-generation"]
            assert details["kind"] == "in-process"
            inner = mgr.cache.get_model("local/chat:text-generation").choice.inner
            assert inner.model_id == "org/small" and inner.is_loaded
            await mgr.unload_model("local/chat", "text-generation")
            assert not inner.is_loaded and inner.unload_calls == 1

        run(scenario)


def test_no_backend_is_unavailable_with_the_reasons():
    serve(**{"local/chat": {"backends": [
        {"url": f"http://127.0.0.1:{model_server._free_port()}"}, {"cmd": ["no-such-model-server", "--port", "{port}"]},
    ]}})

    async def scenario():
        with pytest.raises(model_server.NoBackend) as caught:
            await ModelManager(device="cpu").run_inference("local/chat", "text-generation", chat("hi"))
        assert "nothing answers at http://127.0.0.1:" in str(caught.value)
        assert "no-such-model-server is not installed" in str(caught.value)

    run(scenario)


def test_extra_fields_reach_the_server_and_the_request_wins():
    serve(**{"local/chat": {"extra": {"top_k": 3, "model": "ignored"}}})

    async def scenario():
        out = await ModelManager(device="cpu").run_inference("local/chat", "text-generation", chat("hi"))
        assert out["result"].text == "echo: hi (model=local/chat, max_tokens=8) top_k=3"

    run(scenario)


def test_ollama_is_asked_to_unload_on_evict():
    port, proc = start_fake()
    try:
        provider("ollama", port, on_evict="unload")
        serve(**{"local/chat": {"backends": [{"provider": "ollama", "upstream_model": "fake"}]}})

        async def scenario():
            mgr = ModelManager(device="cpu")
            await mgr.run_inference("local/chat", "text-generation", chat("hi"))
            await mgr.unload_model("local/chat", "text-generation")

        run(scenario)
        assert httpx.get(f"http://127.0.0.1:{port}/v1/models").json()["unloaded"] == ["fake"]
    finally:
        proc.kill()
