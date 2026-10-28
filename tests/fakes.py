import asyncio
import dataclasses
from collections import Counter
from contextlib import contextmanager
from typing import Any

from models import BaseModel, registry


def sample_result(task: str, request: Any) -> Any:
    """A result that fits ``task``'s contract; for a task without one, the request echoed."""
    if task in ("text-generation", "vision-language", "custom"):
        return {"text": f"echo: {request.text}"}
    if task == "text-classification":
        return {"label": "POSITIVE", "confidence": 0.9, "scores": {"NEGATIVE": 0.1, "POSITIVE": 0.9}}
    if task == "question-answering":
        return {"answer": request.context.split()[0], "confidence": 0.9, "start_position": 0, "end_position": 0}
    if task == "rerank":
        # Longer documents score higher, so the order is predictable.
        order = sorted(range(len(request.documents)), key=lambda i: -len(request.documents[i]))[: request.top_n]
        return {"results": [{"index": i, "score": len(request.documents[i]) / 100} for i in order], "tokens": 7}
    return {"echo": request}


class FakeModel(BaseModel):
    """In-memory model that records lifecycle calls and simulates slow work."""

    load_delay = 0.0
    predict_delay = 0.0
    # What estimate_bytes() reports: the size the cache budgets.
    size_bytes = 0
    instances = []
    last_request = None
    # ("load" | "unload", model_id), in order, across instances.
    log = []

    async def estimate_bytes(self):
        return self.size_bytes

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Mutated in place, so unloading (which restores the pre-load
        # attributes) keeps the counts.
        self.calls = Counter()
        FakeModel.instances.append(self)

    load_calls = property(lambda self: self.calls["load"])
    unload_calls = property(lambda self: self.calls["unload"])
    predict_calls = property(lambda self: self.calls["predict"])

    async def load(self):
        self.calls["load"] += 1
        FakeModel.log.append(("load", self.model_id))
        await asyncio.sleep(self.load_delay)
        self.weights = object()

    async def predict(self, request: Any) -> Any:
        self.calls["predict"] += 1
        FakeModel.last_request = request
        await asyncio.sleep(self.predict_delay)
        return sample_result(self.task, request)

    def unload(self):
        self.calls["unload"] += 1
        FakeModel.log.append(("unload", self.model_id))


@contextmanager
def swapped(old: type, new: type):
    """Serve ``new`` wherever ``old`` is registered, same IDs and patterns."""
    registry.discover()
    saved = list(registry._entries)
    registry._entries[:] = [dataclasses.replace(e, cls=new) if e.cls is old else e for e in saved]
    try:
        yield
    finally:
        registry._entries[:] = saved


@contextmanager
def registered(cls: type, **kwargs):
    """Register ``cls`` (register() keyword arguments) for the duration."""
    registry.discover()
    saved = list(registry._entries)
    saved_tasks = cls.__dict__.get("tasks")
    registry.register(**kwargs)(cls)
    try:
        yield
    finally:
        registry._entries[:] = saved
        if saved_tasks is None:
            del cls.tasks
        else:
            cls.tasks = saved_tasks
