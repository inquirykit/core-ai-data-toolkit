"""
/evidence: signed, hash-chained records of web pages a browser extension
captures (see evidence/), kept by workspace.

    GET  /evidence/workspaces                    the workspaces ("default" always exists)
    POST /evidence/workspaces                    {id?, name}: a new workspace (admin)
    GET  /evidence/cases                         the workspace's cases ("inbox" always exists)
    POST /evidence/cases                         {name}: a new case
    GET  /evidence/cases/{id}/captures           the case's captures, newest first
    POST /evidence/captures                      multipart: "metadata" JSON and one "file" per artifact
    GET  /evidence/captures/{id}                 the signed record, its hash, signature and time stamp
    GET  /evidence/captures/{id}/artifacts/{name}  one captured file
    GET  /evidence/cases/{id}/annotations        tags and notes of the case's captures that have any
    PUT  /evidence/captures/{id}/annotations     {tags?, notes?}: replace the capture's tags, notes or both
    GET  /evidence/cases/{id}/todos              the case's to-do list
    PUT  /evidence/cases/{id}/todos              {todos}: replace it
    GET  /evidence/verify                        re-check the workspace's whole ledger
    GET  /evidence/public-key                    the signing key's public half, PEM
    POST /evidence/timestamp-pending             try again for records without a time stamp, all workspaces (admin)

The routes about cases and captures act in one workspace: ``?workspace=<id>``,
the default one when left out (as clients from before workspaces do). A
capture is found only in its own workspace.

Annotations (tags and analyst notes added after a capture was taken) and a
case's todos are mutable and kept apart from the records: they never enter
the signed record, its hash, the chain or /evidence/verify.

Every route needs the API key, like the rest of the API. A request that
carries an Origin header must come from a browser extension
(chrome-extension:// or moz-extension://), or from one of
EVIDENCE_ALLOWED_ORIGINS exactly; web pages are refused even with the key.
Requests without an Origin (curl, tests) pass. Refusals are answered as every
route's are (``{"detail": message, "error": message}``); a body that does not
validate is FastAPI's 422.

Upload sizes: full-page screenshots and MHTML snapshots run to tens of
megabytes; nothing here caps the body, only ``MAX_METADATA_BYTES`` the
metadata field.
"""

import json
import logging
import time
import uuid
from typing import Annotated, Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StrictBool
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.formparsers import MultiPartException

from config import Config
from errors import Forbidden, NotFound, Refused
from evidence import Ledger, timestamp_one, timestamp_pending, verify_all, verify_uploads
from evidence.enrich import enrichment
from evidence.record import DEFAULT_WORKSPACE_ID, MAX_WORKSPACE_ID
from . import deps
from . import responses

logger = logging.getLogger(__name__)

EXTENSION_SCHEMES = ("chrome-extension://", "moz-extension://")
MAX_METADATA_BYTES = 16 * 1024 * 1024
MAX_CASE_NAME = 200
MAX_WORKSPACE_NAME = 200
MAX_TAGS, MAX_TAG = 64, 64
MAX_NOTES, MAX_NOTE = 200, 20000
MAX_TODOS, MAX_TODO = 500, 2000


def check_origin(origin: Optional[str] = Header(None)) -> None:
    """A request from a browser must come from an extension (or an allowed origin); one without an Origin passes."""
    if origin is None:
        return
    origin = origin.strip().rstrip("/")
    allowed = Config.EVIDENCE_ALLOWED_ORIGINS
    if allowed:
        ok = any(origin == o.strip().rstrip("/") for o in allowed)
    else:
        ok = origin.startswith(EXTENSION_SCHEMES)
    if not ok:
        raise Forbidden("origin not allowed")


# The origin is checked before the key, as it always was: a web page gets 403 whether or not it has the key (a
# body that is not JSON at all is FastAPI's 422 first, as on every route). JSON bodies are read whatever their
# Content-Type says, as they were before they were pydantic models: clients other than the extension send none.
router = APIRouter(prefix="/evidence", tags=["evidence"], dependencies=[Depends(check_origin), *deps.CLIENT],
                   strict_content_type=False)


async def _ledger(
    store: deps.Evidence,
    workspace: str = Query(DEFAULT_WORKSPACE_ID, min_length=1, max_length=MAX_WORKSPACE_ID,
                           description="The workspace (GET /evidence/workspaces); the default one when left out"),
) -> Ledger:
    """The ledger of the request's workspace; 404 for one that is not there."""
    return await store.ledger(workspace)


WorkspaceLedger = Annotated[Ledger, Depends(_ledger)]


# ----------------------------------------------------------------- bodies
def _trimmed(what: str, longest: int) -> AfterValidator:
    """A text trimmed, non-empty and at most ``longest`` characters."""
    def check(value: str) -> str:
        value = value.strip()
        if not value or len(value) > longest:
            raise ValueError(f"{what} must be 1-{longest} characters")
        return value
    return AfterValidator(check)


