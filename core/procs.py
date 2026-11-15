"""
Child processes the service starts in their own process group (jobs, model
servers, AutoGluon predictors): what of the environment they
get (``child_env``: what a program needs to run, never the service's settings
or keys); signal the whole group, so their children get it too; stop one
gracefully; and kill every tracked one still running when the service exits,
however it exits.
"""

import asyncio
import atexit
import os
import signal
from pathlib import Path
from typing import Dict, Optional, Set

from . import PROJECT_ROOT


_tracked: Set[asyncio.subprocess.Process] = set()


def signal_group(pid: int, sig: int) -> None:
    try:
        os.killpg(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def env_python(envs_dir: Path, name: str) -> Optional[Path]:
    """The interpreter of job environment ``name`` (``envs_dir/<name>``), or None until it is set up: its install
    finished (scripts/setup_job_env.sh writes ``.ready`` last) and its ``bin/python`` resolves (it links to a Python
    uv keeps elsewhere)."""
    target = envs_dir / name
    python = target / "bin" / "python"
    return python if (target / ".ready").is_file() and python.exists() else None


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


# What a child gets of this process's environment: what any program needs to run (where things are, the locale, the
# proxies and certificates that reach the network, the GPUs) and the settings of the ML runtimes and caches its tools
# read. Never the service's own settings, nor a key it holds (its own, the providers', the enrichers', the remotes'),
# whatever they are named: a name nobody listed here stays with the service. Anything else a child needs is given to
# it, as a model server entry's ``env`` and a job's own are.
PASSED = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LANGUAGE", "TZ", "TERM", "TMPDIR", "TEMP", "TMP",
    "VIRTUAL_ENV", "LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH", "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "no_proxy",
    "all_proxy", "POLARS_MAX_THREADS", "RAYON_NUM_THREADS", "LOKY_MAX_CPU_COUNT", "MPLCONFIGDIR",
    "SENTENCE_TRANSFORMERS_HOME", "UV_PYTHON_INSTALL_DIR", "UV_NO_CACHE", "HF_TOKEN", "HUGGING_FACE_HUB_TOKEN",
    "HF_HUB_DISABLE_IMPLICIT_TOKEN",
})
PASSED_PREFIXES = (
    "LC_", "XDG_", "PYTHON", "PYENV_", "CONDA_", "HF_", "HUGGING_FACE_", "HUGGINGFACE_", "TRANSFORMERS_",
    "TORCH", "PYTORCH_", "CUDA", "NVIDIA_", "CUDNN_", "NCCL_", "OMP_", "KMP_", "GOMP_", "MKL_", "OPENBLAS_", "VECLIB_",
    "TOKENIZERS_", "NUMEXPR_", "MALLOC_", "RAY_", "MLX_", "METAL_", "VLLM_", "SGLANG_", "LLAMA_", "GGML_", "OLLAMA_",
)
# A name like a key's is never passed by its prefix (NVIDIA_API_KEY, OLLAMA_API_KEY): only the Hub token, by name,
# which a model's download and the Hub's own code need, as they always had it.
KEY_ENDINGS = ("_KEY", "_TOKEN", "_SECRET", "_PASSWORD")


def passed(name: str) -> bool:
    """Whether every child gets the environment variable ``name``: so no secret may be named so
    (api/settings.py, check_secret_name; enrichers/spec.py)."""
    return name in PASSED or (name.startswith(PASSED_PREFIXES) and not name.upper().endswith(KEY_ENDINGS))


def child_env(**extra: str) -> Dict[str, str]:
    """The variables of this process's environment a child gets (``passed``), plus ``extra``: what every child
    starts with."""
    return {**{k: v for k, v in os.environ.items() if passed(k)}, **extra}


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
