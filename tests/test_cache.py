import asyncio

import pytest

from models import memory
from models.cache import CapacityError, ModelCache
from tests.fakes import FakeModel

GB = memory.GB


@pytest.fixture(autouse=True)
def fresh_fakes(monkeypatch):
    FakeModel.instances.clear()
    FakeModel.log.clear()
    FakeModel.load_delay = 0.0
    # Free memory never limits, unless a test says so.
    monkeypatch.setattr(memory, "free_bytes", lambda device: None)


def run(coro):
    return asyncio.run(coro)


def maker(model_id, size_gb=0.0):
    def create():
        m = FakeModel(model_id, "t")
        m.size_bytes = int(size_gb * GB)
        return m
    return create


async def use(cache, model_id, size_gb=0.0):
    """Check a model out and straight back in."""
    entry, _ = await cache.checkout(model_id, maker(model_id, size_gb))
    await cache.checkin(entry)
    return entry.model


def test_count_limit_evicts_lru_before_loading():
    async def scenario():
        cache = ModelCache(max_models=3, max_memory_gb=0)
        for name in ("m0", "m1", "m2"):
            await use(cache, name)
        await use(cache, "m0")  # touch m0 so m1 becomes LRU
        await use(cache, "m3")

        assert list(cache.entries) == ["m2", "m0", "m3"]
        m1 = next(m for m in FakeModel.instances if m.model_id == "m1")
        assert m1.unload_calls == 1 and not m1.is_loaded
        # m1 was gone before m3 started loading: never both in memory.
        assert FakeModel.log[-2:] == [("unload", "m1"), ("load", "m3")]

    run(scenario())


def test_memory_budget_counts_model_sizes():
    async def scenario():
        cache = ModelCache(max_models=10, max_memory_gb=10)
        await use(cache, "a", 4)
        await use(cache, "b", 4)
        await use(cache, "c", 4)  # 12 GB would not fit: a goes first
        assert list(cache.entries) == ["b", "c"]
        assert cache.get_cache_stats()["memory_used_gb"] == pytest.approx(8)
        await use(cache, "small", 1)  # fits next to b and c
        assert list(cache.entries) == ["b", "c", "small"]

    run(scenario())


def test_model_larger_than_the_budget_still_loads_alone():
    async def scenario():
        cache = ModelCache(max_models=3, max_memory_gb=2)
        await use(cache, "a", 1)
        await asyncio.wait_for(use(cache, "huge", 5), timeout=2)
        assert list(cache.entries) == ["huge"]

    run(scenario())


def test_low_free_memory_evicts_too(monkeypatch):
    monkeypatch.setattr(memory, "free_bytes", lambda device: 3 * GB)

    async def scenario():
        cache = ModelCache(max_models=10, max_memory_gb=0)
        await use(cache, "a", 1)
        await use(cache, "b", 4)  # more than is free: a is evicted to try
        assert list(cache.entries) == ["b"]

    run(scenario())


def test_busy_models_are_skipped_and_full_cache_waits_then_fails():
    async def scenario():
        cache = ModelCache(max_models=2, max_memory_gb=0, wait_timeout=0.2)
        busy, _ = await cache.checkout("busy", maker("busy"))
        await use(cache, "idle")
        await use(cache, "new")  # evicts idle, never busy
        assert list(cache.entries) == ["busy", "new"]

        held, _ = await cache.checkout("new", maker("new"))
        with pytest.raises(CapacityError):
            await use(cache, "third")
        assert list(cache.entries) == ["busy", "new"]

        # Once one is checked back in, a waiting load takes its place.
        waiting = asyncio.create_task(use(cache, "third"))
        await asyncio.sleep(0.05)
        await cache.checkin(held)
        await asyncio.wait_for(waiting, timeout=1)
        assert list(cache.entries) == ["busy", "third"]
        await cache.checkin(busy)

    run(scenario())


def test_one_load_at_a_time():
    async def scenario():
        FakeModel.load_delay = 0.05
        cache = ModelCache(max_models=5, max_memory_gb=0)
        await asyncio.gather(use(cache, "a"), use(cache, "b"))
        # Loads did not interleave: each finished before the next began.
        assert [kind for kind, _ in FakeModel.log] == ["load", "load"]
        assert len(cache.entries) == 2

    run(scenario())


