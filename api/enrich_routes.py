"""
/enrich and /enrichers: look a value up in outside services (see enrichers/).

    POST /enrich                    {value, type?, enrichers?, refresh?}  every enricher for the value's type
    GET  /enrichers                 the enrichers, with whether each can run
    GET  /enrichers/{name}
    PUT  /enrichers/{name}          create or replace one (admin)
    DELETE /enrichers/{name}        remove it, or a built-in's saved changes (admin)
    PUT  /enrichers/{name}/secret   {value}: save its API key, encrypted (admin)
    POST /enrichers/{name}/test     {value, type?}: run it now, without the cache (admin)
    DELETE /enrichers/cache         forget cached results (admin)

Looking up needs the client key; changing enrichers and their keys the admin
key. The dashboard's Enrichers section is a client of these routes.
"""

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Response
from pydantic import BaseModel, ConfigDict, Field

from enrichers import EnricherSpec, EnrichService, EntityType
from enrichers.spec import NAME
from core.errors import NotFound, Refused
from . import audit, deps
from . import responses
from .settings import SecretRequest, check_secret_name, save_secret

router = APIRouter(tags=["enrichers"], dependencies=deps.CLIENT)
# This module's routes that only the admin's key opens.
admin_router = APIRouter(tags=["enrichers"], dependencies=deps.ADMIN)

MAX_VALUE_CHARS = 2048


class EnrichRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str = Field(..., min_length=1, max_length=MAX_VALUE_CHARS, description="An IP, domain, URL, hash, email, CVE...")
    type: Optional[EntityType] = Field(None, description="Its entity type; detected when left out")
    enrichers: Optional[List[str]] = Field(None, max_length=100, description="Only these enrichers; all of the type's when left out")
    refresh: bool = Field(False, description="Ignore cached results")


class TestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str = Field(..., min_length=1, max_length=MAX_VALUE_CHARS)
    type: Optional[EntityType] = None


async def _entry(service: EnrichService, name: str):
    entry = await service.store.get(name)
    if entry is None:
        raise NotFound(f"No enricher named {name!r}")
    return entry


@router.post("/enrich", **responses.answering(responses.EnrichResponse))
async def enrich(service: deps.Enrichers, body: EnrichRequest) -> Dict[str, Any]:
    """Run every enricher that takes the value's type and merge what they found."""
    if body.enrichers is not None:
        known = await service.store.all()
        for name in body.enrichers:
            if name not in known:
                raise NotFound(f"No enricher named {name!r}")
    return await service.enrich(body.value, body.type, body.enrichers, body.refresh)


@router.get("/enrichers", **responses.answering(responses.EnricherList))
async def list_enrichers(service: deps.Enrichers) -> Dict[str, Any]:
    """Every enricher: its definition, where it came from, and whether it can run now."""
    return {
        "enrichers": [service.describe(e) for e in (await service.store.all()).values()],
        "terms_allowed": service.terms_allowed,
        "types": [t.value for t in EntityType],
    }


@admin_router.delete("/enrichers/cache", status_code=204, response_class=Response)
async def forget_cache(service: deps.Enrichers, enricher: Optional[str] = None):
    """Drop cached results, all of them or one enricher's."""
    await service.cache.forget(enricher)


@router.get("/enrichers/{name}", **responses.answering(responses.EnricherInfo))
async def get_enricher(service: deps.Enrichers, name: str) -> Dict[str, Any]:
    """One enricher, as GET /enrichers lists it."""
    return service.describe(await _entry(service, name))


@admin_router.put("/enrichers/{name}", **responses.answering(responses.EnricherInfo))
async def put_enricher(service: deps.Enrichers, name: str, spec: EnricherSpec) -> Dict[str, Any]:
    """Create or replace an enricher. A built-in's name keeps the built-in, changed. 400 for a ``secret`` that is one of
    the service's settings, a provider's key or a variable its child processes get."""
    if not NAME.match(name):
        raise Refused("Names are lower case letters, digits and _")
    if spec.name != name:
        raise Refused(f"The body names {spec.name!r}, the path {name!r}")
    if spec.secret:
        check_secret_name(spec.secret)
    entry = await service.store.save(spec)
    # The definition changed, so its cached results no longer describe it.
    await service.cache.forget(name)
    return service.describe(entry)


@admin_router.delete("/enrichers/{name}", status_code=204, response_class=Response)
async def delete_enricher(service: deps.Enrichers, name: str):
    """Remove an enricher, or the saved changes of a built-in one (which comes back as shipped)."""
    entry = await _entry(service, name)
    if entry.builtin and not entry.overridden:
        raise Refused("A built-in enricher cannot be deleted; disable it instead")
    await service.store.delete(name)
    await service.cache.forget(name)


@admin_router.put("/enrichers/{name}/secret", **responses.answering(responses.SecretSaved))
async def put_secret(service: deps.Enrichers, keys: deps.Keys, name: str, body: SecretRequest) -> Dict[str, Any]:
    """Save the enricher's API key, encrypted; it takes effect at once. 400 when the enricher names a setting of
    the service, a provider's key or a variable its child processes get, 503 without a master key
    (SECRETS_MASTER_KEY)."""
    entry = await _entry(service, name)
    variable = entry.spec.secret
    if not variable:
        raise Refused(f"{name} names no secret")
    # Checked again here: a built-in's may name one, or it was saved before the check.
    check_secret_name(variable)
    value = body.value.strip()
    await save_secret(keys, variable, value)
    return {"secret": variable, "secret_set": bool(value)}


@admin_router.post("/enrichers/{name}/test", **responses.answering(responses.EnricherTest))
@audit.reads
async def test_enricher(service: deps.Enrichers, name: str, body: TestRequest) -> Dict[str, Any]:
    """Run the enricher on a value now: no cache, even when disabled. Shows the request it sent, its key masked."""
    entry = await _entry(service, name)
    type_, value = service.resolve_value(body.value, body.type)
    if type_ not in entry.spec.inputs:
        raise Refused(f"{name} takes {', '.join(t.value for t in entry.spec.inputs)}, and {value!r} is {type_.value}",
        )
    sent = service.masked_request(entry.spec, type_, value)
    result = await service.run_one(entry, type_, value, force=True)
    return {"value": value, "type": type_.value, "result": result, "request": sent}
