"""Enrichers: specs, value classification, the store, the cache, the runner and the routes.

Sources are faked with httpx.MockTransport, so nothing leaves the machine.
"""

import asyncio
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from enrichers import EnricherSpec, EnrichService, EnricherStore, EntityType, MemoryCache, ResultCache, classify
from enrichers.spec import apply_filters, map_outputs, parse_rate, parse_ttl
from enrichers.store import Entry


def spec(name="geo", **changes):
    base = {
        "name": name,
        "inputs": ["ipv4"],
        "ttl": "1h",
        "request": {"url": "https://geo.test/{value}"},
        "outputs": {"country": "country.code", "asn": "asn"},
    }
    base.update(changes)
    return EnricherSpec.model_validate(base)


# ----------------------------------------------------------------- specs and values
def test_spec_parses_ttl_and_rate():
    assert parse_ttl("90s") == 90 and parse_ttl("24h") == 86400 and parse_ttl("7d") == 7 * 86400
    assert parse_rate("1000/day") == (1000, 86400) and parse_rate("45/min") == (45, 60)
    assert spec(ttl="15m").ttl_s == 900


@pytest.mark.parametrize(
    "changes, complaint",
    [
        ({"name": "Bad Name"}, "name"),
        ({"ttl": "soon"}, "ttl"),
        ({"rate_limit": "lots"}, "rate_limit"),
        ({"request": {"url": "ftp://x"}}, "url"),
        ({"request": {"url": "https://x", "body": {"a": "b"}}}, "POST"),
        ({"outputs": {"ok": "[invalid"}}, "JMESPath"),
        ({"request": {"url": "https://x/{secret}"}}, "secret"),
        ({"kind": "http", "request": None}, "request"),
        ({"inputs": []}, "inputs"),
    ],
)
def test_spec_rejects(changes, complaint):
    with pytest.raises(ValueError) as e:
        spec(**changes)
    assert complaint in str(e.value)


def test_classify_values():
    assert classify("8.8.8.8") == (EntityType.IPV4, "8.8.8.8")
    assert classify("1.2.3[.]4") == (EntityType.IPV4, "1.2.3.4")
    assert classify("2001:db8::1") == (EntityType.IPV6, "2001:db8::1")
    assert classify("10.0.0.0/8") == (EntityType.CIDR, "10.0.0.0/8")
    assert classify("as15169") == (EntityType.ASN, "AS15169")
    assert classify("Example.COM.") == (EntityType.DOMAIN, "example.com")
    assert classify("hxxps://evil[.]example/x") == (EntityType.URL, "https://evil.example/x")
    assert classify("Bob@Example.com") == (EntityType.EMAIL, "bob@example.com")
    assert classify("cve-2024-3094") == (EntityType.CVE, "CVE-2024-3094")
    assert classify("D41D8CD98F00B204E9800998ECF8427E") == (EntityType.MD5, "d41d8cd98f00b204e9800998ecf8427e")
    assert classify("a" * 40)[0] == EntityType.SHA1 and classify("0" * 64)[0] == EntityType.SHA256
    assert classify("1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa")[0] == EntityType.CRYPTO_WALLET
    assert classify("0x" + "a" * 40)[0] == EntityType.CRYPTO_WALLET
    assert classify("+1 (415) 555-0100") == (EntityType.PHONE, "+14155550100")
    assert classify("Apple Inc") == (EntityType.GENERIC, "Apple Inc")


# ----------------------------------------------------------------- store
def test_store_overrides_and_restores_builtins(tmp_path):
    store = EnricherStore(tmp_path)
    names = set(store.all())
    assert {"ipwhois", "cve", "hashlookup"} <= names
    entry = store.get("ipwhois")
    assert entry.builtin and not entry.overridden
    changed = entry.spec.model_copy(update={"enabled": False, "ttl": "2d"})
    saved = store.save(changed)
    assert saved.builtin and saved.overridden and not saved.spec.enabled
    assert json.loads((tmp_path / "ipwhois.json").read_text())["ttl"] == "2d"
    assert store.delete("ipwhois") and store.get("ipwhois").spec.enabled
    # The operator's own enrichers are not built-in; a bad file is skipped, not fatal.
    store.save(spec("mine"))
    assert not store.get("mine").builtin
    (tmp_path / "broken.json").write_text("{not json")
    (tmp_path / "wrong.json").write_text(json.dumps(spec("other").model_dump(mode="json")))
    assert "mine" in store.all() and "broken" not in store.all() and "wrong" not in store.all()


