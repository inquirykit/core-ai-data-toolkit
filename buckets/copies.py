"""
Copying the files of a bucket to its remote (remotes.py), and reading one
back.

A bucket that names a remote has its files copied there, each to
``<workspace>/<bucket>/<file id>`` under the remote's root, by a task of the
kind "copy" on the task queue (tasks/runner.py). A copy is asked for
(``submit``) after each upload and each delete in such a bucket
(``changed``), when its remote is set, or named again while its copy is
behind (``set_remote``: the page's Copy now), when a remote that buckets
name is saved (``remote_saved``), and at startup and then hourly for each
bucket with a remote whose copy is behind (``start``): files waiting, or a
last copy that failed, was cancelled or waits for a retry. So files uploaded
while the remote failed are copied once it works again, at the latest an
hour after the last retry gave up, or after a restart. An admin's ask (the
remote set or saved) also starts at once a copy that waits for its retry.

A bucket has one copy at a time: the kind is ``one_per_ref`` on the queue,
by the bucket's ref (``<workspace>/<bucket>``). A change while its copy is
queued asks for that one; a change while it runs queues one more, which
waits for it without holding one of the kind's CONCURRENCY places from the
other buckets, and then copies what changed meanwhile.

A copy reads the bucket's files whose ``copied`` is null, by id, streams each
from disk to the remote on a thread (``Remotes.write_file``) and marks it
copied (naming.py's ``mark_copied``), one at a time: so nothing already
copied is copied again when a copy that failed or stopped runs again. It
marks them against the bucket's naming of its remote as it began
(``Namings.named``), which the spec it then reads is of: once the bucket's
remote is named again (another, none, or the same after it was changed
meanwhile), nothing more is marked, and the copy ends there; nothing is
deleted at the old one. Then it lists the remote's objects under the
bucket's prefix and deletes those whose file is gone here: a file deleted
here is deleted there. A remote that fails fails the task with its message,
which the runner retries RETRIES times, the wait doubling from
RETRY_DELAY_S. Dropping a bucket, or its workspace, leaves its copies at the
remote, as clearing its remote does: they are deleted at the remote, by
hand. A file whose bytes are missing from disk is skipped (and stays
waiting); one deleted while its copy is being written leaves an object the
next copy deletes, and a write that fails or is stopped part way deletes
what it sent.

A file whose bytes are missing from disk, in a bucket with a remote, that
was copied there, is read back (``fetch``, asked by files.py's ``locate``):
streamed into a temporary file in the bucket's folder on a thread, checked
against its row's size and SHA-256, and moved into place; then, if the file
was deleted (or its bucket dropped) meanwhile, its bytes go again.
"""

import asyncio
import hashlib
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

from core.errors import NotFound, Unavailable
from tasks.runner import CANCELLED, FAILED, QUEUED, Kind, TaskCancelled, TaskFailed, TaskRunner, public

from .files import FILE_ID, BucketFiles
from .naming import Naming, Namings
from .remotes import RemoteError, Remotes, Spec, Stopped, key_of

logger = logging.getLogger(__name__)

# The kind of task a copy is.
KIND = "copy"
# Copies under way at once, each of another bucket (a bucket's own changes go to its running copy).
CONCURRENCY = 2
# Times a copy that failed is tried again, and the wait before the first retry (doubling): 1, 2, 4, 8 and 16 minutes.
RETRIES = 5
RETRY_DELAY_S = 60.0
# Seconds between two looks for buckets whose copy is behind (``start``, hourly in api/app.py's lifespan).
CHECK_S = 3600.0
# Days an ended copy is kept (TaskRunner.prune deletes it then): a task is queued per change.
KEEP_DAYS = 7
# Files waiting read at a time; ids checked against the rows at a time, when objects with no file are looked for.
PAGE = 200
CHECK_BATCH = 1000
# The shortest time between two progress notes of a copy.
PROGRESS_S = 1.0

def _ref(workspace: str, bucket: str) -> str:
    return f"{workspace}/{bucket}"


