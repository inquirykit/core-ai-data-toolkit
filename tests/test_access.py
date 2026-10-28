"""Every route needs a key, but those the public routers declare: a route added without one fails here."""

from fastapi.routing import iter_route_contexts

from api import deps, public_routes, setup_routes
from api.app import app

CLIENT_GUARDS = {deps.require_api_key}
ADMIN_GUARDS = {deps.require_admin_key}
# Paths whose every route (the public ones aside) is the admin's.
ADMIN_PREFIXES = ("/admin", "/jobs", "/hub")


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


def test_admin_paths_need_the_admin_key():
    weaker = sorted(
        name for level, name, endpoint in routes_under(ADMIN_PREFIXES) if level != "admin" and endpoint not in PUBLIC
    )
    assert weaker == [], f"admin routes a client key opens: {weaker}"


def routes_under(prefixes):
    return [r for r in _routes() if r[1].split(" ", 1)[1].startswith(prefixes)]
