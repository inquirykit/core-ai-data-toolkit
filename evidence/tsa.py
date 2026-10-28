"""
RFC 3161 trusted timestamps.

Only a record's SHA-256 is sent to the time stamping authority; its signed
reply (``record.tsr``) proves the record existed at ``genTime`` and was not
back-dated. Here the status, nonce, hash algorithm and message imprint are
checked; full validation of the token's certificate chain is left to
``openssl ts -verify``, as the browser extension's README describes.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import timezone
from typing import TYPE_CHECKING, Optional, Tuple

import httpx
from cryptography.x509 import ObjectIdentifier
from rfc3161_client import HashAlgorithm, PKIStatus, TimestampRequestBuilder, decode_timestamp_response

if TYPE_CHECKING:
    from .store import EvidenceStore, Ledger

logger = logging.getLogger(__name__)

SHA256_OID = ObjectIdentifier("2.16.840.1.101.3.4.2.1")
GRANTED = (PKIStatus.GRANTED, PKIStatus.GRANTED_WITH_MODS)


class TsaError(Exception):
    pass


@dataclass
class Timestamp:
    """A granted token: the whole TimeStampResp, DER, exactly as the TSA sent it."""

    response_der: bytes
    gen_time: str


@dataclass
class TokenInfo:
    gen_time: str
    nonce: Optional[int]


def _iso(dt) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    text = dt.strftime("%Y-%m-%dT%H:%M:%S")
    if dt.microsecond:
        text += f".{dt.microsecond // 1000:03d}"
    return text + "Z"


def inspect(response_der: bytes, expected_digest: bytes) -> TokenInfo:
    """Decode a stored TimeStampResp and check it covers ``expected_digest`` (a SHA-256)."""
    try:
        response = decode_timestamp_response(response_der)
    except Exception as e:
        raise TsaError(f"decoding TimeStampResp: {e}") from None
    if response.status not in GRANTED:
        try:
            status = PKIStatus(response.status).name
        except ValueError:
            status = str(response.status)
        raise TsaError(f"TSA refused the request: {status} {' '.join(response.status_string or [])}".strip())
    try:
        info = response.tst_info
    except Exception as e:
        raise TsaError(f"TSA response has no usable token: {e}") from None
    imprint = info.message_imprint
    if imprint.hash_algorithm != SHA256_OID:
        raise TsaError("token imprint is not SHA-256")
    if imprint.message != expected_digest:
        raise TsaError("token imprint does not match the record hash")
    return TokenInfo(gen_time=_iso(info.gen_time), nonce=info.nonce)


def build_request(canonical: bytes):
    """A TimeStampReq over the SHA-256 of ``canonical`` (the record hash), with a random nonce, asking for the TSA's certificate."""
    try:
        return (
            TimestampRequestBuilder()
            .data(canonical)
            .hash_algorithm(HashAlgorithm.SHA256)
            .nonce(nonce=True)
            .cert_request(cert_request=True)
            .build()
        )
    except Exception as e:
        raise TsaError(f"building the request: {e}") from None


class Tsa:
    """The configured time stamping authority; ``url`` empty means timestamping is off."""

    def __init__(self, url: str, timeout: float = 20, version: str = ""):
        self.url = url.strip()
        self.timeout = timeout
        self.version = version
        self._client: Optional[httpx.AsyncClient] = None

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self.timeout, headers={"User-Agent": f"ai-toolkit-evidence/{self.version}"}, follow_redirects=False
            )
        return self._client

    async def request(self, canonical: bytes, digest: bytes) -> Timestamp:
        """Ask for a token over ``canonical`` (whose SHA-256 is ``digest``); raises TsaError."""
        if not self.enabled:
            raise TsaError("timestamping is disabled")
        req = build_request(canonical)
        try:
            response = await self._http().post(
                self.url, content=req.as_bytes(), headers={"Content-Type": "application/timestamp-query"}
            )
        except httpx.HTTPError as e:
            raise TsaError(f"contacting TSA {self.url}: {e}") from None
        if response.status_code >= 400:
            raise TsaError(f"TSA {self.url} answered HTTP {response.status_code}")
        info = inspect(response.content, digest)
        if req.nonce is not None and info.nonce != req.nonce:
            raise TsaError("TSA response nonce does not match request")
        return Timestamp(response_der=response.content, gen_time=info.gen_time)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


async def timestamp_one(store: "Ledger", tsa: Tsa, capture_id: str, canonical: bytes, record_hash: str) -> dict:
    """Request a token for one record of the workspace and record the outcome; what POST /evidence/captures answers
    as ``timestamp``."""
    if not tsa.enabled:
        return {"status": "disabled"}
    try:
        ts = await tsa.request(canonical, bytes.fromhex(record_hash))
    except TsaError as e:
        logger.warning("Timestamping %s failed: %s", capture_id, e)
        await store.set_timestamp_error(capture_id, str(e))
        return {"status": "failed", "error": str(e), "tsa": tsa.url}
    await store.set_timestamp(capture_id, ts.response_der, ts.gen_time)
    return {"status": "granted", "genTime": ts.gen_time, "tsa": tsa.url}


async def timestamp_pending(store: "EvidenceStore", tsa: Tsa) -> Tuple[int, int]:
    """Try again for every record without a token, in every workspace; how many were granted and how many failed."""
    ok = failed = 0
    if not tsa.enabled:
        return ok, failed
    for ledger in await store.ledgers():
        for row in await ledger.pending_timestamps():
            result = await timestamp_one(ledger, tsa, row["capture_id"], row["record_json"].encode(), row["record_hash"])
            if result["status"] == "granted":
                ok += 1
            else:
                failed += 1
            await asyncio.sleep(0)
    return ok, failed
