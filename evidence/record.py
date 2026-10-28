"""
The signed capture record: what it holds, its canonical bytes, its hash and
the chain between records.

A record wraps what the extension sent (``client``), the artifacts the server
verified, what the enrichers said about the page's hosts, the receive time and
the previous record's hash in its workspace's chain. It is serialized once, as
RFC 8785 canonical JSON, and those exact bytes are hashed, signed and stored;
nothing ever re-serializes them.

v3 records name their workspace (``workspaceId``). v2 records, from before
workspaces, have none: they are all in the default workspace.
"""

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List

import rfc8785

from errors import Refused

SCHEMA = "evidence-capture/record/v3"
SERVICE_NAME = "ai-toolkit-evidence"
GENESIS_HASH = "0" * 64
DEFAULT_CASE_ID = "inbox"
DEFAULT_CASE_NAME = "Inbox"
DEFAULT_WORKSPACE_ID = "default"
DEFAULT_WORKSPACE_NAME = "Default"
MAX_ARTIFACT_NAME = 100
# Lower-case letters and digits in groups joined by single hyphens, so a UUID fits.
WORKSPACE_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MAX_WORKSPACE_ID = 36


@dataclass
class VerifiedArtifact:
    """An uploaded file whose hash matched what the client declared."""

    name: str
    media_type: str
    sha256: str
    data: bytes = field(repr=False)

    def entry(self) -> Dict[str, Any]:
        return {"name": self.name, "mediaType": self.media_type, "size": len(self.data), "sha256": self.sha256}


def new_id() -> str:
    return str(uuid.uuid4())


def check_workspace_id(value: Any) -> str:
    if not isinstance(value, str) or len(value) > MAX_WORKSPACE_ID or not WORKSPACE_ID.match(value):
        raise Refused(f"a workspace id is 1-{MAX_WORKSPACE_ID} lower-case letters and digits, "
                                 "in groups joined by single hyphens (a UUID, say)")
    return value


def now_iso() -> str:
    """UTC, millisecond precision, Z suffix: 2026-10-18T12:00:00.123Z."""
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(record: Dict[str, Any]) -> bytes:
    return rfc8785.dumps(record)


def is_canonical(data: bytes) -> bool:
    """Whether ``data`` is exactly the RFC 8785 form of the JSON it holds."""
    try:
        return canonical(json.loads(data)) == data
    except (ValueError, TypeError):
        return False


def is_valid_artifact_name(name: Any) -> bool:
    """Artifact names become file names, so they are kept to a safe alphabet."""
    if not isinstance(name, str):
        return False
    raw = name.encode("utf-8", "surrogateescape")
    return (
        0 < len(raw) <= MAX_ARTIFACT_NAME
        and raw[0:1].isalnum()
        and raw[0:1].isascii()
        and all(b in b"abcdefghijklmnopqrstuvwxyz0123456789._-" for b in raw)
    )


def build_record(
    *,
    seq: int,
    capture_id: str,
    workspace_id: str,
    case_id: str,
    received_at: str,
    prev_hash: str,
    version: str,
    signing_key_sha256: str,
    artifacts: List[VerifiedArtifact],
    client: Any,
    enrichment: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        "schema": SCHEMA,
        "seq": seq,
        "captureId": capture_id,
        "workspaceId": workspace_id,
        "caseId": case_id,
        "receivedAt": received_at,
        "prevRecordHash": prev_hash,
        "service": {"name": SERVICE_NAME, "version": version, "signingKeySha256": signing_key_sha256},
        "artifacts": [a.entry() for a in artifacts],
        "client": client,
        "enrichment": enrichment,
    }


def verify_uploads(metadata: Any, files: Dict[str, bytes]) -> List[VerifiedArtifact]:
    """Check the uploaded files against the hashes the client declared (computed in the browser).

    Every declared artifact must be uploaded and hash as declared; nothing undeclared
    may be uploaded. Anything altered in transit is refused.
    """
    if not isinstance(metadata, dict):
        raise Refused("metadata must be a JSON object")
    declared = metadata.get("artifacts")
    if not isinstance(declared, list) or not declared:
        raise Refused("metadata.artifacts must be a non-empty array")
    remaining = dict(files)
    verified: List[VerifiedArtifact] = []
    seen = set()
    for entry in declared:
        if not isinstance(entry, dict):
            raise Refused(f"invalid artifact entry {json.dumps(entry)[:200]}")
        name = entry.get("name")
        if not is_valid_artifact_name(name):
            raise Refused(f"invalid artifact name in {json.dumps(entry)[:200]}")
        if name in seen:
            raise Refused(f"artifact {name} is declared twice")
        seen.add(name)
        claimed = entry.get("sha256")
        if not isinstance(claimed, str) or not claimed:
            raise Refused(f"artifact {name} has no sha256")
        if name not in remaining:
            raise Refused(f"artifact {name} declared but not uploaded")
        data = remaining.pop(name)
        actual = sha256_hex(data)
        if actual != claimed.lower():
            raise Refused(f"hash mismatch for {name}: client said {claimed}, received {actual}")
        size = entry.get("size")
        if size is not None and (not isinstance(size, int) or isinstance(size, bool) or size != len(data)):
            raise Refused(f"size mismatch for {name}")
        media_type = entry.get("mediaType")
        if not isinstance(media_type, str) or not media_type:
            media_type = "application/octet-stream"
        verified.append(VerifiedArtifact(name=name, media_type=media_type, sha256=actual, data=data))
    if remaining:
        raise Refused(f"uploaded file {next(iter(remaining))} was not declared")
    return verified
