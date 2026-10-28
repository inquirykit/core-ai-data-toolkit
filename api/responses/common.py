"""What every response module builds on: ``Out`` (open at run time, closed in the schema), ``answering``."""

from typing import Any, Dict, Literal

from pydantic import BaseModel, ConfigDict

Json = Dict[str, Any]


def _closed(schema: Dict[str, Any]) -> None:
    # Extra fields pass through at run time, but the schema lists only the declared ones, so the generated
    # types stay exact (no catch-all index signature hiding a misspelt field).
    schema.pop("additionalProperties", None)


# What every response model is configured with: extra fields kept, the schema closed.
OPEN = ConfigDict(extra="allow", json_schema_extra=_closed)


class Out(BaseModel):
    model_config = OPEN


def answering(model: type) -> Dict[str, Any]:
    """A route's response options: ``model`` for the schema and the check, and only the fields the route set
    (a field it leaves out stays out, instead of coming back as its default)."""
    return {"response_model": model, "response_model_exclude_unset": True}


class Deleted(Out):
    """What a DELETE that answers with a body says: the id of what it deleted."""
    deleted: str


# How the cache knows a model: what the model browser and GET /v1/models show.
ModelStatus = Literal["loaded", "loading", "unloaded"]


class ErrorDetail(Out):
    """What every error answers with (api/app.py): the message; under /evidence also as ``error``."""
    detail: str
