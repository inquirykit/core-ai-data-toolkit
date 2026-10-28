"""
Prometheus metrics for GET /metrics.

Counters and a latency histogram are kept in this process since it started,
the way Prometheus expects (it computes rates and windows itself); gauges are
read live from the model cache and the host when scraped. The same object is
the manager's monitor: each request and model event is counted here and then
handed to the monitoring store (when monitoring is on) for the dashboard.
"""

import math
import threading
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Optional, Tuple

if TYPE_CHECKING:
    from models.events import RequestEvent

GB = 1024 ** 3
# Request latency buckets, in seconds: cold starts take tens of seconds, a classification a few milliseconds.
BUCKETS = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0)
PREFIX = "ai_toolkit_"


def _escape(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _labels(labels: Dict[str, Any]) -> str:
    if not labels:
        return ""
    return "{" + ",".join(f'{k}="{_escape(v)}"' for k, v in labels.items()) + "}"


def _number(value: float) -> str:
    if isinstance(value, float) and math.isinf(value):
        return "+Inf"
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e15:
        return str(int(value))
    return repr(value) if isinstance(value, float) else str(value)


class Metrics:
    """Counts requests, latency, tokens and model events, and forwards each record to ``store``."""

    def __init__(self, store: Any = None):
        self.store = store
        self._lock = threading.Lock()
        self.requests: Dict[Tuple[str, str, str], int] = {}
        self.cold_starts: Dict[Tuple[str, str], int] = {}
        self.tokens: Dict[Tuple[str, str, str], int] = {}
        self.events: Dict[str, int] = {}
        # Per task: counts per bucket (the last one is +Inf), the sum and the count.
        self.latency: Dict[str, List[int]] = {}
        self.latency_sum: Dict[str, float] = {}
        self.latency_count: Dict[str, int] = {}

    @property
    def dropped(self) -> int:
        return self.store.dropped if self.store is not None else 0

    def on_request(self, event: "RequestEvent") -> None:
        """The model manager's sink (models/events.py): the request counted, and kept by the monitoring store."""
        self.record_request(
            model_id=event.model_id, task=event.task, status=event.status, duration_ms=event.duration_ms,
            cold=event.cold, error=event.error, trace_id=event.calls[0].trace_id if event.calls else None,
            prompt_tokens=event.prompt_tokens, completion_tokens=event.completion_tokens,
        )

    def record_request(
        self, model_id: str, task: str, status: str, duration_ms: float, cold: bool, error: Optional[str] = None,
        trace_id: Optional[str] = None, prompt_tokens: Optional[int] = None, completion_tokens: Optional[int] = None,
    ) -> None:
        seconds = duration_ms / 1000
        with self._lock:
            self.requests[(model_id, task, status)] = self.requests.get((model_id, task, status), 0) + 1
            if cold:
                self.cold_starts[(model_id, task)] = self.cold_starts.get((model_id, task), 0) + 1
            for kind, count in (("prompt", prompt_tokens), ("completion", completion_tokens)):
                if count:
                    self.tokens[(model_id, task, kind)] = self.tokens.get((model_id, task, kind), 0) + count
            counts = self.latency.setdefault(task, [0] * (len(BUCKETS) + 1))
            for i, bound in enumerate(BUCKETS):
                if seconds <= bound:
                    counts[i] += 1
            counts[-1] += 1
            self.latency_sum[task] = self.latency_sum.get(task, 0.0) + seconds
            self.latency_count[task] = self.latency_count.get(task, 0) + 1
        if self.store is not None:
            self.store.record_request(
                model_id=model_id, task=task, status=status, duration_ms=duration_ms, cold=cold, error=error,
                trace_id=trace_id, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
            )

    def record_event(
        self, kind: str, model_key: str, duration_ms: Optional[float] = None, detail: Optional[str] = None
    ) -> None:
        with self._lock:
            self.events[kind] = self.events.get(kind, 0) + 1
        if self.store is not None:
            self.store.record_event(kind=kind, model_key=model_key, duration_ms=duration_ms, detail=detail)

    # -------------------------------------------------------------- rendering
    def render(self, cache: Dict[str, Any], system: Dict[str, Any], version: str = "") -> str:
        """The metrics in Prometheus' text exposition format (version 0.0.4)."""
        lines: List[str] = []

        def metric(name: str, kind: str, help_text: str, samples: Iterable[Tuple[Dict[str, Any], float]], suffix: str = "") -> None:
            lines.append(f"# HELP {PREFIX}{name} {help_text}")
            lines.append(f"# TYPE {PREFIX}{name} {kind}")
            for labels, value in samples:
                lines.append(f"{PREFIX}{name}{suffix}{_labels(labels)} {_number(value)}")

        with self._lock:
            requests = dict(self.requests)
            cold_starts = dict(self.cold_starts)
            tokens = dict(self.tokens)
            events = dict(self.events)
            latency = {task: list(counts) for task, counts in self.latency.items()}
            latency_sum = dict(self.latency_sum)
            latency_count = dict(self.latency_count)

        metric("info", "gauge", "The service: its version and device.",
               [({"version": version or "unknown", "device": cache.get("device") or ""}, 1)])
        metric("requests_total", "counter", "Inference requests since the process started, by outcome.",
               [({"model": m, "task": t, "status": s}, n) for (m, t, s), n in sorted(requests.items())])
        metric("cold_starts_total", "counter", "Requests that had to load their model first.",
               [({"model": m, "task": t}, n) for (m, t), n in sorted(cold_starts.items())])
        metric("tokens_total", "counter", "Prompt and completion tokens of generation requests, when the model counts them.",
               [({"model": m, "task": t, "kind": k}, n) for (m, t, k), n in sorted(tokens.items())])
        metric("model_events_total", "counter", "Model loads, failed loads, unloads, evictions and pins.",
               [({"kind": k}, n) for k, n in sorted(events.items())])

        lines.append(f"# HELP {PREFIX}request_duration_seconds Request time, model load included, by task.")
        lines.append(f"# TYPE {PREFIX}request_duration_seconds histogram")
        for task in sorted(latency):
            counts = latency[task]
            for i, bound in enumerate((*BUCKETS, math.inf)):
                lines.append(f'{PREFIX}request_duration_seconds_bucket{{task="{_escape(task)}",le="{_number(float(bound))}"}} {counts[i]}')
            lines.append(f'{PREFIX}request_duration_seconds_sum{{task="{_escape(task)}"}} {_number(latency_sum[task])}')
            lines.append(f'{PREFIX}request_duration_seconds_count{{task="{_escape(task)}"}} {latency_count[task]}')

        details = cache.get("model_details") or {}
        per_model = lambda field: [  # noqa: E731
            ({"model": d["model_id"], "task": d["task"]}, field(d)) for d in details.values()
        ]
        metric("models_loaded", "gauge", "Models in the cache now.", [({}, cache.get("cache_size", 0))])
        metric("models_max", "gauge", "MAX_MODELS.", [({}, cache.get("max_models", 0))])
        metric("loads_pending", "gauge", "Model loads under way or waiting for room.", [({}, cache.get("loads_pending", 0))])
        metric("memory_used_bytes", "gauge", "Budgeted size of the loaded models.", [({}, (cache.get("memory_used_gb") or 0) * GB)])
        metric("memory_budget_bytes", "gauge", "MAX_MEMORY_GB (0: no budget).", [({}, (cache.get("max_memory_gb") or 0) * GB)])
        metric("model_memory_bytes", "gauge", "Budgeted size of each loaded model.",
               per_model(lambda d: (d.get("memory_usage") or 0) * GB))
        metric("model_in_use", "gauge", "Requests each loaded model is running.", per_model(lambda d: d.get("in_use") or 0))
        metric("model_queued", "gauge", "Requests waiting for a slot on each loaded model.", per_model(lambda d: d.get("queued") or 0))
        metric("model_pinned", "gauge", "Whether each loaded model is pinned (never evicted).", per_model(lambda d: int(bool(d.get("pinned")))))
        for key, name, help_text in (
            ("device_memory_gb", "device_memory_bytes", "Memory of the inference device."),
            ("free_memory_gb", "device_memory_free_bytes", "Memory free on the inference device, other processes included."),
        ):
            if cache.get(key) is not None:
                metric(name, "gauge", help_text, [({}, cache[key] * GB)])

        metric("process_uptime_seconds", "gauge", "Seconds since the process started.", [({}, system.get("uptime_s", 0))])
        if "rss_gb" in system:
            metric("process_resident_memory_bytes", "gauge", "Resident memory of the process.", [({}, system["rss_gb"] * GB)])
            metric("process_cpu_percent", "gauge", "CPU use of the process since the last scrape.", [({}, system.get("cpu_percent", 0))])
        host = system.get("host_memory")
        if host:
            metric("host_memory_bytes", "gauge", "Host memory, total and available.",
                   [({"state": "total"}, host["total_gb"] * GB), ({"state": "available"}, host["available_gb"] * GB)])
        gpus = system.get("gpus") or []
        if gpus:
            metric("gpu_memory_bytes", "gauge", "GPU memory by state: allocated and reserved by this process, and total.", [
                ({"index": g["index"], "name": g["name"], "state": state}, g[f"{state}_gb"] * GB)
                for g in gpus for state in ("allocated", "reserved", "total")
            ])
        metric("monitoring_dropped_total", "counter", "Monitoring rows dropped because the write queue was full.",
               [({}, self.dropped)])
        return "\n".join(lines) + "\n"
