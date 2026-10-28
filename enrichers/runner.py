"""
Runs enrichers: renders the request, sends it, maps the reply, caches it.

Every enricher that takes the value's type runs at once (up to
``max_concurrency``), each with its own timeout, so one slow source does not
hold the others. Per enricher there is a request counter for its
``rate_limit`` and a cooldown set by a 429 from the source. A request goes
only to a public address: the host is resolved first and private, loopback
and link-local addresses are refused (ENRICH_ALLOW_PRIVATE lifts that for
sources on the local network), because the request templates are editable.
"""

import asyncio
import collections
import ipaddress
import json
import logging
import os
import time
from typing import Any, Deque, Dict, List, Optional, Tuple, Union
from urllib.parse import quote, urljoin, urlsplit

import httpx

from .cache import MemoryCache, ResultCache
from .classify import classify, normalise
from .spec import EnricherSpec, EntityType, apply_filters, map_outputs, parse_rate
from .store import EnricherStore, Entry

logger = logging.getLogger(__name__)

OK, NOT_FOUND, ERROR, NO_KEY, DISABLED, NOT_ALLOWED, RATE_LIMITED = (
    "ok", "not_found", "error", "no_key", "disabled", "not_allowed", "rate_limited",
)
MAX_REPLY_BYTES = 1_000_000
MAX_REDIRECTS = 5
MAX_TEXT_RAW = 65536
DEFAULT_COOLDOWN_S = 60
USER_AGENT = "ai-toolkit-enrichers/1.0"


def _result(enricher: EnricherSpec, status: str, **fields) -> Dict[str, Any]:
    out = {
        "enricher": enricher.name, "status": status, "outputs": {}, "raw": None, "cached": False,
        "observed_at": None, "expires_at": None, "ms": None, "http_status": None, "error": None,
        "terms": enricher.terms, "attribution": enricher.attribution,
    }
    out.update(fields)
    return out


class RateState:
    """One enricher's request times within its window, and its cooldown after a 429."""

    def __init__(self):
        self.times: Deque[float] = collections.deque()
        self.cooldown_until: Optional[float] = None

    def prune(self, period_s: float) -> None:
        cutoff = time.time() - period_s
        while self.times and self.times[0] < cutoff:
            self.times.popleft()