class Copies:
    """The copies of the buckets' files at their remotes: the "copy" kind of task, and the reads back. Made with the
    files (``files``), the remotes, which remote each bucket names (``namings``) and the task queue; api/app.py
    tells the files and the remotes it is there (``files.copies``, ``remotes.copies``). Each copy is given
    ``retries`` and ``retry_delay_s``."""

    def __init__(self, files: BucketFiles, remotes: Remotes, namings: Namings, tasks: TaskRunner):
        self.files = files
        self.tables = files.tables
        self.remotes = remotes
        self.namings = namings
        self.tasks = tasks
        self.retries = RETRIES
        self.retry_delay_s = RETRY_DELAY_S
        # A running copy's task id -> set to stop it, between two pieces of a file.
        self._stops: Dict[str, threading.Event] = {}
        tasks.add(Kind(KIND, run=self._run, stop=self._stop, concurrency=CONCURRENCY, on_interrupt="resume",
                       keep_s=KEEP_DAYS * 86400, one_per_ref=True))

    # ------------------------------------------------------------ queueing
    async def submit(self, workspace: str, bucket: str, now: bool = False) -> None:
        """Have the bucket's files copied: by its copy queued already, else by a new one, which waits for the one
        running (``one_per_ref``). ``now`` (an admin's ask) starts a queued copy that waits for its retry as soon
        as there is room."""
        task = await self.tasks.submit(KIND, ref=_ref(workspace, bucket), params={"workspace": workspace,
                                                                                  "bucket": bucket},
                                       retries=self.retries, retry_delay_s=self.retry_delay_s)
        if now and task["status"] == QUEUED and (task["not_before"] or 0) > time.time():
            await self.tasks.due_now(task["id"])

    async def changed(self, workspace: str, bucket: str) -> None:
        """A file of the bucket was uploaded or deleted: ask for its copy, when it has a remote. One that cannot be
        asked for is logged, as the file is kept: the bucket's next change asks again, or the hourly look."""
        if await self.namings.remote_of(workspace, bucket) is None:
            return
        try:
            await self.submit(workspace, bucket)
        except Exception:
            logger.exception("Could not queue the copy of %s/%s to its remote: it is queued with the bucket's next "
                             "change, or within the hour", workspace, bucket)

    async def _behind(self, workspace: str, bucket: str) -> bool:
        """Whether the bucket's copy is behind: files wait, or its last copy failed, was cancelled or waits for a
        retry (a delete it may not have done at the remote)."""
        if await self.namings.uncopied(workspace, bucket, "", 1):
            return True
        last = await self.tasks.find(KIND, _ref(workspace, bucket))
        return last is not None and (last["status"] in (FAILED, CANCELLED)
                                     or (last["status"] == QUEUED and last["retried"] > 0))

    async def start(self) -> None:
        """At startup, once the task queue runs, then hourly (CHECK_S): ask for the copy of each bucket with a remote
        whose copy is behind (``_behind``), such as one whose retries all failed while its remote was down."""
        for refs in (await self.namings.buckets()).values():
            for ref in refs:
                workspace, bucket = ref.split("/", 1)
                try:
                    if await self._behind(workspace, bucket):
                        await self.submit(workspace, bucket)
                except Exception:
                    logger.exception("Could not queue the copy of %s to its remote", ref)

    async def remote_saved(self, name: str) -> None:
        """The remote was saved (its secrets' variables changed, say): ask now for the copies of the buckets that
        name it whose copy is behind, in case it works now."""
        for ref in (await self.namings.buckets()).get(name, []):
            workspace, bucket = ref.split("/", 1)
            try:
                if await self._behind(workspace, bucket):
                    await self.submit(workspace, bucket, now=True)
            except Exception:
                logger.exception("Could not queue the copy of %s to its remote %s", ref, name)

    # ------------------------------------------------------- a bucket's remote
    async def set_remote(self, workspace: str, bucket: str, remote: Optional[str]) -> Dict[str, Any]:
        """Name the bucket's remote, or none (None): its files are copied to a new one, every one of them again,
        and stay where they are at the one before. Naming the one it names already asks for its copy now when that
        is behind (``_behind``). NotFound for a remote that is not there. ``state`` after."""
        await self.tables.check_bucket(workspace, bucket)
        async with self.remotes.naming:
            if remote is not None:
                await self.remotes.get(remote)
            changed = await self.namings.name(workspace, bucket, remote)
        if remote is not None and (changed or await self._behind(workspace, bucket)):
            await self.submit(workspace, bucket, now=True)
        return await self.state(workspace, bucket)

    async def state(self, workspace: str, bucket: str) -> Dict[str, Any]:
        """The bucket's remote, its files, how many are copied there and waiting (null without a remote), and its
        copy running here, else its latest (null before the first)."""
        await self.tables.check_bucket(workspace, bucket)
        remote = await self.namings.remote_of(workspace, bucket)
        count = await self.tables.count(workspace, bucket)
        waiting = await self.tables.count(workspace, bucket, [("copied", "null")]) if remote is not None else None
        ref = _ref(workspace, bucket)
        running = self.tasks.running_on(KIND, ref)
        task = await self.tasks.lookup(running) if running is not None else None
        if task is None:
            task = await self.tasks.find(KIND, ref)
        return {"remote": remote, "count": count, "copied": None if waiting is None else max(count - waiting, 0),
                "waiting": waiting, "task": public(task) if task is not None else None}

    # ------------------------------------------------------------- copying
    async def _run(self, task: Dict[str, Any]) -> Dict[str, Any]:
        """One copy (its task's run): ``{remote, copied, deleted, skipped}``. TaskFailed with the remote's message,
        TaskCancelled once stopped."""
        workspace, bucket = task["params"]["workspace"], task["params"]["bucket"]
        done = {"remote": None, "copied": 0, "deleted": 0, "skipped": 0}
        stop = self._stops[task["id"]] = threading.Event()
        try:
            await self._copy(task["id"], workspace, bucket, stop, done)
            return done
        finally:
            self._stops.pop(task["id"], None)

    async def _stop(self, task: Dict[str, Any]) -> None:
        """Stop a running copy: the file being written is deleted at the remote, as far as it lets it."""
        stop = self._stops.get(task["id"])
        if stop is not None:
            stop.set()

    async def _copy(self, task_id: str, workspace: str, bucket: str, stop: threading.Event,
                    done: Dict[str, Any]) -> None:
        """One pass: the files waiting copied, then what was deleted here deleted there; counted in ``done``."""
        # The naming first, then the spec, which is then the one of that naming: a remote's place does not change
        # while a bucket names it (Remotes.put), so the copy writes where the files it marks are.
        naming = await self.namings.named(workspace, bucket)
        if naming is None:
            return  # its remote was cleared, or the bucket dropped: nothing to copy
        done["remote"] = naming[0]
        try:
            spec = await self.remotes.get(naming[0])
        except NotFound as e:
            raise TaskFailed(str(e), retry=False) from None
        try:
            op = await asyncio.to_thread(self.remotes.operator, spec)
            if await self._copy_files(task_id, spec, op, naming, workspace, bucket, stop, done):
                await self.tasks.progress(task_id, None, "deleting what was deleted here")
                done["deleted"] += await self._prune(spec, op, naming, workspace, bucket, stop)
        except (RemoteError, Unavailable) as e:
            # The remote's own message (a variable not set, among them): what the task fails with.
            raise TaskFailed(str(e)) from None
        except Stopped:
            raise TaskCancelled() from None
        except NotFound:
            # The bucket's workspace dropped meanwhile: the table store finds no workspace to read. Nothing more to
            # copy then, as when the bucket alone is dropped.
            try:
                await self.tables.check_bucket(workspace, bucket)
            except NotFound:
                return
            raise

    async def _copy_files(self, task_id: str, spec: Spec, op: Any, naming: Naming, workspace: str, bucket: str,
                          stop: threading.Event, done: Dict[str, Any]) -> bool:
        """Copy the files waiting, by id, marking each copied; counted in ``done``. False when the bucket's remote
        was named again meanwhile (or the bucket went)."""
        total = await self.tables.count(workspace, bucket, [("copied", "null")])
        after, noted, copied = "", 0.0, 0
        while rows := await self.namings.uncopied(workspace, bucket, after, PAGE):
            for row in rows:
                after = row["id"]
                if stop.is_set():
                    raise Stopped()
                if not await self._write(spec, op, workspace, bucket, row["id"], stop):
                    done["skipped"] += 1
                    continue
                if not await self.namings.mark_copied(workspace, bucket, [row["id"]], naming, time.time()):
                    return False
                done["copied"] += 1
                copied += 1
                if time.monotonic() - noted >= PROGRESS_S:
                    noted = time.monotonic()
                    await self.tasks.progress(task_id, min(copied / max(total, 1), 1.0),
                                                    f"copied {copied} of {total}")
        return await self._still_named(workspace, bucket, naming)

    async def _still_named(self, workspace: str, bucket: str, naming: Naming) -> bool:
        """Whether the bucket is there and names its remote as ``naming`` says (its record goes a moment after the
        bucket, when its files' code is told it was dropped)."""
        try:
            await self.tables.check_bucket(workspace, bucket)
        except NotFound:
            return False
        return await self.namings.named(workspace, bucket) == naming

    async def _write(self, spec: Spec, op: Any, workspace: str, bucket: str, file_id: str,
                     stop: threading.Event) -> bool:
        """Copy one file to the remote, on a thread; False when its bytes are not on disk (deleted since it was
        read, or removed by hand). Cancelled (the service stopping), the thread is told to stop too."""
        try:
            await asyncio.to_thread(self._write_file, spec, op, workspace, bucket, file_id, stop)
        except asyncio.CancelledError:
            stop.set()
            raise
        except FileNotFoundError:
            logger.info("The file %s of %s/%s has no bytes on disk: not copied to %s", file_id, workspace, bucket,
                        spec["name"])
            return False
        return True

    def _write_file(self, spec: Spec, op: Any, workspace: str, bucket: str, file_id: str,
                    stop: threading.Event) -> None:
        with open(self.files.path(workspace, bucket, file_id), "rb") as source:
            self.remotes.write_file(spec, op, key_of(workspace, bucket, file_id), source, stop)

    async def _prune(self, spec: Spec, op: Any, naming: Naming, workspace: str, bucket: str,
                     stop: threading.Event) -> int:
        """Delete the objects under the bucket's prefix at the remote whose file is gone here; how many. Only names
        a file of a bucket has (32 hex digits) are looked at, and nothing more is deleted once the bucket's remote
        is named again (another, or none), or the bucket is dropped: its copies stay then."""
        prefix = key_of(workspace, bucket)
        names = [n for n in await asyncio.to_thread(self.remotes.file_names, spec, op, prefix) if FILE_ID.fullmatch(n)]
        deleted = 0
        for start in range(0, len(names), CHECK_BATCH):
            batch = names[start:start + CHECK_BATCH]
            held = {row["id"] for row in await self.tables.bulk_get(workspace, bucket, batch)}
            gone = [name for name in batch if name not in held]
            # After the rows were read: a bucket dropped since reads as one with no files. Named again during a
            # batch, its objects of files deleted here still go.
            if gone and not await self._still_named(workspace, bucket, naming):
                return deleted
            for name in gone:
                if stop.is_set():
                    raise Stopped()
                await asyncio.to_thread(self.remotes.delete_file, spec, op, prefix + name)
                deleted += 1
        return deleted

    # ---------------------------------------------------------- reading back
    async def fetch(self, workspace: str, bucket: str, row: Dict[str, Any]) -> bool:
        """Read the file's bytes back from the bucket's remote into their place on disk: True once they are there;
        False for a bucket without a remote, or a file not copied to it. Unavailable when the remote fails, or what
        it holds is not the file (its size or SHA-256 differs); NotFound when the file was deleted, or its bucket
        dropped, while its bytes came back (which then go too)."""
        remote = await self.namings.remote_of(workspace, bucket)
        if remote is None or row.get("copied") is None:
            return False
        try:
            spec = await self.remotes.get(remote)
            path = await asyncio.to_thread(self._fetch, spec, workspace, bucket, row)
        except (NotFound, Unavailable) as e:
            raise Unavailable(f"The bytes of the file {row['id']} of {workspace}/{bucket} are missing from disk, "
                              f"and reading them back from its remote failed: {e}") from None
        except FileNotFoundError:
            # The bucket's folder went as its bytes came: dropped meanwhile.
            raise NotFound(f"No bucket {bucket!r} in {workspace!r}: it was dropped while a file of it was read back "
                           "from its remote") from None
        # A delete takes the row, then the bytes: one that took the row as these came finds no bytes yet to take.
        if not await self.tables.bulk_get(workspace, bucket, [row["id"]]):
            await asyncio.to_thread(path.unlink, missing_ok=True)
            raise NotFound(f"No file {row['id']!r} in {workspace}/{bucket}: it was deleted while it was read back "
                           "from its remote")
        logger.info("Read the file %s of %s/%s back from the remote %s", row["id"], workspace, bucket, remote)
        return True

    def forget(self, workspace: str, buckets: Sequence[str]) -> None:
        """Forget the remotes of these buckets of the workspace: dropped, or about to be made. Blocking."""
        self.namings.forget(workspace, buckets)

    def _fetch(self, spec: Spec, workspace: str, bucket: str, row: Dict[str, Any]) -> Path:
        """The bytes read back into place: their path. Blocking."""
        op = self.remotes.operator(spec)
        out, temp, final = self.files.open_new(workspace, bucket, row["id"])
        hasher = hashlib.sha256()
        try:
            with out:
                size = self.remotes.read_file(spec, op, key_of(workspace, bucket, row["id"]), out, hasher)
            if (size, hasher.hexdigest()) != (row["size"], row["sha256"]):
                what = "size" if size != row["size"] else "SHA-256"
                raise RemoteError(f"what the remote {spec['name']} holds is not the file: its {what} differs")
            os.replace(temp, final)
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
        return final
