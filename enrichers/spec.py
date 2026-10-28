"""
What an enricher is: a declarative HTTP call with inputs and outputs.

An enricher takes one value of a known entity type (an IP address, a domain,
a file hash, ...) and asks an outside service about it. Most such services are
one request returning JSON, so an enricher is data, not code: a request
template with ``{value}`` and ``{secret}`` filled in, and ``outputs`` that pick
fields out of the reply with JMESPath expressions. The dashboard's Enrichers
section edits these; enrichers/store.py keeps them as files.

A few enrichers cannot be a request (reverse DNS, local databases); they are
Python with the same contract, ``kind: "local"`` (see enrichers/local/).
"""

import hashlib
import ipaddress
import re
from enum import Enum
from typing import Any, Callable, Dict, List, Literal, Optional, get_args

import jmespath
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class EntityType(str, Enum):
    """The kinds of value an enricher can take. classify.py tells them apart."""

    IPV4 = "ipv4"
    IPV6 = "ipv6"
    CIDR = "cidr"
    ASN = "asn"
    DOMAIN = "domain"
    URL = "url"
    EMAIL = "email"
    PHONE = "phone"
    MD5 = "md5"
    SHA1 = "sha1"
    SHA256 = "sha256"
    CVE = "cve"
    CRYPTO_WALLET = "crypto_wallet"
    GENERIC = "generic"


# What a source's terms allow, so a deployment can turn a class of sources off
# (Config.ENRICHER_TERMS): usable for anything; usable with a credit to the
# source; free for non-commercial use only; needs a paid plan.
Terms = Literal["open", "attribution", "non_commercial", "paid"]
TERMS = get_args(Terms)
Filter = Literal["lower", "upper", "strip", "md5", "sha1", "sha256", "reverse_ptr"]

NAME = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,62}$")
_TTL = re.compile(r"^(\d+)\s*(s|m|h|d)$")
_RATE = re.compile(r"^(\d+)\s*/\s*(s|sec|second|m|min|minute|h|hour|d|day)$")
_UNIT_S = {"s": 1, "sec": 1, "second": 1, "m": 60, "min": 60, "minute": 60, "h": 3600, "hour": 3600, "d": 86400, "day": 86400}
PLACEHOLDERS = ("{value}", "{secret}", "{type}")


def parse_ttl(text: str) -> int:
    """'90s', '15m', '24h' or '7d' as seconds."""
    match = _TTL.match(text.strip())
    if not match:
        raise ValueError("ttl must be a number followed by s, m, h or d, e.g. 24h")
    return int(match.group(1)) * _UNIT_S[match.group(2)]


def parse_rate(text: str) -> tuple:
    """'1000/day' or '45/min' as (count, seconds)."""
    match = _RATE.match(text.strip())
    if not match:
        raise ValueError("rate_limit must be a count per period, e.g. 1000/day or 45/min")
    count = int(match.group(1))
    if count < 1:
        raise ValueError("rate_limit must allow at least one request")
    return count, _UNIT_S[match.group(2)]


class RequestSpec(BaseModel):
    """The HTTP request, with ``{value}``, ``{secret}`` and ``{type}`` to fill in.

    ``{value}`` is the value looked up (URL-encoded in ``url``, inserted as is
    elsewhere), ``{secret}`` the key, ``{type}`` the value's entity type
    ("ipv4", "sha256", ...). A query parameter or body field whose rendered
    value is empty (an optional secret that is not set) is left out.
    """

    # Answers carry every field, defaults included: the schema of an answer requires them all.
    model_config = ConfigDict(extra="forbid", json_schema_serialization_defaults_required=True)

    method: Literal["GET", "POST"] = "GET"
    url: str = Field(..., min_length=1, max_length=2000)
    query: Dict[str, str] = Field(default_factory=dict, max_length=32)
    headers: Dict[str, str] = Field(default_factory=dict, max_length=32)
    body: Optional[Dict[str, Any]] = Field(None, description="POST only; string values are templates")
    body_format: Literal["json", "form"] = "json"

    @field_validator("url")
    @classmethod
    def _scheme(cls, url: str) -> str:
        url = url.strip()
        if not url.lower().startswith(("https://", "http://")):
            raise ValueError("url must start with https:// or http://")
        return url

    @field_validator("headers")
    @classmethod
    def _header_names(cls, headers: Dict[str, str]) -> Dict[str, str]:
        for name in headers:
            if not re.match(r"^[A-Za-z0-9-]+$", name):
                raise ValueError(f"bad header name: {name!r}")
            if name.lower() == "host":
                raise ValueError("the Host header is set from the URL")
        return headers

    @model_validator(mode="after")
    def _body_only_on_post(self):
        if self.body is not None and self.method != "POST":
            raise ValueError("body needs method POST")
        return self


