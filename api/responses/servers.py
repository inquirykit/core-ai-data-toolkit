"""/admin/model-servers: entries, providers and probes."""

from typing import List, Literal, Optional

from pydantic import ConfigDict

from models.server_specs import ModelEntry, Provider
from .common import OPEN, Out


# Where an entry comes from: MODEL_SERVERS_FILE, the file /admin saves to, or saved over the file's.
Origin = Literal["file", "saved", "replaced"]


class ProviderInfo(Provider):
    """A provider in effect: GET /admin/model-servers lists these."""
    # Every field of the provider is in the answer, defaults included: required in the schema.
    model_config = ConfigDict(**OPEN, json_schema_serialization_defaults_required=True)

    name: str
    origin: Origin
    # The models with a backend on it
    used_by: List[str]
    # The file's version, when a saved one replaces it
    file_provider: Optional[Provider]


class ModelServerInfo(ModelEntry):
    """A model server entry in effect. Its backends hold what was given, so a provider's settings stay inherited."""
    model_config = ConfigDict(**OPEN, protected_namespaces=())

    model_id: str
    origin: Origin
    # The file's version, when a saved one replaces it
    file_entry: Optional[ModelEntry]
    # "loaded", "loading" or "unloaded"
    status: str
    # The backend that answers now, when loaded, e.g. "ollama: qwen3.5:4b"
    serving: Optional[str]


class ModelServerProblem(Out):
    # The file and entry, e.g. "file: models.qwen3.5-4b"
    where: str
    message: str


class ModelServers(Out):
    # MODEL_SERVERS_FILE, and whether it exists
    file: str
    file_exists: bool
    # MODEL_SERVERS_SAVED_FILE; None: nothing can be saved from /admin
    saved_file: Optional[str]
    editable: bool
    # MODEL_SERVERS_ALLOW_COMMANDS: /admin may add cmd and env
    commands_allowed: bool
    tasks: List[str]
    providers: List[ProviderInfo]
    models: List[ModelServerInfo]
    # Entries that could not be read, and backends on missing providers
    problems: List[ModelServerProblem]
    # What a change did, e.g. which models it unloaded
    notes: List[str]


class BackendProbe(Out):
    index: int
    label: str
    kind: Literal["url", "cmd", "hub"]
    # "unlisted": answers without listing the model, so used only when nothing else can serve it
    state: Literal["ok", "unlisted", "unavailable"]
    detail: str


class ModelServerProbe(Out):
    model_config = ConfigDict(**OPEN, protected_namespaces=())

    model_id: str
    # The backend a load would use now; None: none can serve the model
    chosen: Optional[int]
    backends: List[BackendProbe]
