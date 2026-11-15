import asyncio
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from models import memory, registry, servers
from models.manager import ModelManager
from models.servers import ServerModel, parse_specs
from models.servers.entries import Provider, parse_entry
from tests.fakes import FakeModel, registered
from tests.waits import until_async

FAKE = str(Path(__file__).resolve().parent / "fake_model_server.py")
CMD = (sys.executable, FAKE, "--port", "{port}")


@pytest.fixture(autouse=True)
def clean_registry():
    with registry.restored(), servers.restored():
        yield


def serve(**specs):
    """Serve each model ID by its entry (flat, or with backends), started from the fake server by default."""
    def entry(kw):
        if "backends" in kw:
            return {"task": "text-generation", **kw}
        return {"task": "text-generation", "cmd": list(CMD), "startup_timeout": 20, **kw}

    servers.register_specs({model_id: parse_entry(model_id, entry(kw)) for model_id, kw in specs.items()})


def chat(text, **kw):
    return {"text": text, "messages": [{"role": "user", "content": text}], "max_length": 8, **kw}


def start_fake(*args):
    """The fake server as one that runs elsewhere (Ollama, say): its port and process, once it answers."""
    port = servers._free_port()
    proc = subprocess.Popen([sys.executable, FAKE, "--port", str(port), *args])
    for _ in range(100):
        try:
            httpx.get(f"http://127.0.0.1:{port}/v1/models", timeout=1)
            break
        except httpx.HTTPError:
            time.sleep(0.05)
    return port, proc


def provider(name, port, **kw):
    servers.register_specs({}, {name: Provider(url=f"http://127.0.0.1:{port}", **kw)})


STARTED = {"cmd": list(CMD), "startup_timeout": 20}
# The label a cmd backend of the fake server gets: its program's name.
PYTHON = Path(sys.executable).name


def run(scenario):
    async def wrapped():
        try:
            return await scenario()
        finally:
            await servers.stop_all()
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
        await until_async(lambda: mgr.get_cache_status()["cache_size"] == 0, 2.5, "the dead server is still cached")
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
        assert process.returncode is None and "local/sleepy" in servers._sleeping
        out = await mgr.run_inference("local/sleepy", "text-generation", chat("back"))
        assert out["result"].text.startswith("echo: back")
        assert mgr.cache.get_model("local/sleepy:text-generation").process is process
        await servers.stop_all()
        assert process.returncode is not None

    run(scenario)


def test_a_sleeping_server_whose_wake_is_cancelled_is_stopped(monkeypatch):
    serve(**{"local/sleepy": {"on_evict": "sleep"}, "local/other": {}})

    async def scenario():
        mgr = ModelManager(device="cpu", max_models=1)
        await mgr.run_inference("local/sleepy", "text-generation", chat("hi"))
        process = mgr.cache.get_model("local/sleepy:text-generation").process
        await mgr.run_inference("local/other", "text-generation", chat("hi"))
        assert "local/sleepy" in servers._sleeping
        post = httpx.AsyncClient.post

        async def slow_wake(self, url, *args, **kwargs):
            if url == "/wake_up":
                await asyncio.sleep(30)
            return await post(self, url, *args, **kwargs)

        monkeypatch.setattr(httpx.AsyncClient, "post", slow_wake)
        waking = asyncio.create_task(mgr.run_inference("local/sleepy", "text-generation", chat("back")))
        await until_async(lambda: "local/sleepy" not in servers._sleeping, what="the wake to start")
        await asyncio.sleep(0.2)
        waking.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waking
        # Out of _sleeping and woken for nobody: stopped, not left running for the next load to start another beside.
        assert process.returncode is not None and process.pid not in servers._processes
        await servers.stop_all()

    run(scenario)


