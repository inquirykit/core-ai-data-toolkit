import asyncio
import json
import logging
import time
from collections import Counter

import pytest

from models.manager import ModelManager
from monitoring import LogBuffer, MonitoringStore, summarize
from monitoring.store import ACCURACY, HOUR, HOURS_KEPT_DAYS, REQUEST, latency_bucket, of_request, percentile
from db.engine import MIGRATIONS
from tests.fakes import FakeModel, registered


@pytest.fixture
def store(arcade):
    s = MonitoringStore(arcade)
    yield s
    s.close()


def histogram(*values):
    return Counter(latency_bucket(v) for v in values)


def test_percentile_is_nearest_rank_within_the_accuracy():
    values = histogram(*range(1, 101))
    assert percentile(values, 50) == pytest.approx(50, rel=ACCURACY)
    assert percentile(values, 95) == pytest.approx(95, rel=ACCURACY)
    assert percentile(histogram(7.0), 95) == pytest.approx(7, rel=ACCURACY)
    assert percentile(histogram(0.0), 50) == pytest.approx(0, abs=0.01)
    assert percentile({}, 95) is None


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
    assert gpt2["p50_ms"] == pytest.approx(30, rel=ACCURACY) and gpt2["p95_ms"] == pytest.approx(50, rel=ACCURACY)
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
    # A filter's text is matched as written: ArcadeDB's wildcards in it match only themselves.
    assert asyncio.run(store.recent_requests(model="gp?2")) == [] and asyncio.run(store.recent_requests(model="g%2")) == []
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
    old = {"ts": now, "model_id": "m", "task": "t", "status": "ok", "duration_ms": 1, "cold": 0}
    assert summarize([of_request(old)], now - 1, now + 1)["totals"]["prompt_tokens"] == 0


def hours(store):
    return {(r["model_id"], r["ok"], r["error"]): r for r in store._select(f"SELECT FROM {HOUR}")}


def test_each_batch_adds_up_in_the_hourly_summary(store):
    store.record_request("m", "t", "ok", 10, cold=True, prompt_tokens=3)
    store.record_request("c", "t", "ok", 1, cold=False)
    store.flush()
    store.record_request("m", "t", "error", 30, cold=False, prompt_tokens=4)
    store.flush()
    rows = hours(store)
    assert set(rows) == {("m", 1, 1), ("c", 1, 0)}
    m = rows["m", 1, 1]
    assert (m["cold_starts"], m["prompt_tokens"], m["hour"] % 3600) == (1, 7, 0)
    assert sum(json.loads(m["latency"]).values()) == 2


def test_a_long_range_is_summed_up_from_whole_hours(store):
    for ms in (10, 20, 30):
        store.record_request("m", "t", "ok", ms, cold=False)
    store.record_request("m", "t", "client_error", 5, cold=False)
    store.flush()
    now = time.time()
    summary = asyncio.run(store.summary(now - 86400, now + 1))
    assert summary["bucket_s"] == 3600 and summary["since"] % 3600 == 0 and len(summary["timeline"]) <= 60
    assert (summary["totals"]["requests"], summary["totals"]["client_errors"]) == (4, 1)
    assert summary["models"][0]["p50_ms"] == pytest.approx(10, rel=ACCURACY)
    assert sum(p["ok"] for p in summary["timeline"]) == 3
    week = asyncio.run(store.summary(now - 7 * 86400, now, buckets=60))
    assert week["bucket_s"] == 3 * 3600 and week["totals"]["requests"] == 4


def test_a_range_older_than_the_request_history_reads_its_hours(store):
    db = store._db()
    hour = (time.time() - 30 * 86400) // 3600 * 3600
    store._commit(db, lambda: store._insert(db, HOUR, {
        "hour": int(hour), "model_id": "m", "task": "t", "ok": 2, "client_error": 0, "error": 1, "cold_starts": 0,
        "prompt_tokens": 0, "completion_tokens": 0, "last_request": hour + 60, "latency": json.dumps({latency_bucket(40): 3}),
    }))
    summary = asyncio.run(store.summary(hour + 600, hour + 1200))
    assert (summary["totals"]["requests"], summary["totals"]["errors"]) == (3, 1)
    assert summary["totals"]["p95_ms"] == pytest.approx(40, rel=ACCURACY)
    assert asyncio.run(store.summary(hour - 7200, hour - 3600))["totals"]["requests"] == 0


def test_requests_from_before_the_hourly_summary_are_summed_up_once(arcade):
    first = MonitoringStore(arcade)
    db = first._db()
    now = time.time()
    rows = [{"id": i, "ts": now - i * 86400, "model_id": "m", "task": "t", "status": "ok", "duration_ms": 1.0,
             "cold": False} for i in range(3)]
    first._commit(db, lambda: [first._insert(db, REQUEST, row) for row in rows])
    first._command(f"DELETE FROM {MIGRATIONS} WHERE id = 'monitoring-hourly-summary'")
    again = MonitoringStore(arcade)
    assert sorted(r["ok"] for r in again._select(f"SELECT FROM {HOUR}")) == [1, 1, 1]
    assert asyncio.run(again.summary(now - 3 * 86400, now + 1))["totals"]["requests"] == 3
    first.close()
    again.close()


def test_hours_are_kept_longer_than_requests(store):
    db = store._db()
    now = time.time()
    for days in (30, HOURS_KEPT_DAYS + 1):
        hour = int((now - days * 86400) // 3600 * 3600)
        store._commit(db, lambda: store._insert(db, HOUR, {"hour": hour, "model_id": f"{days}d", "task": "t", "ok": 1,
                                                           "latency": "{}"}))
    asyncio.run(store.prune())
    assert [r["model_id"] for r in store._select(f"SELECT FROM {HOUR}")] == ["30d"]


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