def test_remove_waits_for_requests_and_clear_unloads_all():
    async def scenario():
        cache = ModelCache()
        entry, _ = await cache.checkout("a", maker("a"))
        removal = asyncio.create_task(cache.remove_model("a"))
        await asyncio.sleep(0.05)
        assert not removal.done() and entry.model.is_loaded
        await cache.checkin(entry)
        await asyncio.wait_for(removal, timeout=1)
        assert entry.model.unload_calls == 1 and "a" not in cache.entries
        with pytest.raises(KeyError):
            await cache.remove_model("a")

        b = await use(cache, "b")
        await cache.clear_cache()
        assert b.unload_calls == 1
        assert cache.get_cache_stats()["cache_size"] == 0

    run(scenario())


def test_idle_models_are_unloaded():
    async def scenario():
        cache = ModelCache()
        old = await use(cache, "old")
        cache.entries["old"].info.last_used -= 100
        held, _ = await cache.checkout("held", maker("held"))
        cache.entries["held"].info.last_used -= 100
        await use(cache, "recent")
        await cache.unload_idle(50)
        assert old.unload_calls == 1
        assert list(cache.entries) == ["held", "recent"]
        await cache.checkin(held)

    run(scenario())


def test_stats_report_sizes_and_device_memory():
    async def scenario():
        cache = ModelCache()
        await use(cache, "a", 0.5)
        stats = cache.get_cache_stats()
        assert stats["memory_backend"] == "rss"
        assert stats["model_details"]["a"]["memory_usage"] == pytest.approx(0.5)
        assert stats["model_details"]["a"]["kind"] == "in-process"
        assert stats["device_memory_gb"] >= 0

    run(scenario())


def test_lower_limits_evict_idle_models_now():
    async def scenario():
        cache = ModelCache(max_models=5, max_memory_gb=10)
        for name in ("a", "b", "c"):
            await use(cache, name, 2)
        held, _ = await cache.checkout("a", maker("a"))  # a is busy, and now most recent
        assert await cache.set_limits(max_memory_gb=3) == ["b", "c"]
        # a stays while busy, though it alone keeps the cache within 3 GB.
        assert list(cache.entries) == ["a"] and not cache.over_limits()
        assert await cache.set_limits(max_models=1, max_memory_gb=1) == []
        assert cache.over_limits()  # busy a is over 1 GB, and stays until checked in
        await cache.checkin(held)
        assert await cache.set_limits(max_memory_gb=1) == ["a"]

    run(scenario())


def test_plan_says_what_a_load_would_do_without_doing_it(monkeypatch):
    async def scenario():
        cache = ModelCache(max_models=3, max_memory_gb=10)
        assert cache.plan("a", 4 * GB) == {"outcome": "fits", "evicts": [], "reason": None}
        await use(cache, "a", 4)
        busy, _ = await cache.checkout("b", maker("b", 4))
        await use(cache, "c", 1)
        assert cache.plan("a", 4 * GB)["outcome"] == "loaded"
        # Full on count: the least recently used idle model goes, never the busy one.
        assert cache.plan("d", 1 * GB) == {"outcome": "evicts", "evicts": ["a"], "reason": None}
        assert cache.plan("d", 6 * GB) == {"outcome": "evicts", "evicts": ["a", "c"], "reason": None}
        assert cache.plan("d", 7 * GB) == {"outcome": "waits", "evicts": ["a", "c"], "reason": "max_memory_gb"}
        await cache.checkin(busy)
        assert cache.plan("d", 11 * GB) == {"outcome": "too_big", "evicts": ["a", "b", "c"], "reason": "max_memory_gb"}
        monkeypatch.setattr(memory, "free_bytes", lambda device: 2 * GB)
        assert cache.plan("d", 3 * GB)["evicts"] == ["a"], "evicting a frees its memory too"
        assert list(cache.entries) == ["a", "b", "c"], "planning unloads nothing"

    run(scenario())


def test_free_memory_eviction_stops_once_it_frees_nothing(monkeypatch):
    # Free memory is system-wide: when evicting a model does not raise it,
    # evicting the rest would not either, so the load goes ahead.
    monkeypatch.setattr(memory, "free_bytes", lambda device: 3 * GB)

    async def scenario():
        cache = ModelCache(max_models=10, max_memory_gb=0)
        await use(cache, "a", 1)
        await use(cache, "b", 1)
        await use(cache, "c", 4)
        assert list(cache.entries) == ["b", "c"]

    run(scenario())


