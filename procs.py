"""
Child processes the service starts in their own process group (jobs, model
servers, AutoGluon predictors): signal the whole group, so their children get
it too; stop one gracefully; and kill every tracked one still running when the
service exits, however it exits.
"""

import asyncio
import atexit
import os
import signal
from pathlib import Path
from typing import Dict, Set

PROJECT_ROOT = Path(__file__).resolve().parent

_tracked: Set[asyncio.subprocess.Process] = set()


def signal_group(pid: int, sig: int) -> None:
    try:
        os.killpg(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def track(process: asyncio.subprocess.Process) -> asyncio.subprocess.Process:
    """Kill ``process``'s group at exit unless it was stopped (``stop``) or let go (``untrack``) first."""
    _tracked.add(process)
    return process


def untrack(process: asyncio.subprocess.Process) -> None:
    _tracked.discard(process)


async def stop(process: asyncio.subprocess.Process, grace: float) -> None:
    """SIGTERM the process's group, then SIGKILL it if the process has not exited after ``grace`` seconds."""
    if process.returncode is None:
        signal_group(process.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), grace)
        except asyncio.TimeoutError:
            signal_group(process.pid, signal.SIGKILL)
            await process.wait()
    untrack(process)


# Environment variables no child gets: the service's keys and secrets (api/app.py names them with ``hide``). A job
# or a model server has no use for the API keys, the webhook secret or the enrichers' keys, and leaks them less
# when it never has them.
hidden: Set[str] = set()


def hide(*names: str) -> None:
    """Keep these environment variables out of every child's environment from now on."""
    hidden.update(name for name in names if name)


def child_env(**extra: str) -> Dict[str, str]:
    """This process's environment without the hidden names, plus ``extra``: what every child starts with."""
    return {**{k: v for k, v in os.environ.items() if k not in hidden}, **extra}


def python_env(cpu_only: bool, **extra: str) -> Dict[str, str]:
    """The environment for a Python child that imports this project: unbuffered output, no GPU when ``cpu_only``."""
    env = child_env(
        PYTHONPATH=os.pathsep.join(filter(None, [str(PROJECT_ROOT), os.environ.get("PYTHONPATH")])),
        PYTHONUNBUFFERED="1",
        **extra,
    )
    if cpu_only:
        env["CUDA_VISIBLE_DEVICES"] = ""
    return env


@atexit.register
def _kill_leftovers() -> None:
    # Whether or not the leader has exited: children it left in its group (a Ray worker, vLLM's engine) go too.
    for process in list(_tracked):
        signal_group(process.pid, signal.SIGKILL)