def _tags(value: List[str]) -> List[str]:
    """Tags, each trimmed with its inner whitespace collapsed, without repeats (case-insensitively; the first
    spelling stays)."""
    tags: List[str] = []
    seen = set()
    for tag in value:
        tag = " ".join(tag.split())
        if not tag or len(tag) > MAX_TAG:
            raise ValueError(f"each tag is 1-{MAX_TAG} characters")
        if tag.casefold() not in seen:
            seen.add(tag.casefold())
            tags.append(tag)
    if len(tags) > MAX_TAGS:
        raise ValueError(f"at most {MAX_TAGS} tags")
    return tags


class NewWorkspace(BaseModel):
    id: Optional[str] = Field(None, max_length=MAX_WORKSPACE_ID, description="Your application's id for it; unset: one is made")
    name: Annotated[str, _trimmed("workspace name", MAX_WORKSPACE_NAME)]


class NewCase(BaseModel):
    name: Annotated[str, _trimmed("case name", MAX_CASE_NAME)]


class Note(BaseModel):
    id: Optional[str] = Field(None, description="Unset for a new note")
    text: Annotated[str, _trimmed("each note's text", MAX_NOTE)]


class AnnotationBody(BaseModel):
    """A key left out keeps that list."""
    model_config = ConfigDict(extra="forbid")

    tags: Optional[Annotated[List[str], AfterValidator(_tags)]] = None
    notes: Optional[List[Note]] = Field(None, max_length=MAX_NOTES)


class Todo(BaseModel):
    id: Optional[str] = Field(None, description="Unset for a new todo")
    text: Annotated[str, _trimmed("each todo's text", MAX_TODO)]
    done: StrictBool = False


class TodosBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    todos: List[Todo] = Field(..., max_length=MAX_TODOS)


# -------------------------------------------------------------- workspaces
@router.get("/workspaces", **responses.answering(responses.EvidenceWorkspaces))
async def list_workspaces(store: deps.Evidence) -> Dict[str, Any]:
    return {"workspaces": await store.list_workspaces()}


@router.post("/workspaces", **responses.answering(responses.WorkspaceCreated), status_code=201, dependencies=deps.ADMIN)
async def create_workspace(body: NewWorkspace, store: deps.Evidence) -> Dict[str, Any]:
    """Make a workspace, with its own ledger: give the id of the workspace it stands for (your application's), or let
    one be made. 409 if the id is taken."""
    return {"workspace": await store.create_workspace(body.id or str(uuid.uuid4()), body.name)}


# ------------------------------------------------------------------ cases
@router.get("/cases", **responses.answering(responses.EvidenceCases))
async def list_cases(ledger: WorkspaceLedger) -> Dict[str, Any]:
    return {"cases": await ledger.list_cases()}


@router.post("/cases", **responses.answering(responses.CaseCreated))
async def create_case(body: NewCase, ledger: WorkspaceLedger) -> Dict[str, Any]:
    return {"case": await ledger.create_case(body.name)}


@router.get("/cases/{case_id}/captures", **responses.answering(responses.CaptureSummaries))
async def list_captures(case_id: str, ledger: WorkspaceLedger) -> Dict[str, Any]:
    return {"captures": await ledger.list_by_case(case_id)}


async def _read_multipart(request: Request) -> Tuple[Any, Dict[str, bytes]]:
    try:
        form = await request.form(max_part_size=MAX_METADATA_BYTES)
    except (MultiPartException, StarletteHTTPException) as e:  # Starlette's own 400 for a part too large
        reason = getattr(e, "message", None) or getattr(e, "detail", None) or str(e)
        raise Refused(f"invalid multipart body: {reason}") from None
    metadata: Any = None
    have_metadata = False
    files: Dict[str, bytes] = {}
    try:
        for name, value in form.multi_items():
            if name == "metadata":
                if have_metadata:
                    raise Refused("duplicate metadata field")
                have_metadata = True
                text = (await value.read()).decode("utf-8", "replace") if isinstance(value, UploadFile) else value
                try:
                    metadata = json.loads(text)
                except ValueError as e:
                    raise Refused(f"metadata is not JSON: {e}") from None
            elif name == "file":
                if not isinstance(value, UploadFile) or not value.filename:
                    raise Refused("file field without filename")
                if value.filename in files:
                    raise Refused(f"duplicate file {value.filename}")
                files[value.filename] = await value.read()
            else:
                raise Refused(f"unexpected field {name!r}")
    finally:
        # The uploads were spooled to temporary files; drop them.
        await form.close()
    if not have_metadata:
        raise Refused("missing metadata field")
    return metadata, files


