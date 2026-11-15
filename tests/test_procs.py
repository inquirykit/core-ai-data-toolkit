"""procs: process groups stopped gracefully, and killed at exit with the children they leave."""

import asyncio
import os
import sys
import time

from core import procs

# Ignores SIGTERM, so stop() has to escalate.
STUBBORN = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('up', flush=True); time.sleep(60)"
# Starts a grandchild in the same group, prints its pid and exits: what a job leaving a Ray worker behind looks like.
LEAVES_A_CHILD = (
    "import subprocess, sys; "
    "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], stdout=subprocess.DEVNULL); "
    "print(p.pid, flush=True)"
)


async def _spawn(code: str) -> tuple:
    """The process, in its own group, and the first line it printed."""
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", code, stdout=asyncio.subprocess.PIPE, start_new_session=True,
    )
    return process, (await process.stdout.readline()).decode().strip()


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _gone(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            # Reap it if it is our child; a zombie still answers kill(pid, 0).
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return False


def test_stop_escalates_to_sigkill_and_forgets_the_process():
    async def scenario():
        process, _ = await _spawn(STUBBORN)
        procs.track(process)
        started = time.monotonic()
        await procs.stop(process, grace=0.3)
        return process, time.monotonic() - started

    process, took = asyncio.run(scenario())
    assert process.returncode == -9 and took < 5
    assert process not in procs._tracked


def test_leftover_groups_are_killed_at_exit_with_their_children():
    async def scenario():
        process, child = await _spawn(LEAVES_A_CHILD)
        procs.track(process)
        await process.wait()  # the leader has exited; its child lives on in the group
        return process, int(child)

    process, child = asyncio.run(scenario())
    assert process.returncode == 0 and _alive(child)
    procs._kill_leftovers()
    procs.untrack(process)
    assert _gone(child)


def test_python_env_puts_the_project_on_the_path():
    env = procs.python_env(True, JOB_ID="j1")
    assert env["PYTHONPATH"].split(os.pathsep)[0] == str(procs.PROJECT_ROOT)
    assert env["CUDA_VISIBLE_DEVICES"] == "" and env["PYTHONUNBUFFERED"] == "1" and env["JOB_ID"] == "j1"
    assert procs.python_env(False).get("CUDA_VISIBLE_DEVICES") == os.environ.get("CUDA_VISIBLE_DEVICES")


def test_a_child_gets_what_a_program_needs_and_never_a_key(monkeypatch):
    """Whatever a secret is named, a child does not get it: only what is listed as a program's needs passes."""
    for name, value in (("TK_TEST_SECRET", "s3cret"), ("API_KEY", "k"), ("PROVIDER_CLOUD_API_KEY", "sk"),
                        ("R2_SECRET_ACCESS_KEY", "r2"), ("NVIDIA_API_KEY", "nv"), ("OLLAMA_API_KEY", "ol"),
                        ("HTTPS_PROXY", "http://proxy:3128"), ("HF_HOME", "/hf"), ("HF_TOKEN", "hf_x"),
                        ("POLARS_MAX_THREADS", "2"), ("LC_ALL", "C"), ("CUDA_VISIBLE_DEVICES", "0")):
        monkeypatch.setenv(name, value)
    env = procs.python_env(False, JOB_ID="j1")
    assert not {"TK_TEST_SECRET", "API_KEY", "PROVIDER_CLOUD_API_KEY", "R2_SECRET_ACCESS_KEY", "NVIDIA_API_KEY",
                "OLLAMA_API_KEY"} & set(env)
    assert env["HF_TOKEN"] == "hf_x" and env["POLARS_MAX_THREADS"] == "2"
    assert env["HTTPS_PROXY"] == "http://proxy:3128" and env["HF_HOME"] == "/hf" and env["LC_ALL"] == "C"
    assert env["CUDA_VISIBLE_DEVICES"] == "0" and env["PATH"] == os.environ["PATH"] and env["JOB_ID"] == "j1"
    assert procs.child_env(TK_GIVEN="1")["TK_GIVEN"] == "1"
