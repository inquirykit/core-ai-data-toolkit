import pytest
from fastapi.testclient import TestClient

from api import app as app_module
from config import Config
from models.generic.transformers_model import TransformersModel
from monitoring import Metrics
from monitoring.metrics import BUCKETS
from tests.fakes import FakeModel, swapped


class RecordingStore:
    dropped = 0

    def __init__(self):
        self.requests, self.events = [], []

    def record_request(self, **kwargs):
        self.requests.append(kwargs)

    def record_event(self, **kwargs):
        self.events.append(kwargs)


def lines(text):
    return [line for line in text.splitlines() if not line.startswith("#")]


def test_metrics_count_requests_tokens_latency_and_events():
    store = RecordingStore()
    metrics = Metrics(store)
    metrics.record_request("m", "text-generation", "ok", 120.0, cold=True, prompt_tokens=10, completion_tokens=5)
    metrics.record_request("m", "text-generation", "ok", 30.0, cold=False, prompt_tokens=2, completion_tokens=1)
    metrics.record_request("m", "text-generation", "error", 1500.0, cold=False, error="RuntimeError")
    metrics.record_request("c", "text-classification", "ok", 4.0, cold=False)
    metrics.record_event("load", "m:text-generation", duration_ms=900)

    out = lines(metrics.render({"cache_size": 1, "max_models": 3, "memory_used_gb": 1, "max_memory_gb": 8,
                                "loads_pending": 0, "device": "cpu", "model_details": {}}, {"uptime_s": 5}, "1.0"))
    assert 'ai_toolkit_requests_total{model="m",task="text-generation",status="ok"} 2' in out
    assert 'ai_toolkit_requests_total{model="m",task="text-generation",status="error"} 1' in out
    assert 'ai_toolkit_cold_starts_total{model="m",task="text-generation"} 1' in out
    assert 'ai_toolkit_tokens_total{model="m",task="text-generation",kind="prompt"} 12' in out
    assert 'ai_toolkit_tokens_total{model="m",task="text-generation",kind="completion"} 6' in out
    assert 'ai_toolkit_model_events_total{kind="load"} 1' in out
    # 0.12 s and 0.03 s fall in the 0.25 bucket; 1.5 s only from 2.5 on; every request in +Inf.
    assert 'ai_toolkit_request_duration_seconds_bucket{task="text-generation",le="0.25"} 2' in out
    assert 'ai_toolkit_request_duration_seconds_bucket{task="text-generation",le="2.5"} 3' in out
    assert 'ai_toolkit_request_duration_seconds_bucket{task="text-generation",le="+Inf"} 3' in out
    assert 'ai_toolkit_request_duration_seconds_count{task="text-generation"} 3' in out
    assert 'ai_toolkit_request_duration_seconds_sum{task="text-generation"} 1.65' in out
    assert len([l for l in out if l.startswith("ai_toolkit_request_duration_seconds_bucket{task=\"text-classification\"")]) == len(BUCKETS) + 1
    assert 'ai_toolkit_info{version="1.0",device="cpu"} 1' in out
    assert 'ai_toolkit_memory_budget_bytes 8589934592' in out
    assert 'ai_toolkit_process_uptime_seconds 5' in out
    # Everything is handed on to the store, token counts included.
    assert store.requests[0]["prompt_tokens"] == 10 and store.requests[0]["error"] is None
    assert store.requests[2]["error"] == "RuntimeError"
    assert store.events == [{"kind": "load", "model_key": "m:text-generation", "duration_ms": 900, "detail": None}]


def test_labels_are_escaped_and_gauges_follow_the_cache():
    metrics = Metrics()
    metrics.record_request('we"ird\\', "t", "ok", 1.0, cold=False)
    cache = {
        "cache_size": 2, "max_models": 3, "memory_used_gb": 0.5, "max_memory_gb": 0, "loads_pending": 1,
        "device_memory_gb": 16, "free_memory_gb": 4,
        "model_details": {"a:t": {"model_id": "a", "task": "t", "memory_usage": 0.5, "in_use": 1, "queued": 2, "pinned": True}},
    }
    system = {"uptime_s": 1, "rss_gb": 1, "cpu_percent": 3.5, "host_memory": {"total_gb": 32, "available_gb": 8},
              "gpus": [{"index": 0, "name": "A100", "allocated_gb": 1, "reserved_gb": 2, "total_gb": 40}]}
    out = lines(metrics.render(cache, system))
    assert 'ai_toolkit_requests_total{model="we\\"ird\\\\",task="t",status="ok"} 1' in out
    assert 'ai_toolkit_models_loaded 2' in out and 'ai_toolkit_loads_pending 1' in out
    assert 'ai_toolkit_model_memory_bytes{model="a",task="t"} 536870912' in out
    assert 'ai_toolkit_model_queued{model="a",task="t"} 2' in out
    assert 'ai_toolkit_model_pinned{model="a",task="t"} 1' in out
    assert 'ai_toolkit_device_memory_free_bytes 4294967296' in out
    assert 'ai_toolkit_host_memory_bytes{state="available"} 8589934592' in out
    assert 'ai_toolkit_gpu_memory_bytes{index="0",name="A100",state="reserved"} 2147483648' in out
    assert 'ai_toolkit_process_cpu_percent 3.5' in out


@pytest.fixture(scope="module")
def client():
    with swapped(TransformersModel, FakeModel), TestClient(app_module.app) as c:
        yield c


def test_metrics_endpoint(client, monkeypatch):
    r = client.post("/inference", json={"model_id": "metrics/m", "task": "text-generation", "input": {"text": "hi"}})
    assert r.status_code == 200, r.text
    r = client.get("/metrics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain; version=0.0.4")
    assert 'ai_toolkit_requests_total{model="metrics/m",task="text-generation",status="ok"} 1' in r.text
    assert 'ai_toolkit_model_in_use{model="metrics/m",task="text-generation"} 0' in r.text
    assert "# TYPE ai_toolkit_request_duration_seconds histogram" in r.text

    monkeypatch.setattr(Config, "API_KEY", "secret")
    assert client.get("/metrics").status_code == 401
    assert client.get("/metrics", headers={"Authorization": "Bearer secret"}).status_code == 200
