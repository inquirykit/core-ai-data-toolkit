"""/evidence: workspaces, cases, captures, annotations, todos and the verify report."""

from typing import List, Literal, Optional


from .common import Json, Out


class EvidenceWorkspace(Out):
    id: str
    name: str
    createdAt: str


class EvidenceWorkspaces(Out):
    workspaces: List[EvidenceWorkspace]


class WorkspaceCreated(Out):
    workspace: EvidenceWorkspace


class EvidenceCase(Out):
    id: str
    name: str
    createdAt: str


class EvidenceCases(Out):
    cases: List[EvidenceCase]


class CaseCreated(Out):
    case: EvidenceCase


class CaptureSummary(Out):
    seq: int
    captureId: str
    receivedAt: str
    url: Optional[str]
    title: Optional[str]
    recordHash: str
    # The TSA's genTime, once a token was granted
    timestampedAt: Optional[str]


class CaptureSummaries(Out):
    captures: List[CaptureSummary]


class CaptureTimestamp(Out):
    genTime: Optional[str]
    error: Optional[str]


class TimestampRetry(Out):
    """POST /evidence/timestamp-pending: the records granted a token, and those still without one."""
    ok: int
    failed: int


class Capture(Out):
    # The signed record (evidence/record.py), as its canonical JSON says
    record: Json
    recordHash: str
    signature: str
    timestamp: CaptureTimestamp


class AnnotationNote(Out):
    id: str
    text: str
    createdAt: str
    updatedAt: Optional[str]


class Annotation(Out):
    """A capture's tags and notes, added after it was taken: mutable, and never part of the signed record."""
    captureId: str
    tags: List[str]
    notes: List[AnnotationNote]
    updatedAt: str


class Annotations(Out):
    annotations: List[Annotation]


class Todo(Out):
    id: str
    text: str
    done: bool
    createdAt: str
    # When the text last changed
    updatedAt: Optional[str]
    # When it was checked; null while open
    doneAt: Optional[str]


class CaseTodos(Out):
    """A case's to-do list: mutable, and never part of a signed record."""
    caseId: str
    todos: List[Todo]
    # null for a case whose list was never saved
    updatedAt: Optional[str]


class VerifyIssue(Out):
    severity: Literal["error", "warning"]
    seq: int
    captureId: str
    message: str


class VerifyReport(Out):
    # The workspace whose ledger was checked
    workspace: str
    ok: bool
    records: int
    timestamped: int
    headHash: Optional[str]
    signingKeySha256: str
    issues: List[VerifyIssue]