def test_free_memory_eviction_continues_while_it_helps(monkeypatch):
    async def scenario():
        cache = ModelCache(max_models=10, max_memory_gb=0)
        monkeypatch.setattr(memory, "free_bytes", lambda device: 6 * GB - cache.used())
        await use(cache, "a", 1)
        await use(cache, "b", 1)
        await use(cache, "c", 6)  # needs both evicted; each eviction frees its size
        assert list(cache.entries) == ["c"]

    run(scenario())


def test_clear_cache_survives_a_concurrent_eviction():
    async def scenario():
        cache = ModelCache(max_models=2, max_memory_gb=0)
        a, _ = await cache.checkout("a", maker("a"))
        await use(cache, "b")
        clearing = asyncio.create_task(cache.clear_cache())
        await asyncio.sleep(0.02)  # waits for a's request to finish
        await use(cache, "c")  # evicts the idle b meanwhile
        await cache.checkin(a)
        await asyncio.wait_for(clearing, timeout=1)  # b's KeyError does not abort it
        assert list(cache.entries) == ["c"]

    run(scenario())


# ------------------------------------------------------------------ pinning
def test_pinned_models_are_never_evicted():
    async def scenario():
        cache = ModelCache(max_models=2, max_memory_gb=0)
        a = await use(cache, "a")
        cache.set_pinned("a", True)
        await use(cache, "b")
        await use(cache, "c")  # evicts b, the only candidate
        assert list(cache.entries) == ["a", "c"]
        assert cache.plan("d", 0)["evicts"] == ["c"]
        assert cache.get_cache_stats()["model_details"]["a"]["pinned"] is True
        await cache.unload_idle(0)  # the idle timeout skips it too
        assert list(cache.entries) == ["a"] and a.unload_calls == 0

        # Unpinned, it is an ordinary entry again; an explicit unload always works.
        cache.set_pinned("a", False)
        await cache.remove_model("a")
        assert a.unload_calls == 1
        with pytest.raises(KeyError):
            cache.set_pinned("a", True)

    run(scenario())


def test_only_pinned_models_left_means_the_load_goes_ahead():
    async def scenario():
        cache = ModelCache(max_models=1, max_memory_gb=0, wait_timeout=0.2)
        await use(cache, "a")
        cache.set_pinned("a", True)
        # Nothing to evict and nothing to wait for: load beyond the limit rather than fail.
        await use(cache, "b")
        assert list(cache.entries) == ["a", "b"]

    run(scenario())


# -------------------------------------------------------------- queue stats
def test_queue_and_expected_wait_are_reported():
    async def scenario():
        cache = ModelCache(max_models=2, max_memory_gb=0)
        entry, _ = await cache.checkout("a", maker("a"))
        details = lambda: cache.get_cache_stats()["model_details"]["a"]
        assert details()["estimated_wait_ms"] is None  # nothing has finished yet
        assert details()["queued"] == 0 and details()["max_concurrency"] == 1

        entry.note(100)
        entry.note(300)  # recent requests weigh more
        assert 100 < entry.avg_ms < 300
        # One request running at concurrency 1: the next one waits a whole request.
        assert details()["estimated_wait_ms"] == pytest.approx(entry.avg_ms)
        second, _ = await cache.checkout("a", maker("a"))
        assert details()["queued"] == 1
        assert details()["estimated_wait_ms"] == pytest.approx(2 * entry.avg_ms)
        await cache.checkin(second)
        await cache.checkin(entry)
        assert details()["in_use"] == 0 and details()["estimated_wait_ms"] == 0

    run(scenario())


def test_capacity_error_suggests_when_to_retry():
    async def scenario():
        cache = ModelCache(max_models=1, max_memory_gb=0, wait_timeout=0.05)
        busy, _ = await cache.checkout("busy", maker("busy"))
        with pytest.raises(CapacityError) as caught:
            await use(cache, "other")
        assert caught.value.retry_after == 5.0  # no request has finished: the default
        busy.note(2500)
        with pytest.raises(CapacityError) as caught:
            await use(cache, "other")
        assert caught.value.retry_after == pytest.approx(2.5)
        assert cache.get_cache_stats()["loads_pending"] == 0

    run(scenario())
