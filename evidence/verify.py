"""
Re-checks one workspace's whole ledger: sequence, chain links, canonical
encoding, signatures, that each record names the workspace, the files on disk
and the time stamp tokens.

A hash chain detects edits, reordering and deletions inside the log, but not
the loss of the newest records: keep ``headHash`` somewhere else, or rely on
each record's time stamp token, to cover that.
"""

import json
from typing import Any, Dict, List

from . import signing, tsa
from .record import DEFAULT_WORKSPACE_ID, GENESIS_HASH, is_canonical, sha256_hex
from .store import Ledger


def verify_all(store: Ledger) -> Dict[str, Any]:
    """The report GET /evidence/verify answers for the workspace. Blocks: run it on the database's pool."""
    key = store.signer.public_key
    fingerprint = signing.fingerprint(key)
    records = store._all_records()
    issues: List[Dict[str, Any]] = []
    prev_hash = GENESIS_HASH
    timestamped = 0

    for i, row in enumerate(records):
        seq = int(row["seq"])
        capture_id = row["capture_id"]

        def report(severity: str, message: str) -> None:
            issues.append({"severity": severity, "seq": seq, "captureId": capture_id, "message": message})

        data = row["record_json"].encode("utf-8")
        if seq != i + 1:
            report("error", f"sequence gap: expected seq {i + 1}")
        if sha256_hex(data) != row["record_hash"]:
            report("error", "record bytes do not match stored record hash")
        if not signing.verify(key, data, row["signature"]):
            report("error", "signature does not verify")
        try:
            record = json.loads(data)
        except ValueError as e:
            record = None
            report("error", f"record is not valid JSON: {e}")
        if record is not None:
            _check_body(row, record, data, prev_hash, fingerprint, store.workspace_id, report)
        _check_files(store, row, data, record, report)

        token = store.tsr_of(row)
        if token is None:
            last = row.get("tsa_error")
            report("warning", "no trusted timestamp" + (f" (last error: {last})" if last else ""))
        else:
            try:
                tsa.inspect(token, bytes.fromhex(row["record_hash"]))
                timestamped += 1
            except (tsa.TsaError, ValueError) as e:
                report("error", f"timestamp token invalid: {e}")

        # Continue the chain from what the record claims, so one broken record
        # gives one error instead of a cascade through the rest of the log.
        prev_hash = row["record_hash"]

    return {
        "workspace": store.workspace_id,
        "ok": not any(i["severity"] == "error" for i in issues),
        "records": len(records),
        "timestamped": timestamped,
        "headHash": records[-1]["record_hash"] if records else None,
        "signingKeySha256": fingerprint,
        "issues": issues,
    }


def _check_body(row, record, data: bytes, prev_hash: str, fingerprint: str, workspace_id: str, report) -> None:
    if not isinstance(record, dict):
        report("error", "record is not a JSON object")
        return
    if not is_canonical(data):
        report("error", "record is not in RFC 8785 canonical form")
    if record.get("prevRecordHash") != prev_hash:
        report("error", "chain broken: prevRecordHash does not match previous record")
    if record.get("seq") != int(row["seq"]):
        report("error", "seq inside record does not match log position")
    if record.get("captureId") != row["capture_id"]:
        report("error", "captureId inside record does not match index")
    if record.get("caseId") != row["case_id"]:
        report("error", "caseId inside record does not match index")
    # v2 records, from before workspaces, name none: they are the default workspace's.
    if record.get("workspaceId", DEFAULT_WORKSPACE_ID) != workspace_id:
        report("error", "workspaceId inside record does not match its workspace")
    service = record.get("service")
    if not isinstance(service, dict) or service.get("signingKeySha256") != fingerprint:
        report("warning", "record was signed by a different key")


def _check_files(store: Ledger, row, data: bytes, record, report) -> None:
    capture_id = row["capture_id"]
    on_disk = store.files.read_record(capture_id)
    if on_disk is None:
        report("error", "record.json unreadable")
    elif on_disk != data:
        report("error", "record.json on disk differs from the log")
    if not isinstance(record, dict):
        return
    for artifact in record.get("artifacts") or []:
        if not isinstance(artifact, dict) or not isinstance(artifact.get("name"), str) or not isinstance(artifact.get("sha256"), str):
            report("error", "artifact entry missing name or sha256")
            continue
        name = artifact["name"]
        content = store.files.read_artifact(capture_id, name)
        if content is None:
            report("error", f"artifact {name} unreadable")
        elif sha256_hex(content) != artifact["sha256"]:
            report("error", f"artifact {name} has been modified")