class EnricherSpec(BaseModel):
    """One enricher, as its file holds it and as the API serves and accepts it."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., pattern=NAME.pattern, description="Lower case letters, digits and _")
    kind: Literal["http"] = Field("http", description="The only kind: one HTTP request (kept so saved files load)")
    description: str = Field("", max_length=500)
    docs: Optional[str] = Field(None, max_length=500, description="Where the service's API is documented")
    inputs: List[EntityType] = Field(..., min_length=1, description="The entity types it takes")
    terms: Terms = Field("open", description="What the source's terms allow; Config.ENRICHER_TERMS filters on it")
    attribution: Optional[str] = Field(None, max_length=300, description="The credit the terms ask for, if any")
    secret: Optional[str] = Field(None, pattern=ENV_NAME.pattern, description="Environment variable holding the API key")
    secret_optional: bool = Field(False, description="Run without the key too (a keyless tier)")
    enabled: bool = True
    ttl: str = Field("24h", description="How long a result is cached: 90s, 15m, 24h, 7d")
    rate_limit: Optional[str] = Field(None, description="Most requests per period, e.g. 1000/day or 45/min")
    timeout: float = Field(10, ge=1, le=60, description="Seconds to wait for the reply")
    value_filters: List[Filter] = Field(default_factory=list, description="Applied to the value before the request, in order")
    request: RequestSpec
    outputs: Dict[str, str] = Field(default_factory=dict, max_length=64, description="Output name -> JMESPath over the reply")

    @field_validator("ttl")
    @classmethod
    def _ttl(cls, ttl: str) -> str:
        parse_ttl(ttl)
        return ttl.strip()

    @field_validator("rate_limit")
    @classmethod
    def _rate(cls, rate: Optional[str]) -> Optional[str]:
        if rate is not None and rate.strip():
            parse_rate(rate)
            return rate.strip()
        return None

    @field_validator("outputs")
    @classmethod
    def _outputs(cls, outputs: Dict[str, str]) -> Dict[str, str]:
        for name, expression in outputs.items():
            if not NAME.match(name):
                raise ValueError(f"bad output name: {name!r} (lower case letters, digits and _)")
            try:
                jmespath.compile(expression)
            except jmespath.exceptions.JMESPathError as e:
                raise ValueError(f"output {name}: bad JMESPath expression: {e}")
        return outputs

    @model_validator(mode="after")
    def _complete(self):
        if self.secret is None and self._uses("{secret}"):
            raise ValueError("the request uses {secret} but no secret is named")
        if self.secret_optional and self.secret is None:
            raise ValueError("secret_optional needs a secret")
        return self

    def _uses(self, placeholder: str) -> bool:
        r = self.request
        parts = [r.url, *r.query.values(), *r.headers.values()]
        if r.body:
            parts.extend(v for v in r.body.values() if isinstance(v, str))
        return any(placeholder in p for p in parts)

    @property
    def ttl_s(self) -> int:
        return parse_ttl(self.ttl)

    @property
    def uses_secret(self) -> bool:
        return self.secret is not None and self._uses("{secret}")


FILTERS: Dict[str, Callable[[str], str]] = {
    "lower": str.lower,
    "upper": str.upper,
    "strip": str.strip,
    "md5": lambda v: hashlib.md5(v.encode("utf-8")).hexdigest(),
    "sha1": lambda v: hashlib.sha1(v.encode("utf-8")).hexdigest(),
    "sha256": lambda v: hashlib.sha256(v.encode("utf-8")).hexdigest(),
    # An IP address as its reverse DNS name: 1.2.3.4 -> 4.3.2.1.in-addr.arpa
    "reverse_ptr": lambda v: ipaddress.ip_address(v).reverse_pointer,
}


def apply_filters(value: str, filters: List[str]) -> str:
    for f in filters:
        value = FILTERS[f](value)
    return value


def map_outputs(outputs: Dict[str, str], reply: Any) -> Dict[str, Any]:
    """Each output's JMESPath expression over the reply; a failing expression gives None."""
    found: Dict[str, Any] = {}
    for name, expression in outputs.items():
        try:
            found[name] = jmespath.search(expression, reply)
        except jmespath.exceptions.JMESPathError:
            found[name] = None
    return found
