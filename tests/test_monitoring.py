import asyncio
import logging
import time

import pytest

from models.manager import ModelManager
from monitoring import LogBuffer, MonitoringStore, summarize
from monitoring.store import REQUEST, percentile
from tests.fakes import FakeModel, registered


@pytest.fixture
def store(arcade):
    s = MonitoringStore(arcade)
    yield s
    s.close()


def test_percentile_is_nearest_rank():
    values = list(range(1, 101))
    assert percentile(values, 50) == 50
    assert percentile(values, 95) == 95
    assert percentile([7.0], 95) == 7.0
    assert percentile([], 95) is None


def test_requests_and_events_are_written_and_summarized(store):
    for ms in (10, 20, 30, 40):
        store.record_request("gpt2", "text-generation", "ok", ms, cold=ms == 10)
    store.record_request("gpt2", "text-generation", "error", 50, cold=False, error="RuntimeError")
    store.record_request("vega-4b", "decision", "client_error", 5, cold=False, error="ValueError")
    store.record_event("load", "gpt2:text-generation", duration_ms=1234, detail="GPT2Model")
    store.record_event("evict", "gpt2:text-generation", detail="max_models")
    store.flush()

    now = time.time()
    summary = asyncio.run(store.summary(now - 60, now + 1))
    totals = summary["totals"]
    assert (totals["requests"], totals["ok"], totals["errors"], totals["client_errors"]) == (6, 4, 1, 1)
    assert totals["error_rate"] == pytest.approx(1 / 6)

    gpt2 = next(m for m in summary["models"] if m["model_key"] == "gpt2:text-generation")
    assert (gpt2["requests"], gpt2["errors"], gpt2["cold_starts"]) == (5, 1, 1)
    assert gpt2["p50_ms"] == 30 and gpt2["p95_ms"] == 50
    assert sum(p["ok"] + p["error"] + p["client_error"] for p in summary["timeline"]) == 6

    events = asyncio.run(store.events())
    assert [e["kind"] for e in events] == ["evict", "load"]
    assert asyncio.run(store.events(after_id=events[1]["id"]))[0]["kind"] == "evict"
    assert asyncio.run(store.recent_requests(limit=1))[0]["model_id"] == "vega-4b"


def test_reads_filter_by_time_status_and_model(store):
    store.record_request("gpt2", "text-generation", "ok", 10, cold=False)
    store.record_request("vega-4b", "decision", "error", 20, cold=False, error="RuntimeError")
    store.record_event("load", "gpt2:text-generation", duration_ms=5)
    store.flush()
    now = time.time()
    assert [r["model_id"] for r in asyncio.run(store.recent_requests(status="error"))] == ["vega-4b"]
    assert [r["model_id"] for r in asyncio.run(store.recent_requests(model="GPT"))] == ["gpt2"]
    assert [r["model_id"] for r in asyncio.run(store.recent_requests(model="decision"))] == ["vega-4b"]
    assert len(asyncio.run(store.recent_requests(since=now - 60, until=now + 1))) == 2
    assert asyncio.run(store.recent_requests(until=now - 60)) == []
    assert asyncio.run(store.recent_requests(since=now + 60)) == []
    assert [e["kind"] for e in asyncio.run(store.events(since=now - 60))] == ["load"]
    assert asyncio.run(store.events(until=now - 60)) == []


def test_log_buffer_keeps_the_newest_lines():
    buffer = LogBuffer(capacity=3)
    log = logging.getLogger("tests.monitoring.buffer")
    log.propagate = False
    log.setLevel(logging.DEBUG)
    buffer.install("tests.monitoring.buffer", "tests.monitoring.buffer")  # twice: attached once
    assert log.handlers.count(buffer) == 1
    log.debug("one")
    log.info("two %s", "x")
    log.warning("three")
    log.error("four")
    try:
        raise ValueError("boom")
    except ValueError:
        log.exception("five")
    lines = buffer.lines()
    assert [l["message"].splitlines()[0] for l in lines] == ["three", "four", "five"]
    assert [l["id"] for l in lines] == [3, 4, 5] and len(buffer) == 3
    assert "ValueError: boom" in lines[-1]["message"]
    assert [l["message"] for l in buffer.lines(after_id=4)][0].startswith("five")
    assert [l["level"] for l in buffer.lines(level="error")] == ["ERROR", "ERROR"]
    assert [l["message"] for l in buffer.lines(limit=1)][0].startswith("five")
    assert buffer.lines(level="nope") == lines
    log.removeHandler(buffer)