def test_reverse_ptr_filter():
    assert apply_filters("1.2.3.4", ["reverse_ptr"]) == "4.3.2.1.in-addr.arpa"
    assert apply_filters("2001:db8::1", ["reverse_ptr"]).endswith(".8.b.d.0.1.0.0.2.ip6.arpa")
    assert apply_filters(" Ada@Example.com ", ["strip", "lower", "md5"]) == "3e3417d7ef77d5932a6734b916515ed5"


def test_entry_usability(monkeypatch):
    keyed = spec(secret="GEO_KEY", request={"url": "https://geo.test/{value}", "headers": {"Key": "{secret}"}})
    monkeypatch.delenv("GEO_KEY", raising=False)
    assert Entry(keyed, True, False).needs_key() and not Entry(keyed, True, False).usable(["open"])
    monkeypatch.setenv("GEO_KEY", "k")
    assert Entry(keyed, True, False).usable(["open"])
    assert not Entry(spec(terms="non_commercial"), True, False).usable(["open"])
    optional = keyed.model_copy(update={"secret_optional": True})
    monkeypatch.delenv("GEO_KEY")
    assert Entry(optional, True, False).usable(["open"])


# ----------------------------------------------------------------- cache
REPLIES = ["plain text", [1, "two", {"three": 3}], {"nested": {"deep": [None, 1.5]}}, {"@context": "https://schema.org", "@type": "Thing"},
           {"id": 2 ** 64}]


@pytest.mark.parametrize("in_database", [True, False])
def test_cache_keeps_a_reply_as_it_came(request, in_database):
    """Text, lists, JSON-LD (a top-level @type) and big integers: what ArcadeDB's maps would refuse or mangle."""
    if in_database:
        from vectorstore.tables import TableStore

        arcade = request.getfixturevalue("arcade")
        cache = ResultCache(arcade, TableStore(arcade))
    else:
        cache = MemoryCache()
    for n, raw in enumerate(REPLIES):
        asyncio.run(cache.put("geo", "ipv4", f"10.0.0.{n}", {"status": "ok", "outputs": {"n": n}, "raw": raw}, ttl_s=60))
    assert [asyncio.run(cache.get("geo", "ipv4", f"10.0.0.{n}"))["raw"] for n in range(len(REPLIES))] == REPLIES
    cache.close()


@pytest.mark.parametrize("in_database", [True, False])
def test_cache_expires(request, in_database):
    if in_database:
        from vectorstore.tables import TableStore

        arcade = request.getfixturevalue("arcade")
        cache = ResultCache(arcade, TableStore(arcade))
    else:
        cache = MemoryCache()
    result = {"status": "ok", "outputs": {"a": 1}, "raw": {"a": 1}, "http_status": 200}
    asyncio.run(cache.put("geo", "ipv4", "1.1.1.1", result, ttl_s=60))
    hit = asyncio.run(cache.get("geo", "ipv4", "1.1.1.1"))
    assert hit["outputs"] == {"a": 1} and hit["expires_at"] > time.time()
    asyncio.run(cache.put("geo", "ipv4", "2.2.2.2", result, ttl_s=0))
    assert asyncio.run(cache.get("geo", "ipv4", "2.2.2.2")) is None
    assert asyncio.run(cache.forget("geo")) == 2
    cache.close()


# ----------------------------------------------------------------- runner
class FakeSource:
    """An httpx transport standing in for every source; records what it was asked."""

    def __init__(self):
        self.requests = []
        self.status = 200
        self.reply = {"country": {"code": "NL"}, "asn": 1136}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path.endswith("/redirect"):
            return httpx.Response(302, headers={"location": "https://geo.test/after"})
        if request.url.path.endswith("/missing"):
            return httpx.Response(404, json={"error": "no such thing"})
        if request.url.path.endswith("/busy"):
            return httpx.Response(429, headers={"retry-after": "30"})
        if request.url.path.endswith("/text"):
            return httpx.Response(200, text="plain words")
        return httpx.Response(self.status, json=self.reply)


@pytest.fixture
def service(tmp_path):
    store = EnricherStore(tmp_path / "specs")
    svc = EnrichService(store, MemoryCache(), ["open", "attribution"], allow_private=True)
    source = FakeSource()
    svc._client = httpx.AsyncClient(transport=httpx.MockTransport(source))
    svc.source = source
    return svc


def run(coro):
    return asyncio.run(coro)


