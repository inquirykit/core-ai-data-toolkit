"""Every route needs a key, but those the public routers declare: a route added without one fails here. A client key
opens only the routes listed below, so a route added for clients is a choice made here, and one added for the admin
that a client key would open fails."""

import re

import pytest
from fastapi.routing import iter_route_contexts
from fastapi.testclient import TestClient

from api import deps, public_routes, setup_routes
from api.app import app
from core.config import Config

CLIENT_GUARDS = {deps.require_api_key}
ADMIN_GUARDS = {deps.require_admin_key}
# What a client key opens: the models' and the data's routes for apps, and nothing of the service's own (its jobs,
# queue, settings, the workflows' and views' specs). Running a view or a workflow is the client's; writing one is not.
CLIENT_ROUTES = {
    # The models
    "POST /inference", "POST /inference/batch", "POST /vision-language", "GET /tasks",
    "GET /models/catalog", "GET /models/list", "GET /models/types", "GET /metrics",
    "POST /v1/audio/transcriptions", "POST /v1/chat/completions", "POST /v1/completions", "POST /v1/decisions",
    "POST /v1/embeddings", "GET /v1/models", "POST /v1/rerank",
    # The data: workspaces' tables, edges and files (a kept one needs the admin key: admin_for_kept), views run
    "GET /db", "GET /db/{workspace}", "GET /db/{workspace}/_edges", "PUT /db/{workspace}/_edges",
    "POST /db/{workspace}/_edges/_delete", "GET /db/{workspace}/_graph", "POST /db/{workspace}/_transaction",
    "POST /db/{workspace}/_views/{name}",
    "POST /db/{workspace}/{bucket}/_files", "DELETE /db/{workspace}/{bucket}/_files/{file_id}",
    "GET /db/{workspace}/{bucket}/_files/{file_id}",
    "GET /db/{workspace}/{table}", "POST /db/{workspace}/{table}", "PUT /db/{workspace}/{table}/_bulk",
    "POST /db/{workspace}/{table}/_bulk-delete", "POST /db/{workspace}/{table}/_bulk-get",
    "GET /db/{workspace}/{table}/_count", "GET /db/{workspace}/{table}/_schema", "POST /db/{workspace}/{table}/_search",
    "DELETE /db/{workspace}/{table}/{row_id}", "GET /db/{workspace}/{table}/{row_id}",
    "PATCH /db/{workspace}/{table}/{row_id}", "PUT /db/{workspace}/{table}/{row_id}",
    "GET /db/{workspace}/{table}/{row_id}/_edges", "GET /db/{workspace}/{table}/{row_id}/_neighbors",
    "GET /db/{workspace}/{table}/{row_id}/_traverse",
    # Enrichment, and a workflow invoked on rows
    "POST /enrich", "GET /enrichers", "GET /enrichers/{name}", "POST /workflows/{id}/invoke",
    # Evidence capture (the browser extension)
    "POST /evidence/captures", "GET /evidence/captures/{capture_id}", "PUT /evidence/captures/{capture_id}/annotations",
    "GET /evidence/captures/{capture_id}/artifacts/{name}", "GET /evidence/cases", "POST /evidence/cases",
    "GET /evidence/cases/{case_id}/annotations", "GET /evidence/cases/{case_id}/captures",
    "GET /evidence/cases/{case_id}/todos", "PUT /evidence/cases/{case_id}/todos", "GET /evidence/public-key",
    "GET /evidence/search", "GET /evidence/verify",
}


def _guards(dependant, found):
    for sub in dependant.dependencies:
        found.add(sub.call)
        _guards(sub, found)
    return found


def _routes():
    """(level, "METHOD path", endpoint) for every API route as the app serves it, router dependencies included."""
    for ctx in iter_route_contexts(app.routes):
        dependant = getattr(ctx, "dependant", None)
        if dependant is None:  # the docs, the schema, the static files
            continue
        guards = _guards(dependant, set())
        level = "admin" if guards & ADMIN_GUARDS else "client" if guards & CLIENT_GUARDS else "public"
        for method in sorted(ctx.methods or []):
            yield level, f"{method} {ctx.path}", ctx.endpoint


PUBLIC = {route.endpoint for router in (public_routes.router, setup_routes.router) for route in router.routes}


def test_only_the_public_routers_routes_need_no_key():
    routes = list(_routes())
    assert len(routes) > 100
    open_ = sorted(name for level, name, endpoint in routes if level == "public" and endpoint not in PUBLIC)
    assert open_ == [], f"routes without a key check (add deps.CLIENT or deps.ADMIN to their router): {open_}"
    guarded = sorted(name for level, name, endpoint in routes if level != "public" and endpoint in PUBLIC)
    assert guarded == [], f"public routes that ask for a key: {guarded}"


def test_a_client_key_opens_only_the_client_routes():
    client = {name for level, name, _ in _routes() if level == "client"}
    assert sorted(client - CLIENT_ROUTES) == [], "routes a client key opens: list them here, or give them deps.ADMIN"
    assert sorted(CLIENT_ROUTES - client) == [], "listed client routes that are no longer the client's"


def test_no_route_takes_both_keys():
    """A router states its key once and its routes add none (api/deps.py): a route of a client router that asked for
    the admin key too would show both in the schema, where either is taken."""
    both = sorted(name for name, guards in _guarded() if guards & CLIENT_GUARDS and guards & ADMIN_GUARDS)
    assert both == [], f"routes under both keys (move them to the module's admin_router): {both}"


@pytest.fixture
def keys(monkeypatch):
    monkeypatch.setattr(Config, "API_KEY", "client-key-for-the-routing-test")
    monkeypatch.setattr(Config, "ADMIN_API_KEY", "admin-key-for-the-routing-test")


def test_every_route_is_reached_by_its_own_path(keys):
    """Each route's path, with a value for each parameter, goes to that route, and not to one of the same method
    included before it whose parameter takes the same path (``/db/{workspace}/{table}`` would take
    ``/db/{workspace}/_query``). Sent with a wrong key, every guarded route stops at its key check, before its work."""
    reached = {}

    async def noting(scope, receive, send):
        await app(scope, receive, send)
        if scope["type"] == "http":
            reached["route"] = getattr(scope.get("route"), "path", None)

    client = TestClient(noting, raise_server_exceptions=False)
    wrong = []
    for level, name, _ in _routes():
        if level == "public":  # their work would run: they are few, and reached by their own tests
            continue
        method, path = name.split(" ", 1)
        reached.clear()
        assert client.request(method, re.sub(r"\{[^}]+\}", "x1", path), headers={"X-API-Key": "wrong"}).status_code == 401
        if reached.get("route") != path:
            wrong.append(f"{name} -> {reached.get('route')}")
    assert wrong == [], f"paths that go to another route (include its router earlier, api/app.py): {wrong}"


def _guarded():
    for ctx in iter_route_contexts(app.routes):
        dependant = getattr(ctx, "dependant", None)
        if dependant is not None:
            for method in sorted(ctx.methods or []):
                yield f"{method} {ctx.path}", _guards(dependant, set())
