"""
What the model manager tells the rest of the service about each request.

Every request it serves, or fails to, becomes one RequestEvent, handed to each
of its sinks: callables the app gives it (the metrics, which count it and hand
it to the request log, monitoring/store.py). The manager knows nothing of who
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
from typing import Any, Callable, List, Literal, Optional

from core.priorities import HIGH



@dataclass
class RequestContext:
    """How the current request is tagged; ``skip`` keeps its inputs and outputs out of the traces."""

    tags: List[str] = field(default_factory=list)
    session: Optional[str] = None
    skip: bool = False
    priority: int = HIGH
    # Where it came from, as the request log keeps it: "api" (an app, by a client key or none), "admin" (the admin's
    # key: the playground, /admin), "data" (a /db table embedding its rows or reranking a search), "workflow" or "eval"
    # (their runs); None for anything else (preloading, a test).
    source: Optional[str] = None
    # The only models the request's client key may use (access/grants.py; api/deps.py's model_allowed holds it to
    # them); None for any the allow-list lets through. A workflow's own context has none: its models are the admin's.
    models: Optional[List[str]] = None


request_context: contextvars.ContextVar[Optional[RequestContext]] = contextvars.ContextVar("request_context", default=None)


@dataclass
class Call:
    """One input of a request, its output (None when the request failed) and the id of its trace."""

    trace_id: str
    input: Any
    output: Any = None


# How a request went: "ok"; "client_error" for one the client got wrong (a ValueError, errors.Refused...); else
# "error". The request log counts each (monitoring/store.py), and the API declares it (api/responses/common.py).
RequestStatus = Literal["ok", "client_error", "error"]


@dataclass
class RequestEvent:
    model_id: str
    task: str
    status: RequestStatus
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