def test_runner_maps_and_caches(service):
    entry = Entry(spec(), False, False)
    first = run(service.run_one(entry, EntityType.IPV4, "1.1.1.1"))
    assert first["status"] == "ok" and first["outputs"] == {"country": "NL", "asn": 1136}
    assert first["raw"]["asn"] == 1136 and not first["cached"] and first["http_status"] == 200
    assert str(service.source.requests[0].url) == "https://geo.test/1.1.1.1"
    second = run(service.run_one(entry, EntityType.IPV4, "1.1.1.1"))
    assert second["cached"] and len(service.source.requests) == 1
    third = run(service.run_one(entry, EntityType.IPV4, "1.1.1.1", refresh=True))
    assert not third["cached"] and len(service.source.requests) == 2


def test_runner_renders_placeholders(service, monkeypatch):
    monkeypatch.setenv("GEO_KEY", "s3cret")
    s = spec(
        secret="GEO_KEY",
        value_filters=["upper"],
        inputs=["ipv4", "md5"],
        request={
            "method": "POST",
            "url": "https://geo.test/{type}/{value}",
            "query": {"token": "{secret}", "empty": ""},
            "headers": {"X-Key": "{secret}"},
            "body": {"q": "{value}", "fixed": 1},
        },
    )
    run(service.run_one(Entry(s, False, False), EntityType.MD5, "abc def"))
    sent = service.source.requests[-1]
    assert str(sent.url) == "https://geo.test/md5/ABC%20DEF?token=s3cret"
    assert sent.headers["x-key"] == "s3cret" and json.loads(sent.content) == {"q": "ABC DEF", "fixed": 1}
    masked = service.masked_request(s, EntityType.MD5, "abc def")
    assert "s3cret" not in json.dumps(masked) and "***" in masked["url"]


def test_runner_statuses(service, monkeypatch):
    def one(**changes):
        return run(service.run_one(Entry(spec(**changes), False, False), EntityType.IPV4, "1.1.1.1"))

    assert one(request={"url": "https://geo.test/missing"})["status"] == "not_found"
    busy = one(name="busy", request={"url": "https://geo.test/busy"})
    assert busy["status"] == "rate_limited" and "429" in busy["error"]
    # The cooldown the 429 set holds the next request back without sending it.
    again = one(name="busy", request={"url": "https://geo.test/busy"})
    assert again["status"] == "rate_limited" and "slow down" in again["error"]
    assert service.state(spec(name="busy"))["cooldown_until"] > time.time()
    text = one(name="text", request={"url": "https://geo.test/text"})
    assert text["status"] == "ok" and text["raw"] == "plain words" and text["outputs"] == {"country": None, "asn": None}
    assert one(name="off", enabled=False)["status"] == "disabled"
    assert one(name="nc", terms="non_commercial")["status"] == "not_allowed"
    monkeypatch.delenv("GEO_KEY", raising=False)
    keyed = one(name="keyed", secret="GEO_KEY", request={"url": "https://geo.test/x", "headers": {"K": "{secret}"}})
    assert keyed["status"] == "no_key"
    followed = one(name="redir", request={"url": "https://geo.test/redirect"})
    assert followed["status"] == "ok" and str(service.source.requests[-1].url) == "https://geo.test/after"


def test_runner_rate_limit(service):
    entry = Entry(spec(rate_limit="2/min"), False, False)
    for i in range(2):
        assert run(service.run_one(entry, EntityType.IPV4, f"1.1.1.{i}"))["status"] == "ok"
    third = run(service.run_one(entry, EntityType.IPV4, "1.1.1.9"))
    assert third["status"] == "rate_limited" and "2/min" in third["error"]
    assert service.state(entry.spec) == {"requests_in_window": 2, "limit": 2, "cooldown_until": None}


def test_runner_refuses_private_hosts(tmp_path):
    svc = EnrichService(EnricherStore(tmp_path), MemoryCache(), ["open"], allow_private=False)
    entry = Entry(spec(request={"url": "http://127.0.0.1:9/x"}), False, False)
    result = run(svc.run_one(entry, EntityType.IPV4, "1.1.1.1"))
    assert result["status"] == "error" and "not a public address" in result["error"]
    run(svc.close())


def test_enrich_merges_sources(service):
    service.store.save(spec("a", outputs={"country": "country.code"}))
    service.store.save(spec("b", outputs={"country": "'XX'", "asn": "asn"}))
    service.store.save(spec("other", inputs=["domain"]))
    out = run(service.enrich("1.1.1[.]1", names=["a", "b"]))
    assert out["type"] == "ipv4" and out["value"] == "1.1.1.1"
    assert [r["enricher"] for r in out["results"]] == ["a", "b"]
    assert out["merged"] == {"country": "NL", "asn": 1136} and out["sources"] == {"country": "a", "asn": "b"}


