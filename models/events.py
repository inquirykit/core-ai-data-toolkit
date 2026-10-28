"""
What the model manager tells the rest of the service about each request.

Every request it serves, or fails to, becomes one RequestEvent, handed to each
of its sinks: callables the app gives it (the metrics, which forward to the
monitoring store, and the trace store). The manager knows nothing of who
listens, so adding one (a log, an exporter) is one more sink; a sink that
fails is logged and never fails the request. Model events (loads, evictions)
go to the manager's monitor instead.

``request_context`` carries how the current request is tagged (api/app.py
sets it from X-Trace-Tag, X-Session-Id and X-Priority), lets the eval runner
keep its own requests out of the traces (``skip``), and says how urgent the
request is for the model cache (``priority``).
"""

import contextvars
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional

# How urgent a request is when models compete for room in the cache (models/cache.py). A request someone waits on is
# high, the default; the eval runner's and the judge's are normal; low is for bulk work its client marks so.
LOW, NORMAL, HIGH = 0, 1, 2
PRIORITIES = {"low": LOW, "normal": NORMAL, "high": HIGH}


@dataclass
class RequestContext:
    """How the current request is tagged; ``skip`` keeps its inputs and outputs out of the traces."""

    tags: List[str] = field(default_factory=list)
    session: Optional[str] = None
    skip: bool = False
    priority: int = HIGH


request_context: contextvars.ContextVar[Optional[RequestContext]] = contextvars.ContextVar("request_context", default=None)


@dataclass
class Call:
    """One input of a request, its output (None when the request failed) and the id of its trace."""

    trace_id: str
    input: Any
    output: Any = None


@dataclass
class RequestEvent:
    model_id: str
    task: str
    # "ok"; "client_error" for a request the client got wrong (a ValueError, errors.Refused...); else "error"
    status: str
    duration_ms: float
    # Whether the model had to be loaded first
    cold: bool
    # One per input: a batch is one request with several
    calls: List[Call]
    # Which of a model server's backends answered; None for other models
    backend: Optional[str] = None
    # The failure's type name, and the type with its message
    error: Optional[str] = None
    error_message: Optional[str] = None
    # Counted by generation results; other tasks have none
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    context: RequestContext = field(default_factory=RequestContext)


Sink = Callable[[RequestEvent], None]
