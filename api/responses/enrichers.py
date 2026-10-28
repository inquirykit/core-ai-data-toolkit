"""/enrich and /enrichers."""

from typing import Any, Dict, List, Literal, Optional

from pydantic import ConfigDict

from enrichers.spec import EnricherSpec, EntityType, Terms
from .common import OPEN, Json, Out


class EnricherState(Out):
    requests_in_window: int
    limit: Optional[int]
    cooldown_until: Optional[float]


class EnricherInfo(EnricherSpec):
    """One enricher with what decides whether it runs: GET /enrichers lists these."""
    # Every field of the spec is in the answer, defaults included: required in the schema.
    model_config = ConfigDict(**OPEN, json_schema_serialization_defaults_required=True)

    # Shipped with the service
    builtin: bool
    # A saved file replaces the built-in version
    overridden: bool
    # The env var has a value
    secret_set: bool
    # Its terms are in ENRICHER_TERMS
    allowed: bool
    # enabled, allowed, and its key set unless optional
    usable: bool
    state: EnricherState


class EnricherList(Out):
    enrichers: List[EnricherInfo]
    terms_allowed: List[Terms]
    types: List[EntityType]


EnrichStatus = Literal["ok", "not_found", "error", "no_key", "disabled", "not_allowed", "rate_limited"]


class EnrichResult(Out):
    enricher: str
    status: EnrichStatus
    outputs: Json
    # The service's reply, null when none
    raw: Any
    cached: bool
    observed_at: Optional[float]
    expires_at: Optional[float]
    ms: Optional[int]
    http_status: Optional[int]
    error: Optional[str]
    terms: Terms
    attribution: Optional[str]


class EnrichResponse(Out):
    # Normalised
    value: str
    type: EntityType
    results: List[EnrichResult]
    # First ok value per output name
    merged: Json
    # Output name -> the enricher it came from
    sources: Dict[str, str]


class SentRequest(Out):
    method: str
    url: str
    headers: Dict[str, str]


class SecretSaved(Out):
    # The variable the API key is kept in, and whether it holds one now
    secret: str
    secret_set: bool


class EnricherTest(Out):
    value: str
    type: EntityType
    result: EnrichResult
    # The resolved request, the secret already masked
    request: Optional[SentRequest]
