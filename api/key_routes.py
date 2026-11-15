"""
/admin/keys: the client keys (access/keys.py), one for each server or machine that calls the service. A client key
opens the client routes, never the admin's, which stay with ADMIN_API_KEY; its grants (access/grants.py) narrow what it
reaches there. All of it needs the admin key; the API keys section of /admin is a client of these routes.

    GET   /admin/keys?state=&limit=&cursor=            {keys, next, clients_need_a_key}: newest first, never a token
    POST  /admin/keys  {name, expires_in_days, grants}  the new key with its token, the only time it is shown
    PATCH /admin/keys/{id}  {grants}                    the key, with these grants from its next request on
    POST  /admin/keys/{id}/revoke                       the key, revoked from its next request on
"""

from typing import Annotated, Optional

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field, StringConstraints

from access import Grants
from core.config import Config
from core.errors import Conflict
from db.tables import decode_cursor, encode_cursor
from . import deps, responses

router = APIRouter(prefix="/admin/keys", tags=["keys"], dependencies=deps.ADMIN)

MAX_LIMIT = 200


class NewKey(BaseModel):
    model_config = {"extra": "forbid"}

    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)] = Field(
        ..., description="What the key is for: the machine that holds it")
    expires_in_days: Optional[float] = Field(None, gt=0, le=36500, description="Unset: the key never expires")
    grants: Grants = Field(default_factory=Grants, description="Unset: every table and model a client key reaches, "
                                                               "and no managing of models")


class KeyChange(BaseModel):
    model_config = {"extra": "forbid"}

    grants: Grants


@router.get("", **responses.answering(responses.ClientKeyList))
async def list_keys(
    request: Request,
    keys: deps.ClientKeys,
    state: Optional[responses.KeyState] = Query(None, description="Only the keys in this state"),
    limit: int = Query(100, ge=1, le=MAX_LIMIT),
    cursor: deps.Cursor = None,
):
    """A page of the client keys, newest first, each with its state (live, expired or revoked) and when it was last
    used; never a token. ``clients_need_a_key`` says whether the client routes need a key now: API_KEY is set, or a
    client key was ever made, so revoking every one never opens them again."""
    # By the order they were made, not an offset, so a key made meanwhile does not shift the pages.
    listed = await keys.listed(state, decode_cursor(cursor, "s") if cursor else None)
    shown = listed[:limit]
    after = encode_cursor(shown[-1].seq, "s") if len(listed) > limit else None
    return {**responses.page("keys", [k.public() for k in shown], after),
            "clients_need_a_key": deps.clients_need_a_key(request)}


@router.post("", status_code=201, **responses.answering(responses.ClientKeyMade))
async def make_key(keys: deps.ClientKeys, body: NewKey):
    """Make a client key. The reply holds its token, ``aitk_<id>_<secret>``, the only time it is shown: only its hash
    is kept. From the first key made, the client routes need a key. 409 while ADMIN_API_KEY is not set, since client
    keys are made under the superadmin key, the one no one can revoke from here."""
    if not Config.ADMIN_API_KEY:
        raise Conflict("Set the admin key first (the setup wizard, or ADMIN_API_KEY): client keys are made under it")
    key, token = await keys.make(body.name, body.expires_in_days, body.grants)
    return {**key.public(), "token": token}


@router.patch("/{key_id}", **responses.answering(responses.ClientKeyInfo))
async def change_key(keys: deps.ClientKeys, key_id: str, body: KeyChange):
    """Replace a client key's grants: the tables and models it reaches, and whether it manages models. They hold from
    its next request on, under the same token. 404 for an unknown id."""
    return (await keys.grant(key_id, body.grants)).public()


@router.post("/{key_id}/revoke", **responses.answering(responses.ClientKeyInfo))
async def revoke_key(keys: deps.ClientKeys, key_id: str):
    """Revoke a client key: from its next request on it gets 401 "API key revoked". It stays in the list, and in the
    audit log, and cannot be made live again: make a new key. Revoking it again changes nothing. 404 for an unknown
    id."""
    return (await keys.revoke(key_id)).public()
