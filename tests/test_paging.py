"""A route that lists rows of a growing table pages them (AGENTS.md, Data): a GET whose answer holds a list takes
``limit`` and ``cursor`` (/db's ``_limit`` and ``_cursor``), or is named below, with why its list stays small, or as one
still to page. A list route added without paging fails here."""

from api.app import app

# Lists that stay small whatever the data, or that a range or a tail bounds (``since``/``until``, ``after_id`` and a
# ``limit``): answered whole.
SMALL = {
    # The service's own settings, specs and registries: as many as an admin makes, or the code declares.
    "/admin/config", "/admin/evals/metrics", "/admin/model-servers", "/admin/traces/{trace_id}", "/enrichers",
    "/enrichers/{name}", "/jobs/types", "/jobs/schedules", "/queue/webhooks", "/models/catalog", "/models/estimate",
    "/models/types", "/v1/models", "/remotes", "/workflows/nodes", "/workflows/{id}", "/workflows/runs/{run_id}",
    "/admin/datasets",
    # A workspace's or a table's schema, a dashboard's items, a file's columns and first rows.
    "/db/{workspace}", "/db/{workspace}/_edges", "/db/{workspace}/{table}/_schema", "/db/{workspace}/_dashboards/{name}",
    "/jobs/data/{path}/summary", "/playground/conversations/{conversation_id}",
    # Bounded by a limit, a range or a tail.
    "/admin/datasets/{dataset_id}/to-label", "/admin/events", "/admin/logs", "/admin/requests", "/admin/stats",
    "/admin/timeline", "/db/{workspace}/_graph", "/db/{workspace}/{table}/{row_id}/_traverse",
    # The browser extension reads these whole (its contract): a case's notes and to-dos are capped as written.
    "/evidence/cases", "/evidence/cases/{case_id}/annotations", "/evidence/cases/{case_id}/todos", "/evidence/verify",
}
# Lists that grow with use and are answered whole (or cut at a cap without a ``next``): to page, then take off here.
NOT_PAGED_YET = {
    "/admin/datasets/{dataset_id}",  # its run_list: every run of the dataset
    "/admin/runs/{run_id}",  # its items: one per example
    "/admin/runs/compare/{run_a}/{run_b}",  # one row per example
    "/evidence/cases/{case_id}/captures",
    "/jobs/data", "/jobs/{id}/files",  # cut at 2,000 files
    "/playground/conversations", "/playground/presets",  # cut at 1,000
    "/remotes/{name}",  # its missing files
}


def _lists():
    """Each GET route whose answer is a list or holds one at its top, and whether it pages."""
    schema = app.openapi()
    components = schema["components"]["schemas"]

    def resolved(s):
        while isinstance(s, dict) and "$ref" in s:
            s = components[s["$ref"].rsplit("/", 1)[-1]]
        return s if isinstance(s, dict) else {}

    def is_list(s):
        s = resolved(s)
        return s.get("type") == "array" or any(resolved(x).get("type") == "array" for x in s.get("anyOf", []))

    for path, item in schema["paths"].items():
        op = item.get("get")
        answer = (op or {}).get("responses", {}).get("200", {}).get("content", {}).get("application/json", {})
        body = resolved(answer.get("schema"))
        if not body or not (is_list(body) or any(is_list(p) for p in body.get("properties", {}).values())):
            continue
        params = {p["name"] for p in op.get("parameters", [])}
        yield path, {"limit", "cursor"} <= params or {"_limit", "_cursor"} <= params


def test_a_route_that_lists_pages_or_says_why_not():
    unpaged = {path for path, pages in _lists() if not pages}
    assert sorted(unpaged - SMALL - NOT_PAGED_YET) == [], "page it (limit, cursor, next), or name it in SMALL with why"
    assert sorted((SMALL | NOT_PAGED_YET) - unpaged) == [], "named here but paged now (or gone): take it off"
    assert not SMALL & NOT_PAGED_YET