class EnrichService:
    def __init__(
        self,
        store: EnricherStore,
        cache: Union[ResultCache, MemoryCache],
        terms_allowed: List[str],
        allow_private: bool = False,
        max_concurrency: int = 8,
    ):
        self.store = store
        self.cache = cache
        self.terms_allowed = list(terms_allowed)
        self.allow_private = allow_private
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._rates: Dict[str, RateState] = collections.defaultdict(RateState)
        self._client: Optional[httpx.AsyncClient] = None

    # ------------------------------------------------------------------ describing
    def state(self, spec: EnricherSpec) -> Dict[str, Any]:
        rate = self._rates.get(spec.name)
        limit = parse_rate(spec.rate_limit) if spec.rate_limit else None
        if rate is not None and limit is not None:
            rate.prune(limit[1])
        cooldown = rate.cooldown_until if rate is not None else None
        if cooldown is not None and cooldown <= time.time():
            cooldown = None
        return {
            "requests_in_window": len(rate.times) if rate is not None else 0,
            "limit": limit[0] if limit else None,
            "cooldown_until": cooldown,
        }

    def describe(self, entry: Entry) -> Dict[str, Any]:
        spec = entry.spec
        return {
            **spec.model_dump(mode="json"),
            "builtin": entry.builtin,
            "overridden": entry.overridden,
            "secret_set": entry.secret_set,
            "allowed": spec.terms in self.terms_allowed,
            "usable": entry.usable(self.terms_allowed),
            "state": self.state(spec),
        }

    # ------------------------------------------------------------------ running
    def resolve_value(self, value: str, type_: Optional[EntityType]) -> Tuple[EntityType, str]:
        if type_ is None:
            return classify(value)
        return type_, normalise(value, type_)

    async def enrich(
        self,
        value: str,
        type_: Optional[EntityType] = None,
        names: Optional[List[str]] = None,
        refresh: bool = False,
    ) -> Dict[str, Any]:
        """Run every enricher taking the value's type (or the ones named) and merge their outputs."""
        type_, value = self.resolve_value(value, type_)
        entries = [e for e in self.store.all().values() if type_ in e.spec.inputs]
        if names is not None:
            wanted = set(names)
            entries = [e for e in entries if e.name in wanted]
        results = await asyncio.gather(*(self.run_one(e, type_, value, refresh=refresh) for e in entries))
        merged: Dict[str, Any] = {}
        sources: Dict[str, str] = {}
        for r in results:
            if r["status"] != OK:
                continue
            for name, found in r["outputs"].items():
                if found is not None and found != "" and found != [] and name not in merged:
                    merged[name] = found
                    sources[name] = r["enricher"]
        return {"value": value, "type": type_.value, "results": list(results), "merged": merged, "sources": sources}

    async def run_one(
        self, entry: Entry, type_: EntityType, value: str, *, refresh: bool = False, force: bool = False
    ) -> Dict[str, Any]:
        """One enricher's result for the value; ``force`` runs it even when disabled or not allowed, uncached."""
        spec = entry.spec
        if not force:
            if not spec.enabled:
                return _result(spec, DISABLED)
            if spec.terms not in self.terms_allowed:
                return _result(spec, NOT_ALLOWED, error=f"terms {spec.terms!r} are not in ENRICHER_TERMS")
        if entry.needs_key() and not entry.secret_set:
            return _result(spec, NO_KEY, error=f"{spec.secret} is not set")
        if not force and not refresh:
            cached = await self.cache.get(spec.name, type_.value, value)
            if cached is not None:
                return _result(spec, cached["status"], cached=True, **{k: v for k, v in cached.items() if k != "status"})
        blocked = self._rate_limited(spec)
        if blocked is not None:
            return _result(spec, RATE_LIMITED, error=blocked)
        started = time.monotonic()
        try:
            async with self._semaphore:
                result = await self._run_http(spec, type_, value)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # a source failing must not fail the lookup
            logger.warning("Enricher %s failed on %s: %s", spec.name, value, e)
            result = _result(spec, ERROR, error=_describe_error(e))
        result["ms"] = int((time.monotonic() - started) * 1000)
        if result["status"] in (OK, NOT_FOUND) and not force:
            now = time.time()
            result["observed_at"] = now
            result["expires_at"] = now + spec.ttl_s
            await self.cache.put(spec.name, type_.value, value, result, spec.ttl_s)
        return result

    def _rate_limited(self, spec: EnricherSpec) -> Optional[str]:
        """Count a request against the enricher's limit; the reason when it may not run now."""
        rate = self._rates[spec.name]
        now = time.time()
        if rate.cooldown_until is not None:
            if rate.cooldown_until > now:
                return f"the source asked to slow down; trying again after {int(rate.cooldown_until - now)}s"
            rate.cooldown_until = None
        if spec.rate_limit:
            count, period = parse_rate(spec.rate_limit)
            rate.prune(period)
            if len(rate.times) >= count:
                wait = int(rate.times[0] + period - now) + 1
                return f"rate limit {spec.rate_limit} reached; next request in {wait}s"
        rate.times.append(now)
        return None

    # ------------------------------------------------------------------ HTTP
    def render(self, spec: EnricherSpec, type_: EntityType, value: str) -> Dict[str, Any]:
        """The request as it would be sent: method, url, headers, params and body."""
        r = spec.request
        assert r is not None
        value = apply_filters(value, spec.value_filters)
        secret = (spec.secret and os.environ.get(spec.secret)) or ""
        encoded = quote(value, safe="")

        def fill(template: str, encode: bool) -> str:
            return (template.replace("{value}", encoded if encode else value)
                    .replace("{secret}", secret).replace("{type}", type_.value))

        params = {k: fill(v, False) for k, v in r.query.items()}
        params = {k: v for k, v in params.items() if v != ""}
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        headers.update({k: fill(v, False) for k, v in r.headers.items()})
        headers = {k: v for k, v in headers.items() if v != ""}
        body = None
        if r.body is not None:
            body = {k: (fill(v, False) if isinstance(v, str) else v) for k, v in r.body.items()}
            body = {k: v for k, v in body.items() if v != ""}
        return {"method": r.method, "url": fill(r.url, True), "headers": headers, "params": params, "body": body,
                "body_format": r.body_format}

    def masked_request(self, spec: EnricherSpec, type_: EntityType, value: str) -> Dict[str, Any]:
        """``render()`` with the secret replaced by ``***``, for showing in the dashboard."""
        rendered = self.render(spec, type_, value)
        secret = (spec.secret and os.environ.get(spec.secret)) or None

        def mask(text: str) -> str:
            return text.replace(secret, "***") if secret else text

        return {
            "method": rendered["method"],
            "url": mask(str(httpx.URL(rendered["url"], params=rendered["params"]))),
            "headers": {k: mask(v) for k, v in rendered["headers"].items()},
        }

    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(follow_redirects=False)
        return self._client

    async def _run_http(self, spec: EnricherSpec, type_: EntityType, value: str) -> Dict[str, Any]:
        rendered = self.render(spec, type_, value)
        url = str(httpx.URL(rendered["url"], params=rendered["params"]))
        method, headers, body = rendered["method"], rendered["headers"], rendered["body"]
        timeout = httpx.Timeout(spec.timeout)
        for _ in range(MAX_REDIRECTS + 1):
            await self._check_host(url)
            kwargs: Dict[str, Any] = {"headers": headers, "timeout": timeout}
            if body is not None and method == "POST":
                if rendered["body_format"] == "form":
                    kwargs["data"] = body
                else:
                    kwargs["json"] = body
            async with self.client().stream(method, url, **kwargs) as response:
                if response.is_redirect and "location" in response.headers:
                    url = urljoin(url, response.headers["location"])
                    if response.status_code == 303 or (response.status_code in (301, 302) and method == "POST"):
                        method, body = "GET", None
                    continue
                content = await self._read(response)
                return self._interpret(spec, response, content)
        return _result(spec, ERROR, error="too many redirects")

    async def _read(self, response: httpx.Response) -> bytes:
        chunks: List[bytes] = []
        size = 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > MAX_REPLY_BYTES:
                raise ValueError(f"reply longer than {MAX_REPLY_BYTES} bytes")
            chunks.append(chunk)
        return b"".join(chunks)

    def _interpret(self, spec: EnricherSpec, response: httpx.Response, content: bytes) -> Dict[str, Any]:
        status = response.status_code
        reply: Any
        try:
            reply = json.loads(content) if content else None
        except ValueError:
            text = content.decode(response.encoding or "utf-8", errors="replace")
            reply = text[:MAX_TEXT_RAW]
        if status == 404:
            return _result(spec, NOT_FOUND, raw=reply, http_status=status)
        if status == 429:
            retry = response.headers.get("retry-after")
            cooldown = float(retry) if retry and retry.isdigit() else DEFAULT_COOLDOWN_S
            self._rates[spec.name].cooldown_until = time.time() + cooldown
            return _result(spec, RATE_LIMITED, raw=reply, http_status=status,
                           error=f"the source answered 429; pausing for {int(cooldown)}s")
        if status >= 400:
            detail = reply if isinstance(reply, str) else json.dumps(reply)
            return _result(spec, ERROR, raw=reply, http_status=status, error=f"HTTP {status}: {detail[:300]}")
        return _result(spec, OK, outputs=map_outputs(spec.outputs, reply), raw=reply, http_status=status)

    async def _check_host(self, url: str) -> None:
        """Refuse a URL whose host resolves to a private, loopback or link-local address."""
        parts = urlsplit(url)
        host = parts.hostname
        if not host:
            raise ValueError("the URL has no host")
        if parts.scheme not in ("http", "https"):
            raise ValueError(f"unsupported URL scheme {parts.scheme!r}")
        if self.allow_private:
            return
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(host, parts.port or (443 if parts.scheme == "https" else 80))
        except OSError as e:
            raise ValueError(f"{host} does not resolve: {e}")
        for info in infos:
            address = ipaddress.ip_address(info[4][0])
            if not address.is_global:
                raise ValueError(f"{host} resolves to {address}, which is not a public address")

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        self.cache.close()


def _describe_error(e: Exception) -> str:
    if isinstance(e, (asyncio.TimeoutError, httpx.TimeoutException)):
        return "the source did not answer in time"
    if isinstance(e, ValueError):
        return str(e)[:300]
    return f"{type(e).__name__}: {e}"[:300]
