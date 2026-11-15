"""
ServerModel: a model served by an OpenAI-compatible server, its backends
probed and the first that can serve it chosen when it loads (see the
package's docstring); probe() for the /admin page.
"""

import asyncio
import logging
import shutil
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from .. import memory
from ..base import BaseModel, TryNextBackend
from ..cache import CapacityError
from . import processes
from .entries import Backend, ServerSpec, provider_key, with_provider, without_credentials
from .openai_client import OpenAIClient, bearer
from .processes import _sleeping
from .registry import _failed, _providers, _specs

logger = logging.getLogger(__name__)

# Seconds a url backend has to answer when probed: a hosted service's model list is long and comes over the internet.
PROBE_TIMEOUT = 5.0
# Seconds a backend that failed to start is skipped for, when the model has another.
FAILED_COOLDOWN = 60.0


class NoBackend(CapacityError):
    """None of a model's backends can serve it now (routes answer 503 with the reasons)."""


# ------------------------------------------------------------------ probing
@dataclass
class Probe:
    """What probing found for one backend. ``state``: "ok", "unlisted" (answers without listing the
    model; used only when nothing else can serve it) or "unavailable"."""

    index: int
    label: str
    kind: str
    state: str
    detail: str
    # The server is another toolkit (its model list says so)
    peer: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _cooling(model_id: str, index: int) -> Optional[Tuple[float, str]]:
    """(seconds since this backend failed to start, why) while it is still skipped; else None."""
    failed = _failed.get((model_id, index))
    if failed is None:
        return None
    ago = time.monotonic() - failed[0]
    return (ago, failed[1]) if ago < FAILED_COOLDOWN else None


def _listed(response: Any) -> Optional[List[str]]:
    """The model IDs of a /v1/models answer, or None when it is not a model list (a /health answer, say)."""
    try:
        data = response.json().get("data")
    except Exception:
        return None
    if not isinstance(data, list):
        return None
    return [str(item.get("id")) for item in data if isinstance(item, dict) and item.get("id")]


def _from_toolkit(response: Any) -> bool:
    """Whether a /v1/models answer is another toolkit's: it says it owns every model it lists."""
    try:
        data = response.json().get("data")
    except Exception:
        return False
    return isinstance(data, list) and bool(data) and all(isinstance(m, dict) and m.get("owned_by") == "ai-toolkit" for m in data)


def _lists(listed: List[str], name: str) -> bool:
    # Ollama lists "qwen3:latest" for "qwen3".
    return name in listed or (":" not in name and f"{name}:latest" in listed)


async def _probe_url(backend: Backend, name: str, probe: Probe) -> Probe:
    import httpx

    try:
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT, auth=bearer(backend.provider)) as client:
            response = await client.get(backend.url + backend.health_path)
    except httpx.ConnectError:
        probe.detail = f"nothing answers at {without_credentials(backend.url)}"
        return probe
    except httpx.TimeoutException:
        probe.detail = f"{without_credentials(backend.url)} did not answer within {PROBE_TIMEOUT:.0f} s"
        return probe
    except httpx.HTTPError as e:
        probe.detail = f"{without_credentials(backend.url)}: {type(e).__name__}"
        return probe
    if response.status_code in (401, 403) and backend.provider:
        probe.detail = (f"{backend.provider} refused its API key" if provider_key(backend.provider)
                        else f"{backend.provider} wants an API key: add it to the provider")
        return probe
    if response.status_code != 200:
        probe.detail = f"{backend.health_path} answered {response.status_code}"
        return probe
    listed = _listed(response)
    probe.peer = _from_toolkit(response)
    if listed is None:
        probe.state, probe.detail = "ok", "answers"
    elif _lists(listed, name):
        probe.state, probe.detail = "ok", f"answers and lists {name}"
    else:
        shown = ", ".join(listed[:6]) + (", ..." if len(listed) > 6 else "")
        probe.state = "unlisted"
        probe.detail = f"answers, but does not list {name}" + (f" (it lists {shown})" if listed else " (it lists none)")
    return probe


def _probe_cmd(spec: ServerSpec, backend: Backend, probe: Probe) -> Probe:
    if spec.model_id in _sleeping:
        probe.state, probe.detail = "ok", "asleep; wakes in seconds"
        return probe
    found = shutil.which(backend.cmd[0])
    if found is None:
        probe.detail = f"{backend.cmd[0]} is not installed (not found on PATH)"
    else:
        probe.state, probe.detail = "ok", f"{found} is installed; it starts when the model loads"
    return probe


