"""
What MODEL_SERVERS_FILE and /admin say about model servers: providers, models and their backends.

A model ID is served by one or more backends, tried in order when the model
loads (see models/model_server.py). Each backend says where with one of:

- ``provider``: a server named once under [providers], such as Ollama or LM Studio;
- ``url``: a server that already runs elsewhere;
- ``cmd``: a server this service starts, ``{port}`` replaced by a free port;
- ``hub``: a Hugging Face model run in this process by the model file that serves it.

A model without ``backends`` is its own one backend: the flat form, with
``cmd`` or ``url`` beside ``task``, as the file has always taken.

The TOML file (MODEL_SERVERS_FILE) is the operator's. Entries saved from
/admin go to a JSON file of the same shape (MODEL_SERVERS_SAVED_FILE); one
saved there replaces the file's entry of the same name, and deleting it
brings the file's back, as with enrichers. Commands run on this host, so
/admin may only save the ``cmd`` and ``env`` the file already gives the same
model, unless MODEL_SERVERS_ALLOW_COMMANDS is on (see ``new_commands``).
"""

import json
import os
import re
import shlex
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

TASKS = ("text-generation", "vision-language", "embedding", "rerank", "speech-to-text")
Task = Literal["text-generation", "vision-language", "embedding", "rerank", "speech-to-text"]

PROVIDER_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,62}$")
MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}$")

FROZEN = ConfigDict(extra="forbid", frozen=True)
# The keys that say where a backend is; exactly one is given.
PLACES = ("provider", "url", "cmd", "hub")
# What a backend takes from its provider unless it sets them itself.
INHERITED = ("url", "health_path", "max_concurrency", "request_timeout", "on_evict")


