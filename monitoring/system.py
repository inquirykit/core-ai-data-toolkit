"""
Point-in-time process, host and GPU figures for the /admin dashboard.
"""

import os
import platform
import sys
import time
from typing import Any, Dict, List

try:
    import psutil

    _PROCESS = psutil.Process(os.getpid())
    _PROCESS.cpu_percent(None)  # first call primes the measurement
except ImportError:  # psutil is optional, as in models/cache.py
    psutil = None
    _PROCESS = None

_STARTED = time.time()
GB = 1024 ** 3


def _gpus() -> List[Dict[str, Any]]:
    # Only look if a model already imported torch; importing it here would
    # cost seconds and memory just to report that nothing is loaded.
    torch = sys.modules.get("torch")
    if torch is None or not torch.cuda.is_available():
        return []
    gpus = []
    for index in range(torch.cuda.device_count()):
        props = torch.cuda.get_device_properties(index)
        gpus.append({
            "index": index,
            "name": props.name,
            "allocated_gb": torch.cuda.memory_allocated(index) / GB,
            "reserved_gb": torch.cuda.memory_reserved(index) / GB,
            "total_gb": props.total_memory / GB,
        })
    return gpus


def snapshot() -> Dict[str, Any]:
    info: Dict[str, Any] = {
        "pid": os.getpid(),
        "python": platform.python_version(),
        "uptime_s": time.time() - _STARTED,
        "gpus": _gpus(),
    }
    if psutil is not None:
        memory = psutil.virtual_memory()
        info.update({
            "cpu_percent": _PROCESS.cpu_percent(None),
            "cpu_count": psutil.cpu_count(),
            "rss_gb": _PROCESS.memory_info().rss / GB,
            "host_memory": {
                "total_gb": memory.total / GB,
                "available_gb": memory.available / GB,
                "percent": memory.percent,
            },
        })
    return info