def _probe_hub(spec: ServerSpec, backend: Backend, probe: Probe) -> Probe:
    from .. import registry

    try:
        cls = registry.resolve(backend.hub, spec.task)
    except registry.UnsupportedModel as e:
        probe.detail = str(e)
        return probe
    unread = [name for name in backend.load_settings() if name not in cls.load_settings]
    if cls is ServerModel:
        probe.detail = f"{backend.hub} is a model server entry itself, not a model to run here"
    elif unread:
        probe.detail = f"{cls.__name__}, which runs {backend.hub}, does not read {', '.join(unread)}"
    else:
        probe.state, probe.detail = "ok", f"{backend.hub} runs in this process ({cls.__name__})"
    return probe


async def _probe(spec: ServerSpec, index: int) -> Probe:
    configured_backend = spec.backends[index]
    probe = Probe(index, configured_backend.label(), configured_backend.kind, "unavailable", "")
    try:
        backend = with_provider(configured_backend, _providers)
    except KeyError:
        probe.detail = f"no provider named {configured_backend.provider!r}"
        return probe
    cooling = _cooling(spec.model_id, index)
    if cooling is not None:
        ago, why = cooling
        probe.detail = f"failed to start {ago:.0f} s ago ({why}); skipped for {FAILED_COOLDOWN - ago:.0f} s more"
        return probe
    if backend.cmd:
        return _probe_cmd(spec, backend, probe)
    if backend.hub:
        return _probe_hub(spec, backend, probe)
    return await _probe_url(backend, backend.upstream_model or spec.model_id, probe)


async def probe(spec: ServerSpec) -> Tuple[List[Probe], Optional[int]]:
    """Every backend's probe, and the index of the one a load would use now (None: none can serve it)."""
    probes = list(await asyncio.gather(*(_probe(spec, i) for i in range(len(spec.backends)))))
    for state in ("ok", "unlisted"):
        chosen = next((p.index for p in probes if p.state == state), None)
        if chosen is not None:
            return probes, chosen
    return probes, None


# -------------------------------------------------------------------- model
@dataclass
class Choice:
    """The backend a model instance uses: its index in the entry, its settings (the provider's filled in),
    and for a hub backend the in-process model that runs it."""

    index: int
    backend: Backend
    inner: Optional[BaseModel] = None


