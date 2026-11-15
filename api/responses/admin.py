"""/admin: the dashboard's stats, events, requests, logs and config."""

from typing import Any, Dict, List, Literal, Optional


from .common import Json, Out


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
    # Its requests by where they came from: api, admin, data, workflow, eval (other: before the log kept it)
    sources: Dict[str, int]
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
    # Requests holding the model now, running or waiting
    in_use: int
    # Requests waiting for the model now: for one of its slots, and for a batch when it batches
    waiting: int
    # How long a request arriving now would wait for a slot, in ms; None before one has finished
    estimated_wait_ms: Optional[float]
    # Running average of a request's time in ms; None before one has finished
    avg_request_ms: Optional[float]
    # Requests it runs at once
    max_concurrency: int
    # The most single requests it answers in one pass, when it batches them (models/batching.py); None otherwise
    batch_size: Optional[int] = None
    # Never evicted: only an unload removes it
    pinned: bool
    # "in-process", or "server" for a model with its own server process
    kind: str
    # Which backend of a model server answers, e.g. "ollama: qwen3.5:4b"; None for other models
    backend: Optional[str] = None
    # The server elsewhere that answers it, e.g. "ollama" or "gpu-box:8000"; None when it runs on this machine
    upstream: Optional[str] = None
    # That server is another toolkit; ``backend`` then goes on to the one of its backends that answered
    peer: bool = False
    # The Hub commit its weights were loaded from; None when they are not from the Hub cache
    revision: Optional[str] = None


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


Window = Literal["1h", "24h", "7d"]
# The key a request carried (api/deps.py caller): wrong is a key that opens nothing, open that no key is set.
Actor = Literal["admin", "client", "none", "wrong", "open"]


class Stats(Timeline):
    """GET /admin/stats: process and cache state now, plus request metrics over ``window``."""
    window: Window
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


class AuditEntry(Out):
    """A change made through the API (api/audit.py): what was asked, with which key, from where, and how it went."""
    id: int
    ts: float
    method: str
    # The route's path, /db/{workspace}/{table}; ``path`` is the one asked for, with its query
    route: str
    path: str
    status: int
    duration_ms: float
    actor: Actor
    # The client key's id (GET /admin/keys), when the key sent was one, live or not
    key_id: Optional[str]
    ip: Optional[str]
    forwarded: Optional[str]
    agent: Optional[str]


class AuditEntries(Out):
    entries: List[AuditEntry]
    next: Optional[str] = None
    # Whether the audit log is on (AUDIT_RETENTION_DAYS above 0, and the database)
    on: bool
    retention_days: float


class RequestRow(Out):
    id: int
    ts: float
    model_id: str
    task: str
    status: str  # ok, client_error, error
    duration_ms: float
    cold: bool
    # The error's type
    error: Optional[str]
    # Its first input's trace while kept (with q, one whose words match): GET /admin/traces/{trace_id}
    trace_id: Optional[str]
    tags: List[str]
    session: Optional[str]
    # Which backend of a model server answered, e.g. "ollama: qwen3.5:4b"
    backend: Optional[str]
    # Where it came from: api (an app), admin (the playground, /admin), data (a /db table), workflow, eval
    source: Optional[str] = None
    # The first words of its input and output, while its trace is kept
    preview: Optional[str]


class TraceCapture(Out):
    """Whether requests' inputs and outputs are kept, how long, cut to how many characters, and how many requests
    were not kept because the queue was full."""
    enabled: bool
    retention_days: float
    max_chars: int
    dropped: int


class RequestRows(Out):
    requests: List[RequestRow]
    capture: TraceCapture


class Trace(Out):
    """One input of a request with its output, and the request's model, outcome and timing."""
    id: str
    ts: float
    model_id: str
    task: str
    status: str
    duration_ms: float
    cold: bool
    # The request's error, with its text
    error: Optional[str]
    tags: List[str]
    session: Optional[str]
    backend: Optional[str]
    input_chars: int
    output_chars: int
    preview: str
    input: Json
    output: Optional[Json]


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
