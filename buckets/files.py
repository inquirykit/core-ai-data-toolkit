"""
The files of the buckets: each a row of its bucket (a /db table made with
``bucket``; its fields are db/schema.py's ``BUCKET``: name, size, sha256,
content_type, created) and its bytes on disk, one file per row, at
``DATA_DIR/files/<workspace>/<bucket>/<row id>``. Only this writes a bucket's
rows (``owner=FILES``; /db refuses every other write), so a row and its
bytes go together. A delete removes the file: files are not stored by hash,
so two uploads of the same bytes are two files.

A row's id is made here (uuid4, 32 hex digits), never given by a client, so
a file's path never leaves its bucket's folder; every path is still
resolved and checked to stay inside that folder before anything reads or
deletes it. A file's name is what the client called it, 1 to 255
characters without control characters; names need not be unique.

An upload streams: the request's body is written as it arrives to a
temporary file in the bucket's folder (``.upload-...``), off the event
loop, hashed and counted on the way, and refused past ``max_bytes``
(MAX_FILE_BYTES; at once when the request says its length). Whole, it is
moved to its path and its row is written; if either fails, neither stays,
and a request that stops on the way (refused, failed, a client gone) leaves
no temporary file behind.

The table store asks ``check_new`` before it makes a bucket, and tells
``dropped`` after it drops buckets (db/tables.py's FileKeeper), which
removes their folders: api/app.py gives it this. This never reads the
settings: api/app.py passes the folder and MAX_FILE_BYTES.

A bucket may name a remote (remotes.py), which its files are copied to
(copies.py): each upload and each delete tells ``copies``, which queues the
bucket's copy, and a file whose bytes are missing from disk is read back
from the remote when it was copied there (``locate``).
"""

import asyncio
import hashlib
import logging
import os
import re
import shutil
import stat
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, AsyncIterator, BinaryIO, Dict, List, Optional, Sequence, Tuple

from core.errors import Conflict, NotFound, Refused, TooLarge
from db.tables import FILES, MAX_FILE_NAME, TableError, TableStore

logger = logging.getLogger(__name__)

# Bytes gathered from the request before each write: a thread hop per mebibyte, not per network read.
WRITE_BYTES = 1024 * 1024
# A file's id, the row's: made here, never given by a client.
FILE_ID = re.compile(r"[0-9a-f]{32}")
# The start of an upload's temporary file's name, in its bucket's folder while its bytes arrive.
UPLOADING = ".upload-"
# What a file is when its upload says no media type.
DEFAULT_TYPE = "application/octet-stream"
MAX_CONTENT_TYPE = 255
CONTROL = re.compile(r"[\x00-\x1f\x7f]")

Row = Dict[str, Any]


def check_file_name(name: Any) -> str:
    """The name of a file as uploaded: 1 to MAX_FILE_NAME characters, not only spaces, no control characters
    (TableError otherwise)."""
    if not isinstance(name, str) or not name.strip():
        raise TableError(f"A file's name is 1 to {MAX_FILE_NAME} characters")
    if len(name) > MAX_FILE_NAME:
        raise TableError(f"A file's name is at most {MAX_FILE_NAME} characters, not {len(name)}")
    if CONTROL.search(name):
        raise TableError("A file's name holds no control characters (a new line, a tab...)")
    return name


def check_content_type(value: Optional[str]) -> str:
    """The media type an upload says its bytes are, as it says it (application/octet-stream when it says none):
    printable ASCII, at most MAX_CONTENT_TYPE characters, as it is given back on every download."""
    value = (value or "").strip()
    if not value:
        return DEFAULT_TYPE
    if len(value) > MAX_CONTENT_TYPE or not value.isascii() or CONTROL.search(value):
        raise TableError(f"A file's Content-Type is printable ASCII, at most {MAX_CONTENT_TYPE} characters")
    return value


def check_file_id(file_id: Any) -> str:
    """A file's id as its upload gave it: 32 hex digits (TableError otherwise)."""
    if not isinstance(file_id, str) or not FILE_ID.fullmatch(file_id):
        raise TableError("Bad file id: a file's id is the 32 hex digits its upload gave it")
    return file_id


def _write(out: BinaryIO, hasher: Any, parts: List[bytes]) -> None:
    data = b"".join(parts)
    hasher.update(data)
    out.write(data)


def _finish(out: BinaryIO, hasher: Any, parts: List[bytes]) -> None:
    """Write what is left, and close the file."""
    with out:
        _write(out, hasher, parts)