class ServerModel(BaseModel):
    """A model answered by one of its backends (see the module docstring)."""

    kind = "server"

    @classmethod
    def operator_chose(cls, model_id: str) -> bool:
        return model_id in _specs

    def __init__(self, model_id: str, task: Optional[str] = None, device: str = "cpu",
                 options: Optional[Dict[str, Any]] = None):
        super().__init__(model_id, task, device, options)
        # The entry as it was when this instance was made: an edit in /admin reloads the model.
        self.spec: ServerSpec = _specs[model_id]
        self.choice: Optional[Choice] = None
        self._gone = False

    @property
    def max_concurrency(self) -> int:
        if self.choice is None:
            return 1
        return self.choice.inner.max_concurrency if self.choice.inner else self.choice.backend.max_concurrency

    @property
    def batches(self) -> bool:
        """An in-process backend's model batches as it would alone; a server batches for itself."""
        return self.choice is not None and self.choice.inner is not None and self.choice.inner.batches

    @property
    def batch_size(self) -> int:
        """The requests the Batcher gathers into one call: the in-process model's own number."""
        return getattr(self.choice.inner, "batch_size", 32) if self.choice and self.choice.inner else 1

    async def predict_batch(self, requests: List[Any]) -> List[Any]:
        """The in-process model's batch; a server's requests one by one, as before batching (POST /inference/batch)."""
        if self.choice.inner is not None:
            return await self.choice.inner.run_batch(requests)
        return [await self.predict(request) for request in requests]

    @classmethod
    def prompts(cls, model_id: str, revision: Optional[str] = None) -> Dict[str, str]:
        """The prompts of the model the entry's first hub backend runs, at its revision; none without one."""
        from .. import registry

        spec = _specs.get(model_id)
        backend = next((b for b in spec.backends if b.hub), None) if spec else None
        cls = registry.resolve(backend.hub, spec.task) if backend else None
        # An entry's hub model is never another entry (see _probe_hub), so neither are its prompts.
        return {} if cls in (None, ServerModel) else cls.prompts(backend.hub, backend.revision)

    @property
    def alive(self) -> bool:
        if self._gone:
            return False
        if self.choice is not None and self.choice.inner is not None:
            return self.choice.inner.alive
        process = getattr(self, "process", None)
        return process is None or process.returncode is None

    @classmethod
    def weights_source(cls, model_id: str) -> Optional[str]:
        """The weights of the first backend that loads files on this machine; None if all run elsewhere."""
        spec = _specs.get(model_id)
        for backend in spec.backends if spec else ():
            if backend.cmd:
                return backend.upstream_model or model_id
            if backend.hub:
                return backend.hub
        return None

    async def _chosen(self) -> Choice:
        """The backend this instance uses, chosen on first need: estimate_bytes(), which the cache calls
        before loading, so the room it makes is for this backend."""
        if self.choice is not None:
            return self.choice
        probes, index = await probe(self.spec)
        if index is None:
            reasons = "; ".join(f"{p.label}: {p.detail}" for p in probes)
            raise NoBackend(f"{self.model_id}: no backend can serve it now ({reasons})", FAILED_COOLDOWN / 2)
        for p in probes[:index]:
            logger.info("%s: skipping %s (%s)", self.model_id, p.label, p.detail)
        backend = with_provider(self.spec.backends[index], _providers)
        inner = None
        if backend.hub:
            from .. import registry

            options = {**self.options, **backend.load_settings()}
            inner = registry.resolve(backend.hub, self.task)(backend.hub, self.task, self.device, options)
            self.kind = "in-process"
        elif probes[index].state == "unlisted":
            logger.warning("%s: %s %s; using it, as no other backend can serve it",
                           self.model_id, probes[index].label, probes[index].detail)
        self.choice = Choice(index, backend, inner)
        self.backend = self._first_hop = f"{backend.label()}: {backend.hub or backend.upstream_model or self.model_id}"
        self.peer = probes[index].peer
        self.upstream = None if backend.cmd or backend.hub else backend.provider or urlsplit(backend.url).netloc.rpartition("@")[2]
        return self.choice

    async def estimate_bytes(self) -> Optional[int]:
        choice = await self._chosen()
        backend = choice.backend
        if backend.memory_gb:
            return int(backend.memory_gb * memory.GB)
        if choice.inner is not None:
            return await choice.inner.estimate_bytes()
        if not backend.cmd:
            return 0  # runs elsewhere; uses none of this machine's memory
        return await asyncio.to_thread(memory.estimate_bytes, backend.upstream_model or self.model_id)

    # ------------------------------------------------------------ lifecycle
    async def load(self) -> None:
        choice = await self._chosen()
        try:
            if choice.inner is not None:
                await choice.inner.start()
            else:
                await self._connect(choice.backend)
        except Exception as e:
            others = [i for i in range(len(self.spec.backends))
                      if i != choice.index and _cooling(self.model_id, i) is None]
            if not others:
                raise
            _failed[(self.model_id, choice.index)] = (time.monotonic(), str(e)[:300])
            raise TryNextBackend(f"{self.model_id}: {choice.backend.label()} failed ({e}); trying the next backend") from e
        logger.info("%s is served by %s", self.model_id, self.backend)

    async def _connect(self, backend: Backend) -> None:
        process = None
        if backend.cmd:
            process, base_url = (
                await processes.wake(self.model_id, backend) or await processes.spawn(self.model_id, backend)
            )
        else:
            base_url = backend.url
        # Set before the readiness wait, so a failed start still stops it (see unload()).
        self.process = process
        self.client = OpenAIClient(base_url, self._upstream, backend.extra, backend.request_timeout,
                                   auth=bearer(backend.provider))
        if backend.cmd:
            # A server elsewhere already answered its probe.
            await processes.wait_ready(self.model_id, self.client.http, backend, process)

    async def unload(self) -> None:
        choice = self.choice
        if choice is not None and choice.inner is not None:
            await choice.inner.stop()
            return
        client = getattr(self, "client", None)
        if client is not None:
            if choice is not None and choice.backend.on_evict == "unload" and self.is_loaded and not self._gone:
                await processes.unload_upstream(client.http, choice.backend.label(), self._upstream)
            await client.aclose()
        process = getattr(self, "process", None)
        if process is None:
            return
        # Only a server that started properly sleeps; one that failed to start is stopped.
        asleep = (
            choice is not None and choice.backend.on_evict == "sleep" and self.is_loaded
            and process.returncode is None and await processes.sleep(self.model_id, process, client.base_url)
        )
        if not asleep:
            await processes.stop(process)

    # -------------------------------------------------------------- predict
    async def predict(self, request: Any, on_text: Optional[Callable[[str], Any]] = None) -> Any:
        if self.choice.inner is not None:
            return await self.choice.inner.run(request, on_text)
        import httpx

        try:
            if self.task == "embedding":
                result = await self.client.embed(request)
            elif self.task == "rerank":
                result = await self.client.rerank(request)
            elif self.task == "speech-to-text":
                result = await self.client.transcribe(request)
            else:
                result = await self.client.chat(request, on_text)
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            # Nothing reached the server: it went away (Ollama quit, say). The cache drops this
            # instance and the manager retries the request once, on the backend chosen next.
            self._gone = True
            raise TryNextBackend(f"{self.model_id}: {self.choice.backend.label()} stopped answering ({e})") from e
        # Another toolkit says which of its backends answered: ours goes on to it (none: a model of its own).
        hop = self.client.toolkit
        if hop is not None:
            self.peer = True
            self.backend = f"{self._first_hop} → {hop.get('backend') or 'in-process'}"
        return result

    @property
    def _upstream(self) -> str:
        return self.choice.backend.upstream_model or self.model_id
