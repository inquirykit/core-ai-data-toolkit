"""
Runs one job, in its environment's interpreter:

    python -m jobs.worker <module> <class> <workdir>

The runner (jobs/runner.py) starts it in a new process group with the
project root on PYTHONPATH. Its params are in <workdir>/params.json.

stdout is a channel to the runner: one JSON object per line ("progress",
"result" or "error"). Everything the job and its libraries print goes to
stderr instead, which the runner saves as the job's log.
"""

import importlib
import json
import logging
import os
import signal
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Callable, TextIO


def open_channel() -> TextIO:
    """Keep the real stdout for messages to the parent; point fd 1 (prints, libraries) at stderr."""
    sys.stdout.flush()
    channel = os.fdopen(os.dup(1), "w", buffering=1, encoding="utf-8")
    os.dup2(2, 1)
    return channel


def emitter(channel: TextIO) -> Callable[..., None]:
    lock = threading.Lock()

    def emit(**message: Any) -> None:
        line = json.dumps(message, default=str)
        with lock:
            channel.write(line + "\n")
            channel.flush()

    return emit


def exit_with_parent(every: float = 2.0) -> None:
    """End this process group if the service that started it goes away.

    The job runs in its own session, so it would otherwise outlive a crashed
    service and keep its CPUs busy with nobody to report to.
    """
    parent = os.getppid()

    def watch() -> None:
        while True:
            time.sleep(every)
            if os.getppid() != parent:
                # SIGKILL, not SIGTERM: SIGTERM would end this process first,
                # leaving no one to stop children that ignore it (Ray's monitor).
                os.killpg(os.getpgrp(), signal.SIGKILL)

    threading.Thread(target=watch, daemon=True, name="exit-with-parent").start()


def load_class(module: str, qualname: str) -> type:
    target: Any = importlib.import_module(module)
    for part in qualname.split("."):
        target = getattr(target, part)
    return target


def main(argv: list) -> int:
    module, qualname, workdir = argv
    workdir = Path(workdir)
    emit = emitter(open_channel())
    exit_with_parent()
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    from jobs.base import JobContext

    try:
        cls = load_class(module, qualname)
        params = cls.Params.model_validate_json((workdir / "params.json").read_text())
        ctx = JobContext(
            id=os.environ.get("JOB_ID", workdir.name),
            workdir=workdir,
            num_cpus=int(os.environ.get("JOB_NUM_CPUS") or os.cpu_count() or 1),
            device=os.environ.get("JOB_DEVICE") or "cpu",
            emit=emit,
        )
        result = cls().run(ctx, params)
        if cls.Result is not None:
            result = cls.Result.model_validate(result)
        if hasattr(result, "model_dump"):
            result = result.model_dump(mode="json")
        (workdir / "result.json").write_text(json.dumps(result, indent=2, default=str))
        emit(type="result", result=result)
        return 0
    except BaseException as e:  # KeyboardInterrupt and SystemExit too: report them, then fail
        traceback.print_exc()
        emit(type="error", error=f"{type(e).__name__}: {e}"[:2000])
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