def test_url_server_is_used_but_never_stopped():
    port, proc = start_fake()
    try:
        servers.register_specs({"remote/chat": parse_entry(
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
            status = mgr.get_cache_status()
            # Answered elsewhere, by the server at its URL.
            assert status["memory_used_gb"] == 0
            assert status["model_details"]["remote/chat:vision-language"]["upstream"] == f"127.0.0.1:{port}"
            await mgr.clear_all_models()

        run(scenario)
        assert proc.poll() is None
    finally:
        proc.kill()


def test_a_url_s_user_and_password_go_with_its_requests_and_are_never_shown(caplog):
    import api.app  # noqa: F401 (the service's logging: httpx's request lines name the URL whole)

    caplog.set_level("INFO")
    port, proc = start_fake("--basic", "svc:s3cret")  # 401 to a request without them
    try:
        servers.register_specs({
            "remote/chat": parse_entry("remote/chat", {"task": "text-generation", "url": f"http://svc:s3cret@127.0.0.1:{port}"}),
            "remote/gone": parse_entry("remote/gone", {"task": "text-generation", "url": "http://svc:s3cret@127.0.0.1:9"}),
        })

        async def scenario():
            mgr = ModelManager(device="cpu")
            out = await mgr.run_inference("remote/chat", "text-generation", chat("hi"))
            # Shown without them: in the answer, the model's details, a probe that failed and its 503.
            assert out["backend"] == f"http://127.0.0.1:{port}: remote/chat"
            details = mgr.get_cache_status()["model_details"]["remote/chat:text-generation"]
            assert details["upstream"] == f"127.0.0.1:{port}" and "s3cret" not in str(details)
            probes, _ = await servers.probe(servers.specs()["remote/gone"])
            assert probes[0].label == "http://127.0.0.1:9" and "s3cret" not in probes[0].detail
            with pytest.raises(Exception) as refused:
                await mgr.run_inference("remote/gone", "text-generation", chat("hi"))
            assert "s3cret" not in str(refused.value)
            await mgr.clear_all_models()

        run(scenario)
        assert "s3cret" not in caplog.text
    finally:
        proc.kill()


def test_configured_servers_are_allowed_and_listed(monkeypatch):
    from api import deps
    from core.config import Config

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
    assert servers._processes == {}


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
    servers.register_specs(specs)
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
        # A batch goes to the server one request at a time.
        out = await mgr.run_batch("local/rerank", "rerank", [{"query": "q", "documents": ["a", "bb"]}] * 2)
        assert [[r.index for r in result.results] for result in out["results"]] == [[1, 0], [1, 0]]

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

    from models.servers.entries import parse_layer

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
        ({"task": "embedding", "url": "http://x", "revision": "v1", "max_length": 8}, "revision, max_length are for hub"),
    ):
        with pytest.raises(ValueError, match=message):
            parse_entry("m", bad)


def test_provider_that_is_down_falls_back_to_the_started_server():
    provider("ollama", servers._free_port())
    serve(**{"local/vlm": {"backends": [{"provider": "ollama", "upstream_model": "fake"}, {**STARTED, "memory_gb": 1}]}})

    async def scenario():
        mgr = ModelManager(device="cpu", max_memory_gb=10)
        out = await mgr.run_inference("local/vlm", "text-generation", chat("hi"))
        assert out["backend"] == f"{PYTHON}: local/vlm"
        status = mgr.get_cache_status()
        assert status["memory_used_gb"] == pytest.approx(1)
        details = status["model_details"]["local/vlm:text-generation"]
        # A server this service started runs on this machine: nothing elsewhere answers it.
        assert details["backend"] == out["backend"] and details["upstream"] is None

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
            # Answered elsewhere, by the provider it names.
            assert mgr.get_cache_status()["model_details"]["local/chat:text-generation"]["upstream"] == "ollama"

        run(scenario)
    finally:
        proc.kill()


def test_another_toolkit_is_a_peer_and_its_backend_the_second_hop():
    port, proc = start_fake("--toolkit", "ollama: qwen3:8b")
    try:
        provider("gpu-box", port)
        serve(**{"local/chat": {"backends": [{"provider": "gpu-box", "upstream_model": "fake"}]}})

        async def scenario():
            probes, _ = await servers.probe(servers.specs()["local/chat"])
            assert probes[0].peer is True
            mgr = ModelManager(device="cpu")
            out = await mgr.run_inference("local/chat", "text-generation", chat("hi"))
            # The request's backend goes on to the one the other toolkit used, and so does the model's.
            assert out["backend"] == "gpu-box: fake → ollama: qwen3:8b"
            details = mgr.get_cache_status()["model_details"]["local/chat:text-generation"]
            assert details["peer"] is True and details["upstream"] == "gpu-box" and details["backend"] == out["backend"]
            # Streamed, its last chunk says so too.
            pieces = []
            out = await mgr.run_inference("local/chat", "text-generation", chat("again"), on_text=pieces.append)
            assert pieces and out["backend"] == "gpu-box: fake → ollama: qwen3:8b"

        run(scenario)
    finally:
        proc.kill()


def test_a_peer_that_runs_the_model_itself_and_a_server_that_is_no_toolkit():
    port, proc = start_fake("--toolkit")
    plain_port, plain = start_fake()
    try:
        serve(**{
            "peer/embed": {"task": "embedding", "backends": [{"url": f"http://127.0.0.1:{port}", "upstream_model": "fake"}]},
            "plain/embed": {"task": "embedding", "backends": [{"url": f"http://127.0.0.1:{plain_port}", "upstream_model": "fake"}]},
        })

        async def scenario():
            mgr = ModelManager(device="cpu")
            out = await mgr.run_inference("peer/embed", "embedding", {"texts": ["a"]})
            assert out["backend"] == f"http://127.0.0.1:{port}: fake → in-process"
            out = await mgr.run_inference("plain/embed", "embedding", {"texts": ["a"]})
            assert out["backend"] == f"http://127.0.0.1:{plain_port}: fake"
            details = mgr.get_cache_status()["model_details"]
            assert [details["peer/embed:embedding"]["peer"], details["plain/embed:embedding"]["peer"]] == [True, False]

        run(scenario)
    finally:
        proc.kill()
        plain.kill()


def test_server_that_does_not_list_the_model_is_passed_over():
    port, proc = start_fake()
    try:
        serve(**{"local/chat": {"backends": [{"url": f"http://127.0.0.1:{port}", "upstream_model": "other"}, STARTED]}})

        async def scenario():
            probes, chosen = await servers.probe(servers.specs()["local/chat"])
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
        probes, chosen = await servers.probe(servers.specs()["local/chat"])
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
            assert mgr.get_cache_status()["model_details"]["local/chat:text-generation"]["upstream"] == "ollama"
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
        serve(**{"local/chat": {"backends": [{"url": f"http://127.0.0.1:{servers._free_port()}"}, {"hub": "org/small"}]}})

        async def scenario():
            mgr = ModelManager(device="cpu")
            out = await mgr.run_inference("local/chat", "text-generation", chat("hi"))
            assert out["result"].text == "echo: hi" and out["backend"] == "in-process: org/small"
            details = mgr.get_cache_status()["model_details"]["local/chat:text-generation"]
            assert details["kind"] == "in-process" and details["upstream"] is None
            inner = mgr.cache.get_model("local/chat:text-generation").choice.inner
            assert inner.model_id == "org/small" and inner.is_loaded
            await mgr.unload_model("local/chat", "text-generation")
            assert not inner.is_loaded and inner.unload_calls == 1

        run(scenario)


class BatchingEmbedder(FakeModel):
    """An in-process embedding model that answers several requests in one call and reads one load setting."""

    load_settings = ("revision",)
    batch_size = 8
    sizes = []

    @classmethod
    def prompts(cls, model_id, revision=None):
        return {"query": f"{model_id}@{revision}: "}

    def predict_batch(self, requests):
        BatchingEmbedder.sizes.append(len(requests))
        return [{"embeddings": [[1.0, 0.0]] * len(r.texts), "tokens": 1} for r in requests]

    async def predict(self, request):
        return self.predict_batch([request])[0]


def test_hub_backend_gets_its_load_settings_batches_and_has_its_models_prompts():
    BatchingEmbedder.sizes = []
    with registered(BatchingEmbedder, task="embedding", ids=["org/emb"]):
        servers.register_specs({"my-emb": parse_entry("my-emb", {
            "task": "embedding", "hub": "org/emb", "revision": "v2",
        })})

        async def scenario():
            mgr = ModelManager(device="cpu", batch_wait_s=0.02)
            await mgr.run_inference("my-emb", "embedding", {"texts": ["warm"]})
            BatchingEmbedder.sizes = []
            outs = await asyncio.gather(*[mgr.run_inference("my-emb", "embedding", {"texts": [f"t{i}"]})
                                          for i in range(4)])
            assert all(o["result"].embeddings == [[1.0, 0.0]] for o in outs)
            assert sum(BatchingEmbedder.sizes) == 4 and len(BatchingEmbedder.sizes) < 4
            inner = mgr.cache.get_model("my-emb:embedding").choice.inner
            assert inner.options["revision"] == "v2"

        run(scenario)
        assert registry.prompts("my-emb") == {"query": "org/emb@v2: "}


def test_hub_backend_with_a_setting_its_model_does_not_read_is_refused():
    with registered(BatchingEmbedder, task="embedding", ids=["org/emb"]):
        servers.register_specs({"my-emb": parse_entry("my-emb", {
            "task": "embedding", "hub": "org/emb", "max_length": 64,
        })})

        async def scenario():
            with pytest.raises(servers.NoBackend, match="BatchingEmbedder, which runs org/emb, does not read "
                                                        "max_length"):
                await ModelManager(device="cpu").run_inference("my-emb", "embedding", {"texts": ["x"]})

        run(scenario)


def test_no_backend_is_unavailable_with_the_reasons():
    serve(**{"local/chat": {"backends": [
        {"url": f"http://127.0.0.1:{servers._free_port()}"}, {"cmd": ["no-such-model-server", "--port", "{port}"]},
    ]}})

    async def scenario():
        with pytest.raises(servers.NoBackend) as caught:
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
