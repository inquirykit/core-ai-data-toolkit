"""
Periodic background work: ``every`` runs something on a cadence (the job
scheduler's tick, trace pruning, time stamp retries, unloading idle models).
Queued work (jobs, eval runs) is the task runner's (tasks/runner.py).
"""

import asyncio
import inspect
import logging
from typing import Any, Awaitable, Callable, Set

logger = logging.getLogger(__name__)

# The tasks to_its_end runs, held until they end (the event loop holds a task only weakly).
_finishing: Set["asyncio.Task[Any]"] = set()


async def to_its_end(work: Awaitable[Any]) -> Any:
    """Await ``work`` in a task of its own, which a cancel of the caller does not stop: a cancel reaches the caller at
    once, and the work goes on to its end. For undoing what must not be left half done (a model server started for a
    request that went away, a model half unloaded)."""
    task = asyncio.ensure_future(work)
    _finishing.add(task)
    task.add_done_callback(_finishing.discard)
    return await asyncio.shield(task)


async def every(seconds: float, fn: Callable[[], Any], name: str, first: float = 0.0) -> None:
    """Run ``fn`` (plain or async) every ``seconds``, the first time after ``first``, until cancelled.

    A failing run is logged and the loop goes on.
    """
    await asyncio.sleep(first)
    while True:
        try:
            result = fn()
            if inspect.isawaitable(result):
                await result
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("%s failed", name)
        await asyncio.sleep(seconds)

