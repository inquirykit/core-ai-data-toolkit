"""
Which requests the audit log (monitoring/audit.py) keeps: every change through
a route that needs the admin key, or through the setup wizard, and every
delete, whatever key it takes. A change is a POST, PUT, PATCH or DELETE; the
few admin POSTs that only read or try something (a query, a probe, a test)
are marked ``reads`` where they are declared, and left out. A route's level is
its key check (api/deps.py), the one
tests/test_access.py holds every route to, so a route added later is audited
by the rule without being named here.

``record`` hands one answered request to the log, if it is one of those; the
middleware in api/app.py calls it for every request.
"""

import time
from typing import FrozenSet, Optional

from fastapi import Request

from monitoring import AuditLog

from . import deps, setup_routes

WRITES = frozenset({"POST", "PUT", "PATCH", "DELETE"})
SETUP = frozenset(route.endpoint for route in setup_routes.router.routes)


def reads(endpoint):
    """Mark a route's function, under its ``@router.post``: a POST that changes nothing, which the log leaves out."""
    endpoint.audit_reads = True
    return endpoint


def _guards(dependant, found: set) -> set:
    for sub in dependant.dependencies:
        found.add(sub.call)
        _guards(sub, found)
    return found


def audited(route, method: str) -> bool:
    """Whether a request to ``route`` (the app's matched route) with ``method`` goes in the audit log."""
    endpoint = getattr(route, "endpoint", None)
    if method not in WRITES or getattr(endpoint, "audit_reads", False):
        return False
    if method == "DELETE" or endpoint in SETUP:
        return True
    dependant = getattr(route, "dependant", None)
    return dependant is not None and deps.require_admin_key in _guards(dependant, set())


def record(log: Optional[AuditLog], request: Request, status: int, started: float) -> None:
    """Put the answered request in the log when it is one it keeps (``audited``); ``started`` is its
    time.time() when it came in."""
    route = request.scope.get("route")
    if log is None or route is None or not audited(route, request.method):
        return
    query = request.url.query
    who = deps.caller(request)
    log.record(
        ts=started, method=request.method, route=route.path, path=request.url.path + (f"?{query}" if query else ""),
        status=status, duration_ms=(time.time() - started) * 1000,
        actor=who.level, key_id=who.key,
        ip=request.client.host if request.client else None, forwarded=request.headers.get("x-forwarded-for"),
        agent=request.headers.get("user-agent"),
    )


def routes(app) -> FrozenSet[str]:
    """Every "METHOD route" of ``app`` the log keeps (the tests check the list)."""
    from fastapi.routing import iter_route_contexts

    found = set()
    for ctx in iter_route_contexts(app.routes):
        if getattr(ctx, "dependant", None) is None:
            continue
        found.update(f"{method} {ctx.path}" for method in ctx.methods or () if audited(ctx, method))
    return frozenset(found)
