"""
Periodic background work: ``every`` runs something on a cadence (the job
scheduler's tick, trace pruning, time stamp retries, unloading idle models).
Queued work (jobs, eval runs) is the task runner's (jobs/tasks.py).
"""

import asyncio
import inspect
import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)


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