def _discard(out: BinaryIO, path: Path) -> None:
    """Close and remove an upload's temporary file."""
    try:
        out.close()
    finally:
        path.unlink(missing_ok=True)


class BucketFiles:
    """The buckets' files, kept in ``root`` (DATA_DIR/files), each at most ``max_bytes`` (MAX_FILE_BYTES); their rows
    through ``tables``. Its async methods never block the event loop."""

    def __init__(self, tables: TableStore, root: Path, max_bytes: int):
        self.tables = tables
        self.root = Path(root).resolve()
        self.max_bytes = max_bytes
        # What copies the files to their buckets' remotes and reads them back (copies.py; api/app.py sets it).
        self.copies: Any = None

    # ------------------------------------------------------------ paths
    def _inside(self, path: Path) -> Path:
        """``path`` resolved (links followed), once it is known to stay below ``root``. Blocking."""
        resolved = path.resolve()
        if resolved == self.root or not resolved.is_relative_to(self.root):
            raise Refused(f"{path} is not inside the buckets' folder")
        return resolved

    def folder(self, workspace: str, bucket: str) -> Path:
        """The folder of the bucket's files. Blocking."""
        return self._inside(self.root / workspace / bucket)

    def path(self, workspace: str, bucket: str, file_id: str) -> Path:
        """Where the file's bytes are. Blocking."""
        return self._inside(self.root / workspace / bucket / check_file_id(file_id))

    # ------------------------------------------------------ the table store's
    def check_new(self, workspace: str, bucket: str) -> None:
        """Conflict when the folder a new bucket would keep its files in holds files already: left there by hand,
        or by a bucket whose files could not be removed when it was dropped. A remote a bucket of that name named
        is forgotten, so the new one names none. Blocking."""
        folder = self.folder(workspace, bucket)
        try:
            with os.scandir(folder) as entries:
                held = any(True for _ in entries)
        except FileNotFoundError:
            held = False
        except NotADirectoryError:
            held = True
        if held:
            raise Conflict(f"The folder of {workspace}/{bucket} in DATA_DIR/files holds files already: move them away (or "
                           "delete them), then make the bucket")
        if self.copies is not None:
            self.copies.forget(workspace, [bucket])

    def dropped(self, workspace: str, buckets: Sequence[str]) -> None:
        """Remove the folders of these buckets of the workspace, dropped, then the workspace's folder if that left
        it empty, and forget their remotes. A bucket whose folder cannot be removed is logged, and the others are
        still removed. Blocking."""
        if self.copies is not None:
            try:
                self.copies.forget(workspace, buckets)
            except Exception:
                logger.exception("Could not forget the remotes of the buckets %s of %s, dropped", ", ".join(buckets),
                                 workspace)
        for bucket in buckets:
            try:
                shutil.rmtree(self.folder(workspace, bucket))
            except FileNotFoundError:
                pass
            except Exception:
                logger.exception("Could not remove the files of the bucket %s/%s, dropped: remove its folder in "
                                 "DATA_DIR/files by hand", workspace, bucket)
        try:
            os.rmdir(self._inside(self.root / workspace))
        except OSError:
            pass  # not there, or still holding another bucket's files

    # ----------------------------------------------------------------- files
    async def upload(self, workspace: str, bucket: str, name: str, content_type: Optional[str],
                     body: AsyncIterator[bytes], length: Optional[int] = None) -> Row:
        """Keep the bytes ``body`` yields as a new file of the bucket named ``name``, of ``content_type``: its row,
        once the bytes are on disk. ``length`` is the size the request says (its Content-Length), which past
        ``max_bytes`` is refused before a byte is read (TooLarge); a body that turns out longer is refused when it
        gets there. Nothing stays of an upload that does not end with its row."""
        name = check_file_name(name)
        content_type = check_content_type(content_type)
        await self.tables.check_bucket(workspace, bucket)
        if length is not None and length > self.max_bytes:
            raise TooLarge(self._too_large(f"{length} bytes"))
        row_id = uuid.uuid4().hex
        out, temp, final = await asyncio.to_thread(self.open_new, workspace, bucket, row_id)
        hasher = hashlib.sha256()
        size = 0
        try:
            held: List[bytes] = []
            waiting = 0
            async for chunk in body:
                size += len(chunk)
                if size > self.max_bytes:
                    raise TooLarge(self._too_large("more"))
                held.append(chunk)
                waiting += len(chunk)
                if waiting >= WRITE_BYTES:
                    await asyncio.to_thread(_write, out, hasher, held)
                    held, waiting = [], 0
            await asyncio.to_thread(_finish, out, hasher, held)
        except BaseException:
            # Run to its end on its thread even if this request is cancelled meanwhile.
            await asyncio.to_thread(_discard, out, temp)
            raise
        row = {"id": row_id, "name": name, "size": size, "sha256": hasher.hexdigest(), "content_type": content_type,
               "created": time.time()}
        # Shielded: a request cancelled now (a client gone) still ends with the row and its bytes, or neither.
        return await asyncio.shield(asyncio.ensure_future(self._commit(workspace, bucket, row, temp, final)))

    def _too_large(self, size: str) -> str:
        return f"The file is larger than MAX_FILE_BYTES ({self.max_bytes} bytes): {size}"

    def open_new(self, workspace: str, bucket: str, file_id: str) -> Tuple[BinaryIO, Path, Path]:
        """A new temporary file in the bucket's folder (made if need be), and the path its bytes go to: for an upload,
        and for bytes read back from the remote. Blocking."""
        folder = self.folder(workspace, bucket)
        folder.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(dir=folder, prefix=UPLOADING)
        return os.fdopen(fd, "wb"), Path(temp), self.path(workspace, bucket, file_id)

    async def _commit(self, workspace: str, bucket: str, row: Row, temp: Path, final: Path) -> Row:
        """Move the bytes to their path, then write their row; if either fails, neither stays. Then the bucket's
        copy is queued, when it has a remote."""
        try:
            await asyncio.to_thread(os.replace, temp, final)
        except BaseException as e:
            await asyncio.to_thread(temp.unlink, missing_ok=True)
            if isinstance(e, FileNotFoundError):
                raise NotFound(f"No bucket {bucket!r} in {workspace!r}: it was dropped during the upload") from None
            raise
        try:
            row = await self.tables.insert(workspace, bucket, row, owner=FILES)
        except BaseException:
            await asyncio.to_thread(final.unlink, missing_ok=True)
            raise
        await self._changed(workspace, bucket)
        return row

    async def _changed(self, workspace: str, bucket: str) -> None:
        """Tell what copies the files (if anything does) that the bucket's files changed."""
        if self.copies is not None:
            await self.copies.changed(workspace, bucket)

    async def locate(self, workspace: str, bucket: str, file_id: str) -> Tuple[Row, Path, os.stat_result]:
        """The file's row, the path of its bytes and their stat, for a download. Bytes missing from disk are read
        back from the bucket's remote when they were copied there (copies.py's ``fetch``: Unavailable when that
        fails). NotFound when the bucket has no such row, or the row's bytes are missing from disk and from any
        remote (which the message says), or the file was deleted, or its bucket dropped, while they came back."""
        file_id = check_file_id(file_id)
        await self.tables.check_bucket(workspace, bucket)
        rows = await self.tables.bulk_get(workspace, bucket, [file_id])
        if not rows:
            raise NotFound(f"No file {file_id!r} in {workspace}/{bucket}")
        try:
            path, found = await asyncio.to_thread(self._stat, workspace, bucket, file_id)
        except NotFound:
            if self.copies is None or not await self.copies.fetch(workspace, bucket, rows[0]):
                raise
            path, found = await asyncio.to_thread(self._stat, workspace, bucket, file_id)
        return rows[0], path, found

    def _stat(self, workspace: str, bucket: str, file_id: str) -> Tuple[Path, os.stat_result]:
        path = self.path(workspace, bucket, file_id)
        try:
            found = path.stat()
        except FileNotFoundError:
            found = None
        if found is None or not stat.S_ISREG(found.st_mode):
            raise NotFound(f"The file {file_id} of {workspace}/{bucket} has its row, but its bytes are missing from "
                           "disk (DATA_DIR/files)")
        return path, found

    async def delete(self, workspace: str, bucket: str, file_id: str) -> None:
        """Delete the file: its row, then its bytes, then (the bucket's copy, queued) its copy at the bucket's
        remote. A file that is not there is no error; bytes that cannot be removed are logged, as the row is gone
        already."""
        file_id = check_file_id(file_id)
        await self.tables.check_bucket(workspace, bucket)
        path = await asyncio.to_thread(self.path, workspace, bucket, file_id)
        await self.tables.delete(workspace, bucket, [file_id], owner=FILES)
        try:
            await asyncio.to_thread(path.unlink, missing_ok=True)
        except Exception:
            logger.exception("Deleted the file %s of %s/%s, but could not remove its bytes (%s)", file_id, workspace,
                             bucket, path)
        await self._changed(workspace, bucket)