# --------------------------------------------------------------- captures
@router.post("/captures")
async def create_capture(
    request: Request, ledger: WorkspaceLedger, tsa: deps.EvidenceTsa, enrichers: deps.OptionalEnrichers
) -> Dict[str, Any]:
    """Store a capture: verify the uploads, enrich, sign and chain the record, then time stamp it."""
    metadata, files = await _read_multipart(request)
    if not isinstance(metadata, dict):
        raise Refused("metadata must be a JSON object")
    case_id = metadata.get("caseId")
    if not isinstance(case_id, str) or not case_id:
        raise Refused("metadata.caseId is required")
    artifacts = verify_uploads(metadata, files)
    if not await ledger.case_exists(case_id):
        raise NotFound("unknown case")
    started = time.monotonic()
    found = await enrichment(enrichers, metadata, enabled=Config.EVIDENCE_ENRICH, timeout=Config.EVIDENCE_ENRICH_TIMEOUT)
    appended = await ledger.append(case_id, metadata, artifacts, found)
    canonical = appended.pop("recordJson")
    appended["timestamp"] = await timestamp_one(ledger, tsa, appended["captureId"], canonical, appended["recordHash"])
    logger.info("Evidence capture %s (workspace %s, seq %d) stored in %.1fs", appended["captureId"], ledger.workspace_id,
                appended["seq"], time.monotonic() - started)
    return appended


@router.get("/captures/{capture_id}", **responses.answering(responses.Capture))
async def get_capture(capture_id: str, ledger: WorkspaceLedger) -> Dict[str, Any]:
    row = await ledger.get(capture_id)
    if row is None:
        raise NotFound("unknown capture")
    return {
        "record": ledger.record_of(row),
        "recordHash": row["record_hash"],
        "signature": row["signature"],
        "timestamp": {"genTime": row.get("tsa_gen_time"), "error": row.get("tsa_error")},
    }


@router.get("/captures/{capture_id}/artifacts/{name}", response_class=Response)
async def get_artifact(capture_id: str, name: str, ledger: WorkspaceLedger):
    """One captured file, with the media type the record gives it."""
    row = await ledger.get(capture_id)
    if row is None:
        raise NotFound("unknown capture")
    record = ledger.record_of(row)
    entry = next((a for a in record.get("artifacts", []) if isinstance(a, dict) and a.get("name") == name), None)
    path = ledger.files.artifact_path(capture_id, name)
    if entry is None or path is None or not path.is_file():
        raise NotFound("unknown artifact")
    return FileResponse(path, media_type=entry.get("mediaType") or "application/octet-stream", filename=name)


# ------------------------------------------------------------ annotations
@router.get("/cases/{case_id}/annotations", **responses.answering(responses.Annotations))
async def list_annotations(case_id: str, ledger: WorkspaceLedger) -> Dict[str, Any]:
    """The tags and notes of the case's captures that have any."""
    return {"annotations": await ledger.annotations_by_case(case_id)}


@router.put("/captures/{capture_id}/annotations", **responses.answering(responses.Annotation))
async def annotate_capture(capture_id: str, body: AnnotationBody, ledger: WorkspaceLedger) -> Dict[str, Any]:
    """Replace the capture's tags and/or notes (a key left out keeps that list). Never touches the record."""
    notes = None if body.notes is None else [n.model_dump() for n in body.notes]
    return await ledger.annotate(capture_id, body.tags, notes)


# ------------------------------------------------------------------ todos
@router.get("/cases/{case_id}/todos", **responses.answering(responses.CaseTodos))
async def list_todos(case_id: str, ledger: WorkspaceLedger) -> Dict[str, Any]:
    """The case's to-do list, in its order; empty for a case that never had one."""
    return await ledger.todos_of(case_id)


@router.put("/cases/{case_id}/todos", **responses.answering(responses.CaseTodos))
async def set_todos(case_id: str, body: TodosBody, ledger: WorkspaceLedger) -> Dict[str, Any]:
    """Replace the case's to-do list (a todo left out is deleted). Never touches a record."""
    return await ledger.set_todos(case_id, [t.model_dump() for t in body.todos])


# ------------------------------------------------------------------ ledger
@router.get("/verify", **responses.answering(responses.VerifyReport))
async def verify(ledger: WorkspaceLedger) -> Dict[str, Any]:
    """Re-check every record of the workspace."""
    return await ledger.database.run(verify_all, ledger)


@router.get("/public-key", response_class=PlainTextResponse)
async def public_key(store: deps.Evidence):
    pem = await store.database.run(lambda: store.signer.public_pem)
    return PlainTextResponse(pem, media_type="application/x-pem-file")


@router.post("/timestamp-pending", **responses.answering(responses.TimestampRetry), dependencies=deps.ADMIN)
async def retry_timestamps(store: deps.Evidence, tsa: deps.EvidenceTsa) -> Dict[str, Any]:
    """Ask the TSA again for every record without a token, in every workspace."""
    if not tsa.enabled:
        raise Refused("timestamping is disabled: EVIDENCE_TSA_URL is empty")
    ok, failed = await timestamp_pending(store, tsa)
    return {"ok": ok, "failed": failed}