def test_reverse_dns_is_a_request_like_the_others(service):
    spec = service.store.get("ptr").spec
    sent = service.render(spec, EntityType.IPV4, "1.1.1.1")
    assert sent["url"] == "https://cloudflare-dns.com/dns-query"
    assert sent["params"] == {"name": "1.1.1.1.in-addr.arpa", "type": "PTR"}
    reply = {"Status": 0, "Answer": [{"name": "1.1.1.1.in-addr.arpa", "type": 12, "data": "one.one.one.one."}]}
    assert map_outputs(spec.outputs, reply) == {"hostname": "one.one.one.one.", "names": ["one.one.one.one."], "status": 0}


# ----------------------------------------------------------------- routes
@pytest.fixture
def client():
    from api import app as app_module

    with TestClient(app_module.app) as c:
        service = app_module.app.state.enrichers
        source = FakeSource()
        service._client = httpx.AsyncClient(transport=httpx.MockTransport(source))
        service.allow_private = True
        c.source = source
        yield c
        for name in ("geo", "mine"):
            service.store.delete(name)
        c.portal.call(service.cache.forget)


def test_routes_list_and_edit(client):
    listing = client.get("/enrichers").json()
    names = {e["name"]: e for e in listing["enrichers"]}
    assert "ipwhois" in names and names["ipwhois"]["builtin"] and names["ipwhois"]["usable"]
    assert names["abuseipdb"]["secret_set"] is False and names["abuseipdb"]["usable"] is False
    assert names["ptr"]["builtin"] and "ipv4" in listing["types"]

    body = spec("geo").model_dump(mode="json")
    saved = client.put("/enrichers/geo", json=body)
    assert saved.status_code == 200 and saved.json()["builtin"] is False
    assert client.put("/enrichers/other", json=body).status_code == 400
    assert client.put("/enrichers/geo", json={**body, "ttl": "never"}).status_code == 422
    assert client.put("/enrichers/geo", json={**body, "kind": "local"}).status_code == 422

    assert client.get("/enrichers/geo").json()["name"] == "geo"
    assert client.delete("/enrichers/ipwhois").status_code == 400  # unchanged built-in
    changed = client.put("/enrichers/ipwhois", json=_spec_only({**names["ipwhois"], "enabled": False}))
    assert changed.status_code == 200 and changed.json()["overridden"] and not changed.json()["enabled"]
    assert client.delete("/enrichers/ipwhois").status_code == 204
    assert client.get("/enrichers/ipwhois").json()["enabled"] is True
    assert client.delete("/enrichers/geo").status_code == 204
    assert client.get("/enrichers/geo").status_code == 404


def _spec_only(info):
    return {k: v for k, v in info.items() if k not in ("builtin", "overridden", "secret_set", "allowed", "usable", "state")}


def test_routes_enrich_and_test(client, monkeypatch):
    body = spec("geo").model_dump(mode="json")
    assert client.put("/enrichers/geo", json=body).status_code == 200
    out = client.post("/enrich", json={"value": "9.9.9.9", "enrichers": ["geo"]}).json()
    assert out["type"] == "ipv4" and out["results"][0]["outputs"]["country"] == "NL" and out["merged"]["asn"] == 1136
    assert client.post("/enrich", json={"value": "9.9.9.9", "enrichers": ["nope"]}).status_code == 404
    assert client.post("/enrich", json={"value": "9.9.9.9", "type": "domain", "enrichers": ["geo"]}).json()["results"] == []

    tested = client.post("/enrichers/geo/test", json={"value": "9.9.9.9"}).json()
    assert tested["result"]["status"] == "ok" and tested["request"]["url"] == "https://geo.test/9.9.9.9"
    assert tested["result"]["cached"] is False
    assert client.post("/enrichers/geo/test", json={"value": "example.com"}).status_code == 400

    # Keys go to the settings file and take effect at once.
    assert client.put("/enrichers/geo/secret", json={"value": "k"}).status_code == 400  # names no secret
    keyed = {**body, "secret": "GEO_TEST_KEY", "request": {"url": "https://geo.test/{value}", "headers": {"Key": "{secret}"}}}
    monkeypatch.delenv("GEO_TEST_KEY", raising=False)
    assert client.put("/enrichers/geo", json=keyed).json()["usable"] is False
    assert client.put("/enrichers/geo/secret", json={"value": "abc"}).json() == {"secret": "GEO_TEST_KEY", "secret_set": True}
    from config import Config

    assert "GEO_TEST_KEY=abc" in Config.ENV_FILE.read_text()
    assert client.get("/enrichers/geo").json()["usable"] is True
    assert client.put("/enrichers/geo/secret", json={"value": ""}).json()["secret_set"] is False
    assert client.delete("/enrichers/cache").status_code == 204
