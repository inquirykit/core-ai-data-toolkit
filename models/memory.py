"""
How much memory a model needs, and how much the device has.

The cache budgets loaded models by their size: an estimate made before
loading (from safetensors headers, so nothing is downloaded or allocated) or,
when there is no estimate, what loading actually took. Free memory is the
safety net for what the budget cannot see, such as other processes.
"""

import ctypes
import json
import logging
import os
import struct
import sys
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

GB = 1024 ** 3

# Weights are not everything: activations, the KV cache and allocator slack
# come on top. Accelerate's estimator suggests up to 20% for inference.
OVERHEAD = 1.2

# Bytes per element of the dtypes safetensors headers name.
_DTYPE_BYTES = {
    "F64": 8, "I64": 8, "U64": 8,
    "F32": 4, "I32": 4, "U32": 4,
    "F16": 2, "BF16": 2, "I16": 2, "U16": 2,
    "F8_E4M3": 1, "F8_E5M2": 1, "I8": 1, "U8": 1, "BOOL": 1,
}


def _torch():
    """torch if a model already imported it; never import it just to measure."""
    return sys.modules.get("torch")


# ---------------------------------------------------------------- estimates
def _header_params(path: Path) -> Dict[str, int]:
    """dtype -> parameter count, read from one safetensors file's header."""
    with path.open("rb") as f:
        (length,) = struct.unpack("<Q", f.read(8))
        header = json.loads(f.read(length))
    counts: Dict[str, int] = {}
    for name, tensor in header.items():
        if name == "__metadata__":
            continue
        n = 1
        for dim in tensor["shape"]:
            n *= dim
        counts[tensor["dtype"]] = counts.get(tensor["dtype"], 0) + n
    return counts


def _local_dir(model_id: str) -> Optional[Path]:
    """A directory holding the model's files without downloading: a path, or the Hub cache."""
    path = Path(model_id).expanduser()
    if path.is_dir():
        return path
    try:
        from huggingface_hub import snapshot_download

        return Path(snapshot_download(model_id, local_files_only=True, allow_patterns=["*.json"]))
    except Exception:
        return None


def _param_counts(model_id: str) -> Optional[Dict[str, int]]:
    local = _local_dir(model_id)
    if local is not None:
        files = sorted(local.glob("*.safetensors"))
        if files:
            counts: Dict[str, int] = {}
            for file in files:
                for dtype, n in _header_params(file).items():
                    counts[dtype] = counts.get(dtype, 0) + n
            return counts
    if os.environ.get("HF_HUB_OFFLINE", "").lower() in ("1", "true", "yes"):
        return None
    try:
        from huggingface_hub import get_safetensors_metadata

        return dict(get_safetensors_metadata(model_id, timeout=10).parameter_count)
    except Exception:
        return None


def estimate_bytes(model_id: str, element_size: Optional[int] = None) -> Optional[int]:
    """Memory ``model_id`` needs once loaded, or None when it cannot be told.

    ``element_size`` is the bytes per parameter of the dtype it will be
    loaded in; None keeps the dtypes it is stored in.
    """
    try:
        counts = _param_counts(model_id)
    except Exception:
        logger.debug("No size estimate for %s", model_id, exc_info=True)
        return None
    if not counts:
        return None
    total = sum(n * (element_size or _DTYPE_BYTES.get(dtype, 4)) for dtype, n in counts.items())
    return int(total * OVERHEAD)


# ----------------------------------------------------------------- on disk
_WEIGHT_SUFFIXES = (".safetensors", ".bin", ".gguf", ".pt", ".pth", ".npz", ".ckpt")


def cached_weights(cache_dir: Optional[Path] = None) -> Dict[str, int]:
    """Hub model ID -> bytes of weight files in the local Hub cache, for models with any.

    One scan of the cache (``cache_dir``, default the Hub's), no network. A
    repo holding only its config (left by a size estimate, say) is not
    counted as downloaded.
    """
    try:
        from huggingface_hub import scan_cache_dir

        repos = scan_cache_dir(cache_dir).repos
    except Exception:
        logger.debug("Could not scan the Hub cache", exc_info=True)
        return {}
    out: Dict[str, int] = {}
    for repo in repos:
        if repo.repo_type != "model":
            continue
        # Revisions share blobs; count each once.
        blobs = {
            f.blob_path: f.size_on_disk
            for revision in repo.revisions for f in revision.files
            if f.file_name.endswith(_WEIGHT_SUFFIXES)
        }
        if blobs:
            out[repo.repo_id] = sum(blobs.values())
    return out


def local_weights(model_id: str) -> int:
    """Bytes of weight files when ``model_id`` is a local directory, else 0."""
    path = Path(model_id).expanduser()
    if not path.is_dir():
        return 0
    return sum(f.stat().st_size for f in path.iterdir() if f.is_file() and f.name.endswith(_WEIGHT_SUFFIXES))


# -------------------------------------------------------------- measurement
def device_bytes(device: str) -> int:
    """Memory this process holds on ``device``: what the allocator reserved, not just what tensors use."""
    torch = _torch()
    if device.startswith("cuda") and torch is not None and torch.cuda.is_available():
        return torch.cuda.memory_reserved()
    if device == "mps" and torch is not None and torch.backends.mps.is_available():
        return torch.mps.driver_allocated_memory()
    return rss_bytes()


def rss_bytes() -> int:
    try:
        import psutil

        return psutil.Process(os.getpid()).memory_info().rss
    except ImportError:
        return 0


def free_bytes(device: str) -> Optional[int]:
    """Memory still free for a new model on ``device``, across every process, or None if unknown.

    On a Mac the GPU shares system memory, so free system memory is the
    figure that also accounts for model servers running beside this process.
    """
    torch = _torch()
    if device.startswith("cuda") and torch is not None and torch.cuda.is_available():
        free, _total = torch.cuda.mem_get_info()
        return free
    try:
        import psutil

        return psutil.virtual_memory().available
    except ImportError:
        return None


def trim_heap() -> None:
    """Hand freed heap memory back to the OS (glibc keeps it otherwise, so RSS only grows)."""
    if not sys.platform.startswith("linux"):
        return
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except (OSError, AttributeError):
        pass


def describe(device: str) -> Dict[str, Any]:
    """The device figures /models/status and /admin show."""
    free = free_bytes(device)
    return {
        "device_memory_gb": device_bytes(device) / GB,
        "free_memory_gb": None if free is None else free / GB,
    }
