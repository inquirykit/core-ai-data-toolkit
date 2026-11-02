"""Waiting for what the service does in the background: one way for every test, so a test says what it waits for and
a wait that runs out says what never came, instead of a later assertion failing for a reason it does not name."""

import asyncio
import time
from typing import Awaitable, Callable, TypeVar, Union

T = TypeVar("T")
# How often a check is asked again.
STEP = 0.02
What = Union[str, Callable[[], str]]


def _timed_out(timeout: float, what: What) -> AssertionError:
    said = what() if callable(what) else what
    return AssertionError(f"timed out after {timeout:g} s" + (f": {said}" if said else ""))


def until(check: Callable[[], T], timeout: float = 10.0, what: What = "") -> T:
    """``check()``'s first truthy value, asked every STEP seconds; an AssertionError saying ``what`` (a text, or a
    function giving one, to show the state at the time) once ``timeout`` seconds have passed."""
    deadline = time.monotonic() + timeout
    while True:
        value = check()
        if value:
            return value
        if time.monotonic() >= deadline:
            raise _timed_out(timeout, what)
        time.sleep(STEP)


async def until_async(check: Callable[[], Union[T, Awaitable[T]]], timeout: float = 10.0, what: What = "") -> T:
    """``until`` on the event loop: ``check`` may be a plain function or a coroutine function, and the wait sleeps
    without blocking the loop, so the work waited for goes on."""
    deadline = time.monotonic() + timeout
    while True:
        value = check()
        if asyncio.iscoroutine(value):
            value = await value
        if value:
            return value
        if time.monotonic() >= deadline:
            raise _timed_out(timeout, what)
        await asyncio.sleep(STEP)
