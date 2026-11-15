"""/admin/keys: the client keys an admin makes, expires and revokes (api/key_routes.py)."""

from typing import List, Optional

from access.grants import Grants
from access.keys import State

from .common import Out

KeyState = State


class ClientKeyInfo(Out):
    """A client key as the list shows it. Never its token, which only the reply that makes it holds."""
    # The 8 characters after aitk_ in its token, which the audit log names too
    id: str
    name: str
    # Unix times; expires_at null for a key that never expires, revoked_at while it is not revoked
    created_at: float
    expires_at: Optional[float]
    revoked_at: Optional[float]
    # Null until it opens a request (written each minute)
    last_used_at: Optional[float]
    state: KeyState
    # What it may reach (access/grants.py)
    grants: Grants


class ClientKeyMade(ClientKeyInfo):
    # The key, the only time it is shown: give it to the machine, as X-API-Key or Authorization: Bearer
    token: str


class ClientKeyList(Out):
    """A page of the client keys, newest first; ``next`` is the next page's cursor, absent on the last."""
    keys: List[ClientKeyInfo]
    next: Optional[str] = None
    # Whether the client routes need a key now: API_KEY is set, or a client key was ever made
    clients_need_a_key: bool
