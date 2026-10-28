"""/admin: the dashboard's stats, events, requests, logs and config."""

from typing import Any, Dict, List, Literal, Optional


from .common import Out


class TimelinePoint(Out):
    start: float
    ok: int
    client_error: int
    error: int
    p50_ms: Optional[float]
    p95_ms: Optional[float]


class ModelStats(Out):
    model_key: str
    model_id: str
    task: str
    requests: int
    client_errors: int
    errors: int
    cold_starts: int
    last_request: Optional[float]
    p50_ms: Optional[float]
    p95_ms: Optional[float]


class Totals(Out):
    requests: int
    ok: int
    client_errors: int
    errors: int
    error_rate: float
    requests_per_min: float
    p50_ms: Optional[float]
    p95_ms: Optional[float]


class Timeline(Out):
    """GET /admin/timeline: request metrics over a range."""
    since: float
    until: float
    bucket_s: float
    totals: Totals
    models: List[ModelStats]
    timeline: List[TimelinePoint]


class LoadedModel(Out):
    model_id: str
    task: str
    device: str
    torch_dtype: str
    loaded_at: Optional[float]
    last_used: Optional[float]
    # GB counted against the memory budget
    memory_usage: Optional[float]
    # Requests holding the model now
    in_use: int
    # "in-process", or "server" for a model with its own server process
    kind: str
    # Which backend of a model server answers, e.g. "ollama: qwen3.5:4b"; None for other models
    backend: Optional[str] = None


class CacheStatus(Out):
    cached_models: List[str]
    cache_size: int
    max_models: int
    memory_backend: str
    memory_used_gb: float
    device_memory_gb: Optional[float]
    free_memory_gb: Optional[float]
    max_memory_gb: float
    model_details: Dict[str, LoadedModel]


class Gpu(Out):
    index: int
    name: str
    allocated_gb: float
    reserved_gb: float
    total_gb: float


class HostMemory(Out):
    total_gb: float
    available_gb: float
    percent: float


class ProcessInfo(Out):
    device: str
    pid: int
    python: str
    uptime_s: float
    gpus: List[Gpu]
    # Only with psutil installed.
    cpu_percent: Optional[float] = None
    cpu_count: Optional[int] = None
    rss_gb: Optional[float] = None
    host_memory: Optional[HostMemory] = None


class MonitoringInfo(Out):
    enabled: bool
    dropped: int
    retention_days: float


class Stats(Timeline):
    """GET /admin/stats: process and cache state now, plus request metrics over ``window``."""
    window: Literal["1h", "24h", "7d"]
    monitoring: MonitoringInfo
    process: ProcessInfo
    cache: CacheStatus


class ModelEvent(Out):
    id: int
    ts: float
    kind: str  # load, load_failed, evict, unload, pin, unpin, limits
    model_key: str
    duration_ms: Optional[float]
    detail: Optional[str]


class ModelEvents(Out):
    events: List[ModelEvent]


class RequestRow(Out):
    id: int
    ts: float
    model_id: str
    task: str
    status: str  # ok, client_error, error
    duration_ms: float
    cold: bool
    error: Optional[str]
    # The request's trace: its input and output, when tracing is on
    trace_id: Optional[str] = None


class RequestRows(Out):
    requests: List[RequestRow]


class LogLine(Out):
    id: int
    ts: float
    level: str
    levelno: int
    logger: str
    message: str


class Logs(Out):
    lines: List[LogLine]
    # How many lines the server keeps
    capacity: int
    # The server's LOG_LEVEL: nothing below it is ever logged
    level: str


class AdminConfig(Out):
    settings: Dict[str, Any]
    warnings: List[str]


class LimitsResult(Out):
    max_memory_gb: float
    max_models: int
    evicted: List[str]
    env_file: str
    notes: List[str]
