"""
The servers this service starts for a model's cmd backend, and what happens
to a server when its model is evicted.

spawn() starts the command on a free port, as its own process group, and
tracks it with procs so it is stopped at exit; wait_ready() waits until it
answers its health path. On eviction a started server is stopped, or with
``on_evict = "sleep"`` put to sleep and held by model ID until wake() wakes
it for the model's next load; with ``on_evict = "unload"`` an Ollama server
is asked to unload the model now (``keep_alive`` 0). stop_all() stops every
server started, sleeping ones included, at shutdown.
"""

import asyncio
import logging
import shlex
import socket
import time
from typing import Any, Dict, Optional, Tuple

import procs

from .server_specs import Backend

logger = logging.getLogger(__name__)

# Every server process started, so shutdown can stop them all; and the
# sleeping ones (on_evict = "sleep"), by model ID, with their base URL.
_processes: Dict[int, asyncio.subprocess.Process] = {}
_sleeping: Dict[str, Tuple[asyncio.subprocess.Process, str]] = {}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def stop(process: asyncio.subprocess.Process, grace: float = 15.0) -> None:
    # Each server is its own process group, so its children (vLLM's engine processes, say) stop too.
    await procs.stop(process, grace)
    _processes.pop(process.pid, None)


async def stop_all() -> None:
    """Stop every server this process started, sleeping ones included (at shutdown)."""
    _sleeping.clear()
    await asyncio.gather(*(stop(p) for p in list(_processes.values())))


async def stop_sleeping(model_id: str) -> None:
    """Stop the model's sleeping server, if it has one (its entry changed)."""
    held = _sleeping.pop(model_id, None)
    if held is not None:
        await stop(held[0])


async def spawn(model_id: str, backend: Backend) -> Tuple[asyncio.subprocess.Process, str]:
    """Start the backend's command for ``model_id``: the process and its base URL."""
    port = _free_port()
    argv = [arg.replace("{port}", str(port)) for arg in backend.cmd]
    logger.info("Starting model server for %s: %s", model_id, shlex.join(argv))
    try:
        process = await asyncio.create_subprocess_exec(
            *argv, env=procs.child_env(**backend.env), start_new_session=True
        )
    except FileNotFoundError:
        raise RuntimeError(f"{model_id}: {argv[0]} not found; install it or fix cmd") from None
    _processes[process.pid] = procs.track(process)
    return process, f"http://127.0.0.1:{port}"


async def wake(model_id: str, backend: Backend) -> Optional[Tuple[asyncio.subprocess.Process, str]]:
    """The sleeping server for this model, woken up, if there is one."""
    held = _sleeping.pop(model_id, None)
    if held is None:
        return None
    process, base_url = held
    if process.returncode is None:
        import httpx

        try:
            async with httpx.AsyncClient(base_url=base_url, timeout=backend.startup_timeout) as client:
                (await client.post("/wake_up")).raise_for_status()
            logger.info("Woke model server for %s", model_id)
            return process, base_url
        except Exception:
            logger.warning("Could not wake %s; starting a new server", model_id, exc_info=True)
    await stop(process)
    return None


async def wait_ready(model_id: str, client: Any, backend: Backend, process: asyncio.subprocess.Process) -> None:
    """Wait until the started ``process`` answers ``backend.health_path`` through ``client`` (an httpx client)."""
    import httpx

    deadline = time.monotonic() + backend.startup_timeout
    while True:
        if process.returncode is not None:
            raise RuntimeError(
                f"{model_id}: model server exited with code {process.returncode} while starting "
                "(its output is in this service's log)"
            )
        try:
            if (await client.get(backend.health_path, timeout=5.0)).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        if time.monotonic() > deadline:
            raise RuntimeError(f"{model_id}: model server not ready after {backend.startup_timeout:.0f}s")
        await asyncio.sleep(0.5)


async def sleep(model_id: str, process: asyncio.subprocess.Process, base_url: str) -> bool:
    """Put the model's server to sleep and hold it for wake(); False (it should be stopped) if it would not."""
    import httpx

    try:
        async with httpx.AsyncClient(base_url=base_url, timeout=120.0) as client:
            (await client.post("/sleep", params={"level": 1})).raise_for_status()
        logger.info("Model server for %s is asleep", model_id)
    except Exception:
        logger.warning("Could not put %s to sleep; stopping it", model_id, exc_info=True)
        return False
    _sleeping[model_id] = (process, base_url.rstrip("/"))
    return True


async def unload_upstream(client: Any, label: str, model: str) -> None:
    """Ask an Ollama server to unload the model now (keep_alive 0) instead of keeping it for minutes."""
    try:
        response = await client.post("/api/generate", json={"model": model, "keep_alive": 0}, timeout=30.0)
        response.raise_for_status()
        logger.info("Asked %s to unload %s", label, model)
    except Exception as e:
        logger.warning("Could not ask %s to unload %s: %s", label, model, e)