def test_nothing_but_metadata_is_stored(store):
    store.record_request("m", "t", "ok", 1, cold=False)
    store.flush()
    rows = store._rows(store._db(), f"SELECT FROM {REQUEST}")
    assert set(rows[0]) == {"id", "ts", "model_id", "task", "status", "duration_ms", "cold", "error", "trace_id",
                            "prompt_tokens", "completion_tokens"}


def test_token_counts_are_summed(store):
    store.record_request("m", "text-generation", "ok", 10, cold=False, prompt_tokens=100, completion_tokens=20)
    store.record_request("m", "text-generation", "ok", 10, cold=False, prompt_tokens=50, completion_tokens=5)
    store.record_request("c", "text-classification", "ok", 1, cold=False)
    store.flush()
    now = time.time()
    summary = asyncio.run(store.summary(now - 60, now + 1))
    assert summary["totals"]["prompt_tokens"] == 150 and summary["totals"]["completion_tokens"] == 25
    by_key = {m["model_key"]: m for m in summary["models"]}
    assert by_key["m:text-generation"]["completion_tokens"] == 25
    assert by_key["c:text-classification"]["prompt_tokens"] == 0
    # Rows from before the columns existed (plain dicts here) summarize too.
    assert summarize([{"ts": now, "model_id": "m", "task": "t", "status": "ok", "duration_ms": 1, "cold": 0}],
                     now - 1, now + 1)["totals"]["prompt_tokens"] == 0


def test_old_rows_are_pruned(arcade):
    store = MonitoringStore(arcade, retention_days=1)
    db = store._db()
    old = {"id": 1, "ts": time.time() - 2 * 86400, "model_id": "old", "task": "t", "status": "ok", "duration_ms": 1.0,
           "cold": False}
    store._commit(db, lambda: store._insert(db, REQUEST, old))
    store.record_request("new", "t", "ok", 1, cold=False)
    store.flush()
    assert asyncio.run(store.prune()) == 1
    assert [r["model_id"] for r in asyncio.run(store.recent_requests())] == ["new"]
    store.close()


def test_a_closed_store_starts_writing_again(store):
    store.close()
    store.record_request("m", "t", "ok", 1, cold=False)
    store.flush()
    assert len(asyncio.run(store.recent_requests())) == 1


def test_a_full_queue_drops_instead_of_blocking(arcade):
    store = MonitoringStore(arcade, max_queue=1)
    store.close()  # nobody drains the queue now
    store.writer._ensure = lambda: None
    store.record_request("a", "t", "ok", 1, cold=False)
    store.record_request("b", "t", "ok", 1, cold=False)
    assert store.dropped == 1


def test_empty_summary_has_a_full_timeline():
    summary = summarize([], 0, 3600, buckets=60)
    assert summary["totals"]["requests"] == 0 and summary["totals"]["p95_ms"] is None
    assert len(summary["timeline"]) == 60 and summary["bucket_s"] == 60


class RecordingMonitor:
    """The manager's monitor (model events) and its sink (requests), keeping what each is handed."""

    def __init__(self):
        self.requests, self.events = [], []

    def record_event(self, **kw):
        self.events.append(kw)


def test_manager_and_cache_report_requests_and_model_events():
    class Failing(FakeModel):
        async def predict(self, *a, **k):
            raise RuntimeError("boom")

    async def scenario():
        monitor = RecordingMonitor()
        mgr = ModelManager(device="cpu", max_models=1, monitor=monitor, sinks=[monitor.requests.append])
        await mgr.run_inference("a", "t", "hi")           # cold
        await mgr.run_inference("a", "t", "hi")           # warm
        with pytest.raises(ValueError):
            await mgr.run_inference("a", "nope", "hi")    # unsupported task
        with pytest.raises(RuntimeError):
            await mgr.run_inference("b", "fail", "hi")    # loads b, evicting a
        await mgr.unload_model("b", "fail")
        return monitor

    with registered(FakeModel, task="t", fallback=True), registered(Failing, task="fail", fallback=True):
        monitor = asyncio.run(scenario())

    assert [(r.status, r.cold) for r in monitor.requests] == [
        ("ok", True), ("ok", False), ("client_error", False), ("error", True),
    ]
    assert monitor.requests[2].error == "UnsupportedModel"
    assert monitor.requests[3].error_message == "RuntimeError: boom"
    assert [(e["kind"], e["model_key"], e.get("detail")) for e in monitor.events] == [
        ("load", "a:t", "FakeModel"),
        # Room is made before b loads, so a and b are never loaded together.
        ("evict", "a:t", "max_models"),
        ("load", "b:fail", "Failing"),
        ("unload", "b:fail", "manual"),
    ]
