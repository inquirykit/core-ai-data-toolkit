"""
Models served by OpenAI-compatible servers, each with backends tried in order.

Every model in MODEL_SERVERS_FILE, or saved from /admin (see
entries.py), names one or more backends: a server that already
runs (a ``url``, or a ``provider`` such as Ollama or LM Studio), a server this
service starts (a ``cmd``: mlx_vlm.server, vllm serve, llama-server...), or a
Hub model run in this process (``hub``), the last resort.

The backend is chosen when the model loads, not per request: the cache asks
for the model's size before loading it, and that depends on the backend, and
a reply cannot switch servers halfway. Every backend is probed and the first
that can serve the model wins until the model is unloaded:

- a url backend must answer its ``health_path`` and, when that is a model
  list (``/v1/models``), list the model. One that answers without listing it
  is used only when no other backend can serve the model;
- a cmd backend's program must be installed;
- a hub backend needs a model file that serves its ID for the task.

A cmd server that fails to start, or a hub model that fails to load, is
skipped for ``FAILED_COOLDOWN`` seconds and the load moves on to the next
backend (``TryNextBackend``; the manager retries with a fresh instance). A
url server that stops answering marks the model dead: the cache drops it,
and the request is retried once, on whichever backend is chosen next.

To the cache a model server is like any other model: loaded on demand,
counted against MAX_MEMORY_GB by its backend's ``memory_gb`` (estimated from
the weights for cmd and hub backends; none for a server elsewhere), evicted
when idle. Stopping a started server returns all of its memory, and a crash
in it (a Metal abort, an out-of-memory kill) takes down only that model.

ServerModel, the probes and the fallback are in model.py; what
MODEL_SERVERS_FILE and /admin say is read in entries.py, and the entries in
effect are kept in registry.py; the servers this service starts are in
processes.py, and the requests to a server (chat, completions, embeddings,
rerank, transcriptions) in openai_client.py. The names the rest of the
service uses are imported here, so ``models.servers`` is the one module to
import.
"""

from .entries import parse_specs  # noqa: F401  (the names the rest of the service uses, through models.servers)
from .model import NoBackend, ServerModel, probe  # noqa: F401
from .processes import _free_port, _processes, _sleeping, stop_all  # noqa: F401
from .registry import (  # noqa: F401
    configured, load_configured, register_specs, reload, restored, specs,
)
