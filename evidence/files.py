"""
Each capture's files on disk, under EVIDENCE_DIR/captures/<capture id>/:

    artifacts/<name>   the captured files, read-only
    record.json        the exact bytes that were hashed and signed
    record.sig         the raw 64-byte Ed25519 signature
    record.tsr         the RFC 3161 TimeStampResp, once granted

The directory is 0700 and the files 0444, so a stray edit fails loudly.
"""

import os
import shutil
from pathlib import Path
from typing import List, Optional

from .record import VerifiedArtifact, is_valid_artifact_name


def _write_readonly(path: Path, data: bytes) -> None:
    if path.exists():
        os.chmod(path, 0o644)
    path.write_bytes(data)
    os.chmod(path, 0o444)


class CaptureFiles:
    def __init__(self, root: Path):
        self.root = Path(root)

    @property
    def captures_dir(self) -> Path:
        return self.root / "captures"

    def capture_dir(self, capture_id: str) -> Path:
        return self.captures_dir / capture_id

    def artifact_path(self, capture_id: str, name: str) -> Optional[Path]:
        if not is_valid_artifact_name(name):
            return None
        return self.capture_dir(capture_id) / "artifacts" / name

    def write_capture(self, capture_id: str, canonical: bytes, signature: bytes, artifacts: List[VerifiedArtifact]) -> None:
        directory = self.capture_dir(capture_id)
        if directory.exists():
            raise FileExistsError(f"capture directory {directory} already exists")
        self.captures_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        artifact_dir = directory / "artifacts"
        artifact_dir.mkdir(parents=True)
        os.chmod(directory, 0o700)
        for a in artifacts:
            _write_readonly(artifact_dir / a.name, a.data)
        _write_readonly(directory / "record.json", canonical)
        _write_readonly(directory / "record.sig", signature)

    def write_timestamp(self, capture_id: str, response_der: bytes) -> None:
        _write_readonly(self.capture_dir(capture_id) / "record.tsr", response_der)

    def remove(self, capture_id: str) -> None:
        # The files are read-only; the directory's own mode governs their removal.
        shutil.rmtree(self.capture_dir(capture_id), ignore_errors=True)

    def read_record(self, capture_id: str) -> Optional[bytes]:
        try:
            return (self.capture_dir(capture_id) / "record.json").read_bytes()
        except OSError:
            return None

    def read_artifact(self, capture_id: str, name: str) -> Optional[bytes]:
        path = self.artifact_path(capture_id, name)
        if path is None:
            return None
        try:
            return path.read_bytes()
        except OSError:
            return None