def _base_url(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    if not re.match(r"^https?://[^\s/]+", value):
        raise ValueError("url starts with http:// or https:// and a host")
    return value.rstrip("/")


class Provider(BaseModel):
    """A server that runs elsewhere (Ollama, LM Studio, a GPU box), named once for every backend that uses it."""

    model_config = FROZEN

    url: str = Field(..., description="Its base URL, e.g. http://127.0.0.1:11434")
    health_path: str = Field("/v1/models", pattern=r"^/", description="GET path that answers 200 when it is up")
    max_concurrency: int = Field(8, ge=1, le=1024, description="Requests sent at once")
    request_timeout: float = Field(600.0, gt=0, le=86400, description="Seconds per request")
    on_evict: Literal["stop", "unload"] = Field(
        "stop", description='"unload": ask the server to unload the model when the cache evicts it (Ollama)'
    )
    description: str = Field("", max_length=500)

    @field_validator("url")
    @classmethod
    def check_url(cls, value: str) -> str:
        return _base_url(value)


class Backend(BaseModel):
    """One way to serve a model. Exactly one of provider, url, cmd and hub says where."""

    model_config = FROZEN

    provider: Optional[str] = Field(None, description="A server named under [providers]")
    url: Optional[str] = Field(None, description="A server that already runs: its base URL")
    cmd: Tuple[str, ...] = Field((), description="A server to start here; {port} is replaced by a free port")
    hub: Optional[str] = Field(None, description="A Hub model run in this process (Transformers)")
    upstream_model: Optional[str] = Field(None, description='The "model" sent to the server (default: the model ID)')
    memory_gb: float = Field(
        0.0, ge=0, le=4096,
        description="Counted against MAX_MEMORY_GB. 0: estimated from the weights (cmd, hub), none for a server elsewhere",
    )
    max_concurrency: int = Field(8, ge=1, le=1024, description="Requests sent at once")
    startup_timeout: float = Field(600.0, gt=0, le=86400, description="Seconds a started server has to answer")
    request_timeout: float = Field(600.0, gt=0, le=86400, description="Seconds per request")
    health_path: str = Field("/v1/models", pattern=r"^/", description="GET path that answers 200 when it is up")
    on_evict: Literal["stop", "sleep", "unload"] = Field(
        "stop", description='"sleep": vLLM sleep mode (cmd); "unload": ask the server to unload the model (Ollama)'
    )
    env: Dict[str, str] = Field(default_factory=dict, description="Extra environment variables for a cmd server")
    extra: Dict[str, Any] = Field(
        default_factory=dict, description="Fields added to each chat request; the request's own win",
    )

    @field_validator("cmd", mode="before")
    @classmethod
    def split_cmd(cls, value: Any) -> Any:
        return shlex.split(value) if isinstance(value, str) else value

    @field_validator("url")
    @classmethod
    def check_url(cls, value: Optional[str]) -> Optional[str]:
        return _base_url(value)

    @model_validator(mode="after")
    def one_place(self) -> "Backend":
        given = [name for name in PLACES if getattr(self, name)]
        if len(given) != 1:
            raise ValueError("give one of provider, url, cmd or hub" + (f", not {' and '.join(given)}" if given else ""))
        if self.on_evict == "sleep" and not self.cmd:
            raise ValueError('on_evict "sleep" is for cmd backends (vLLM sleep mode)')
        if self.on_evict == "unload" and (self.cmd or self.hub):
            raise ValueError('on_evict "unload" is for provider and url backends (Ollama)')
        if self.env and not self.cmd:
            raise ValueError("env is for cmd backends")
        return self

    @property
    def kind(self) -> str:
        """"cmd", "hub" or "url" (a provider's backend is a url one)."""
        return "cmd" if self.cmd else "hub" if self.hub else "url"

    def label(self) -> str:
        """What /admin, the log and traces call it: the provider, the URL, the program, or in-process."""
        if self.provider:
            return self.provider
        if self.cmd:
            return Path(self.cmd[0]).name
        if self.hub:
            return "in-process"
        return self.url or ""


class ModelEntry(BaseModel):
    """What a file or /admin says about one model ID: its task and its backends, in the order they are tried."""

    model_config = FROZEN

    task: Task
    backends: Tuple[Backend, ...] = Field(..., min_length=1, max_length=16)
    description: str = Field("", max_length=500, description="One line the model browser shows")
    tags: Tuple[str, ...] = Field((), max_length=20, description="Labels to filter by in the model browser")


class ServerSpec(ModelEntry):
    """A model entry with its ID."""

    model_config = ConfigDict(**FROZEN, protected_namespaces=())

    model_id: str


def with_provider(backend: Backend, providers: Dict[str, Provider]) -> Backend:
    """``backend`` with its provider's settings filled in (the ones it sets win). KeyError: no such provider."""
    if not backend.provider:
        return backend
    provider = providers[backend.provider]
    inherited = {name: getattr(provider, name) for name in INHERITED if name not in backend.model_fields_set}
    return backend.model_copy(update=inherited)


# ------------------------------------------------------------------ parsing
MODEL_KEYS = {"task", "backends", "description", "tags"}
BACKEND_KEYS = set(Backend.model_fields)


def describe_error(error: ValidationError, strip: str = "") -> str:
    """A validation error in one line: where and what, for each problem (``strip``: a location prefix to drop)."""
    problems = error.errors()
    # A list whose items failed also reports itself as too short; the items' errors say why.
    if any(p["type"] != "too_short" for p in problems):
        problems = [p for p in problems if p["type"] != "too_short"]
    parts = []
    for problem in problems:
        where = ".".join(str(p) for p in problem["loc"])
        if strip and (where == strip or where.startswith(strip + ".")):
            where = where[len(strip) + 1:]
        message = problem["msg"].removeprefix("Value error, ")
        parts.append(f"{where}: {message}" if where else message)
    return "; ".join(parts)


def parse_entry(model_id: str, raw: Any) -> ServerSpec:
    """One ``[models."<id>"]`` table, with ``backends`` or flat. ValueError when it is not valid."""
    if not MODEL_ID.match(model_id):
        raise ValueError(f"{model_id}: a model ID is letters, digits and ._:/@+- (up to 200)")
    if not isinstance(raw, dict):
        raise ValueError(f"{model_id}: expected a table")
    unknown = sorted(set(raw) - MODEL_KEYS - BACKEND_KEYS)
    if unknown:
        raise ValueError(f"{model_id}: unknown keys {unknown}")
    values = dict(raw)
    flat = {key: values.pop(key) for key in list(values) if key in BACKEND_KEYS}
    if "backends" in values and flat:
        raise ValueError(f"{model_id}: {', '.join(sorted(flat))} go on each backend when backends is given")
    if "backends" not in values:
        values["backends"] = [flat]
    try:
        return ServerSpec(model_id=model_id, **values)
    except ValidationError as e:
        raise ValueError(f"{model_id}: {describe_error(e, '' if 'backends' in raw else 'backends.0')}") from None


def parse_provider(name: str, raw: Any) -> Provider:
    if not PROVIDER_NAME.match(name):
        raise ValueError(f"{name}: a provider name is lower case letters, digits, _ and - (up to 63)")
    if not isinstance(raw, dict):
        raise ValueError(f"{name}: expected a table")
    try:
        return Provider(**raw)
    except ValidationError as e:
        raise ValueError(f"{name}: {describe_error(e)}") from None


@dataclass
class Layer:
    """What one file says: its providers and models, and what it could not read (where -> why)."""

    providers: Dict[str, Provider] = field(default_factory=dict)
    models: Dict[str, ServerSpec] = field(default_factory=dict)
    problems: Dict[str, str] = field(default_factory=dict)


def parse_layer(data: Dict[str, Any]) -> Layer:
    """The providers and models of ``data`` (the files' shape); a bad entry is a problem, the rest still count."""
    layer = Layer()
    unknown = sorted(set(data) - {"providers", "models"})
    if unknown:
        layer.problems["file"] = f"unknown tables {unknown}"
    for name, raw in (data.get("providers") or {}).items():
        try:
            layer.providers[name] = parse_provider(name, raw)
        except ValueError as e:
            layer.problems[f"providers.{name}"] = str(e)
    for model_id, raw in (data.get("models") or {}).items():
        try:
            layer.models[model_id] = parse_entry(model_id, raw)
        except ValueError as e:
            layer.problems[f"models.{model_id}"] = str(e)
    return layer


def parse_specs(data: Dict[str, Any]) -> Dict[str, ServerSpec]:
    """The models of ``data``; ValueError on the first entry that is not valid."""
    layer = parse_layer(data)
    if layer.problems:
        raise ValueError(next(iter(layer.problems.values())))
    return layer.models


def dump_entry(spec: ModelEntry) -> Dict[str, Any]:
    """An entry as the saved file holds it: only what was given, so a provider's settings stay inherited."""
    return spec.model_dump(mode="json", exclude_unset=True, exclude={"model_id"})


def new_commands(spec: ModelEntry, *known: Optional[ModelEntry]) -> List[str]:
    """The commands ``spec`` would run that none of ``known`` does (with the same env): what /admin may not add
    unless MODEL_SERVERS_ALLOW_COMMANDS is on. ``known``: the file's entry and the one in effect."""
    def command(backend: Backend) -> Tuple[Tuple[str, ...], Tuple[Tuple[str, str], ...]]:
        return backend.cmd, tuple(sorted(backend.env.items()))

    seen = {command(b) for entry in known if entry is not None for b in entry.backends if b.cmd}
    return [shlex.join(b.cmd) for b in spec.backends if b.cmd and command(b) not in seen]


# -------------------------------------------------------------------- store
@dataclass
class Effective:
    """The model servers in effect: the file's, each replaced by the saved one of the same name."""

    file: Layer
    saved: Layer
    providers: Dict[str, Provider] = field(default_factory=dict)
    models: Dict[str, ServerSpec] = field(default_factory=dict)
    problems: Dict[str, str] = field(default_factory=dict)

    def __post_init__(self):
        self.providers = {**self.file.providers, **self.saved.providers}
        self.models = {**self.file.models, **self.saved.models}
        self.problems = {
            **{f"file: {k}": v for k, v in self.file.problems.items()},
            **{f"saved: {k}": v for k, v in self.saved.problems.items()},
        }
        for model_id, spec in self.models.items():
            for backend in spec.backends:
                if backend.provider and backend.provider not in self.providers:
                    self.problems[f"models.{model_id}"] = f"{model_id}: no provider named {backend.provider!r}"

    def origin(self, table: str, name: str) -> str:
        """"file", "saved", or "replaced" (saved, over the file's of the same name)."""
        in_file = name in getattr(self.file, table)
        if name in getattr(self.saved, table):
            return "replaced" if in_file else "saved"
        return "file"

    def users(self, provider: str) -> List[str]:
        """The models with a backend on ``provider``."""
        return [m for m, spec in self.models.items() if any(b.provider == provider for b in spec.backends)]


class ServerStore:
    """Reads MODEL_SERVERS_FILE and the saved file (on every call: they are small), and writes the saved one."""

    def __init__(self, file: Path, saved: Optional[Path]):
        self.file = Path(file)
        self.saved = Path(saved) if saved else None

    @classmethod
    def from_config(cls) -> "ServerStore":
        from config import Config

        return cls(Config.MODEL_SERVERS_FILE, Config.MODEL_SERVERS_SAVED_FILE)

    def read_file(self) -> Layer:
        if not self.file.is_file():
            return Layer()
        try:
            return parse_layer(tomllib.loads(self.file.read_text()))
        except (OSError, tomllib.TOMLDecodeError) as e:
            return Layer(problems={"file": f"{self.file}: {e}"})

    def _saved_raw(self) -> Dict[str, Any]:
        if self.saved is None or not self.saved.is_file():
            return {}
        data = json.loads(self.saved.read_text())
        if not isinstance(data, dict):
            raise ValueError("expected a JSON object")
        return data

    def read_saved(self) -> Layer:
        try:
            return parse_layer(self._saved_raw())
        except (OSError, ValueError) as e:
            return Layer(problems={"file": f"{self.saved}: {e}"})

    def effective(self) -> Effective:
        return Effective(self.read_file(), self.read_saved())

    # ------------------------------------------------------------ writing
    def _change(self, table: str, name: str, value: Optional[Dict[str, Any]]) -> bool:
        """Set (or with None remove) one saved entry, keeping the rest of the file as it is; True if it changed."""
        if self.saved is None:
            raise RuntimeError("MODEL_SERVERS_SAVED_FILE is empty: model servers cannot be saved from /admin")
        raw = self._saved_raw()
        entries = raw.setdefault(table, {})
        if value is None:
            if name not in entries:
                return False
            del entries[name]
        else:
            entries[name] = value
        self.saved.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.saved.with_name(self.saved.name + ".tmp")
        tmp.write_text(json.dumps(raw, indent=2) + "\n")
        os.replace(tmp, self.saved)
        return True

    def save_model(self, spec: ServerSpec) -> None:
        self._change("models", spec.model_id, dump_entry(spec))

    def delete_model(self, model_id: str) -> bool:
        return self._change("models", model_id, None)

    def save_provider(self, name: str, provider: Provider) -> None:
        self._change("providers", name, provider.model_dump(mode="json", exclude_unset=True))

    def delete_provider(self, name: str) -> bool:
        return self._change("providers", name, None)
