"""
Remotes: places outside the service that a bucket's files are copied to (S3,
and what speaks its API: R2, B2, MinIO; B2's own API, GCS, Azure Blob, a
folder...), through OpenDAL (https://opendal.apache.org), which is imported
only by the calls that use it, so a service without remotes never loads it.

A remote is a spec the admin writes (``RemoteSpec``): its name, OpenDAL's
``scheme`` and its ``options`` (text: bucket, endpoint, region, root...),
and ``secrets``, which name for each secret option the environment variable
holding its value. Specs are kept in the database (``tk_remote``, a type of
the service's, not a /db workspace, so no name an admin made clashes with
it). A secret's value is never stored, answered or logged: it is read from
the service's environment each time the remote is used, and taken out of
every error message. An option in ``options`` whose name looks like a
secret's (``SECRET_WORDS``: a connection string among them, which holds a
password), or whose value is a URL with a password in it, is refused, so a
key never lands in the database. A child process of the service never gets
a variable a remote names (core/procs.py, child_env). A folder's (``fs``) root is never the buckets' folder (DATA_DIR/files),
inside it or above it, where its copies would be written over the files
they copy (``files_root``).

A bucket names its remote in naming.py (``Namings``), and copies.py copies
its files there. While a bucket names a remote, it
cannot be deleted, nor what says where its copies are changed (Conflict:
``_place``, its scheme, its options, and the variables its options that are
not secrets are read from), which the buckets' ``copied`` would no longer
tell. Its label and the variables its secret options come from (a key
rotated) can.
Saving a remote that buckets name asks for their copies (``copies``,
copies.py's, which api/app.py sets), in case it works now.

OpenDAL's calls block, and their exceptions share no base class: every call
here runs off the event loop (the async methods take a thread; the blocking
ones are for copies.py's threads) inside ``_calling``, which makes any
exception a ``RemoteError`` (Unavailable, 503) with the remote's own message,
its secrets taken out. One OpenDAL operator is kept per remote and made anew
when its scheme, options or secrets' values change, so a remote of the
``memory`` scheme keeps its objects while the service runs.
"""

import asyncio
import hashlib
import json
import os
import re
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Any, BinaryIO, Dict, Iterator, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from core.errors import Conflict, NotFound, Refused, Unavailable
from db import Database, on_pool
from db.specs import SpecStore

from .naming import Namings

REMOTE = "tk_remote"
NAME = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
SCHEME = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
OPTION = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
VARIABLE = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")
# Words that make an option's name look like a secret's: its value is named as an environment variable instead.
# A connection string (mongodb, gridfs, postgresql, mysql...) holds the password of the place it names.
SECRET_WORDS = ("key", "secret", "password", "token", "credential", "connection_string")
# A URL with a password in it (scheme://user:password@host): refused in an option's value, as a secret is.
URL_PASSWORD = re.compile(r"[a-z][a-z0-9+.-]*://[^/?#@\s]*:[^/?#@\s]*@", re.IGNORECASE)
MAX_OPTIONS = 32
MAX_VALUE = 2048
# Bytes read from a file, or from a remote, at a time; and the size of each part a writer sends (S3's parts are at
# least 5 MiB, but for the last), so a file is never held in memory whole.
READ_BYTES = 1024 * 1024
WRITE_CHUNK = 8 * 1024 * 1024
# Where the test route writes its probe object: no workspace's name starts with a dot, so no bucket's copies are there.
PROBE_DIR = ".toolkit-probe/"
# The longest remote's message an error carries.
MAX_MESSAGE = 600

OptionValue = Annotated[str, StringConstraints(max_length=MAX_VALUE)]
Variable = Annotated[str, StringConstraints(pattern=VARIABLE.pattern)]
Spec = Dict[str, Any]


class RemoteSpec(BaseModel):
    """A remote, as the admin writes it and the API answers it: OpenDAL's scheme and options, and the environment
    variables its secret options are read from."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., pattern=NAME.pattern, description="Lower case letters, digits and _, starting with a letter")
    scheme: str = Field(..., pattern=SCHEME.pattern,
                        description="OpenDAL's service: s3 (also R2, B2's S3 API, MinIO), b2, gcs, azblob, fs (a "
                                    "folder), memory (gone with the process: for trying it out)...")
    options: Dict[str, OptionValue] = Field(
        default_factory=dict, max_length=MAX_OPTIONS,
        description="OpenDAL's options for the scheme, as text: bucket, endpoint, region, root (the folder the "
                    "copies go under)... Never a secret: name its variable in secrets")
    secrets: Dict[str, Variable] = Field(
        default_factory=dict, max_length=MAX_OPTIONS,
        description="Each secret option, by the name of the environment variable of the service that holds its "
                    "value: read when the remote is used, never stored or shown, and kept from jobs and model servers")
    label: Optional[str] = Field(None, min_length=1, max_length=200, description="What it is, in words")

    @field_validator("options", "secrets")
    @classmethod
    def _option_names(cls, value: Dict[str, str]) -> Dict[str, str]:
        # Checked here, not in the schema's types, so the schema's map is one of text a form edits as such.
        for option in value:
            if not OPTION.fullmatch(option):
                raise ValueError(f"{option!r}: an option's name is lower case letters, digits and _, as OpenDAL's are")
        return value


class RemoteError(Unavailable):
    """A remote's call failed: its own message, its secrets taken out."""


class Stopped(Exception):
    """A copy stopped part way, for a cancel or the service stopping: the file being written is deleted at the
    remote, as far as it lets it (``Remotes.write_file``)."""


def looks_secret(option: str) -> bool:
    """Whether an option's name says it holds a secret (``SECRET_WORDS``): access_key_id, secret_access_key,
    password, token, credential, connection_string..."""
    return any(word in option.lower() for word in SECRET_WORDS)


def check(spec: RemoteSpec) -> None:
    """Refused for an option given in ``options`` that looks like a secret, or whose value is a URL with a password,
    or one given twice."""
    for option, value in spec.options.items():
        if looks_secret(option):
            raise Refused(f"The option {option} looks like a secret: name the environment variable that holds it in "
                          f"secrets ({{\"{option}\": \"MY_VARIABLE\"}}) instead of giving its value in options")
        if URL_PASSWORD.search(value):
            raise Refused(f"The option {option} is a URL with a password in it: name the environment variable that "
                          f"holds it in secrets ({{\"{option}\": \"MY_VARIABLE\"}}) instead of giving its value in "
                          "options")
    both = sorted(set(spec.options) & set(spec.secrets))
    if both:
        raise Refused(f"{', '.join(both)} given in options and in secrets: give each option once")


def _place(spec: Spec) -> Tuple[Any, ...]:
    """What says where a remote's copies are: its scheme, its options, and the variables of its secrets whose
    option is not a secret's (a root or a bucket read from the environment). A key's variable is not in it, so a key
    can be rotated while buckets name the remote."""
    return (spec["scheme"], spec["options"], {o: v for o, v in spec["secrets"].items() if not looks_secret(o)})


def _said(error: BaseException, secrets: List[str]) -> str:
    """An OpenDAL error in one line: its first line and its source (the context between them names the call, which
    the step does too), each secret's value taken out."""
    lines = [line.strip() for line in str(error).splitlines()]
    text = next((line for line in lines if line), "") or type(error).__name__
    if "Source:" in lines:
        source = [line for line in lines[lines.index("Source:") + 1:] if line]
        if source:
            text += ": " + " ".join(source)
    for value in secrets:
        if value:
            text = text.replace(value, "***")
    return text[:MAX_MESSAGE]


def missing(spec: Spec) -> List[str]:
    """The environment variables the remote's secrets name that the service's environment does not set."""
    return sorted({v for v in spec["secrets"].values() if not os.environ.get(v)})


class Remotes(SpecStore):
    """The remotes' specs, in ``tk_remote`` (db/specs.py: the service's own, scope ""), and their calls through
    OpenDAL. ``namings`` says which buckets name each; ``files_root`` is the buckets' folder, which no folder remote's
    root may overlap."""

    TABLE = REMOTE

    def __init__(self, database: Database, namings: Namings, files_root: Optional[Path] = None):
        super().__init__(database)
        self.namings = namings
        self.files_root = Path(files_root).resolve() if files_root is not None else None
        # What copies the buckets' files (copies.py; api/app.py sets it): asked for the copies of the buckets that
        # name a remote once it is saved.
        self.copies: Any = None
        # A remote's name -> (what it was made from, as a hash: its scheme, options and secrets' values; its operator).
        self._operators: Dict[str, Tuple[str, Any]] = {}
        self._operators_lock = threading.Lock()
        # Held while a remote is deleted, or named by a bucket: so one is never named as it goes.
        self.naming = asyncio.Lock()

    # ------------------------------------------------------------------ specs
    def _specs(self) -> List[Spec]:
        return self._all("")

    def _named(self, name: str) -> Optional[Spec]:
        return self._find("", name)

    find = on_pool(_named)

    async def get(self, name: str) -> Spec:
        """The remote's spec, with when it was made and changed; NotFound for none."""
        spec = await self.find(name)
        if spec is None:
            raise NotFound(f"No remote named {name!r}")
        return spec

    def _describe(self, spec: Spec, named: Dict[str, List[str]]) -> Dict[str, Any]:
        return {**spec, "buckets": named.get(spec["name"], []), "missing": missing(spec)}

    async def list(self) -> List[Dict[str, Any]]:
        """Every remote: its spec, the buckets that name it, and the variables its secrets name that are not set."""
        named = await self.namings.buckets()
        return [self._describe(spec, named) for spec in await self._run(self._specs)]

    async def describe(self, name: str) -> Dict[str, Any]:
        return self._describe(await self.get(name), await self.namings.buckets())

    def _put(self, spec: RemoteSpec) -> None:
        self._save("", spec.name, spec.model_dump(mode="json"))

    async def put(self, spec: RemoteSpec) -> Dict[str, Any]:
        """Create or replace the remote, ``check``ed first, a folder's root too (``_check_root``). Conflict for a
        change of where its copies are (``_place``) while a bucket names it. Then the copies of the buckets that name
        it are asked for, in case it works now."""
        check(spec)
        if spec.scheme == "fs" and "root" in spec.options:
            self._check_root(spec.name, spec.options["root"], Refused)
        async with self.naming:
            before = await self.find(spec.name)
            if before is not None and _place(before) != _place(spec.model_dump(mode="json")):
                buckets = (await self.namings.buckets()).get(spec.name, [])
                if buckets:
                    raise Conflict(f"The buckets {', '.join(buckets)} keep copies at the remote {spec.name}: what says "
                                   "where they are (its scheme, its options, and the variables its options that are "
                                   "not secrets are read from) cannot change while a bucket names it. Make a new remote "
                                   "and name it on those buckets instead (their files are copied to it)")
            await self._run(self._put, spec)
        if self.copies is not None:
            await self.copies.remote_saved(spec.name)
        return await self.describe(spec.name)

    def _check_root(self, name: str, root: str, error: type) -> None:
        """``error`` for a folder remote's root that is the buckets' folder, inside it or above it: its copies would be written
        over the files they copy, or among them. A relative root is the service's working directory's, as OpenDAL
        takes it."""
        if self.files_root is None:
            return
        where = Path(root).resolve()
        if where == self.files_root or where.is_relative_to(self.files_root) or self.files_root.is_relative_to(where):
            raise error(f"The remote {name}'s root {root} is the buckets' folder ({self.files_root}), inside it or above it, "
                        "where its copies would be written over the files they copy: give it a folder outside it")

    async def delete(self, name: str) -> None:
        """Delete the remote; Conflict while a bucket names it (its copies stay where they are)."""
        async with self.naming:
            await self.get(name)
            buckets = (await self.namings.buckets()).get(name, [])
            if buckets:
                raise Conflict(f"The remote {name} is named by the buckets {', '.join(buckets)}: set theirs to "
                               "another, or none, first")
            await self._run(self._remove, "", name)
        with self._operators_lock:
            self._operators.pop(name, None)

    # --------------------------------------------------------------- calling
    @staticmethod
    def _options(spec: Spec) -> Dict[str, str]:
        """The remote's options with its secrets' values, from the service's environment: Unavailable naming a
        variable that is not set."""
        options = dict(spec["options"])
        for option, variable in spec["secrets"].items():
            value = os.environ.get(variable)
            if not value:
                raise Unavailable(f"{variable} is not set in the service's environment: the remote {spec['name']} "
                                  f"reads its {option} from it")
            options[option] = value
        return options

    @contextmanager
    def _calling(self, spec: Spec, what: str) -> Iterator[None]:
        """Any exception an OpenDAL call raises (they share no base class) as a RemoteError saying ``what`` failed,
        with the remote's message, its secrets taken out. Stopped goes through."""
        try:
            yield
        except (Stopped, RemoteError):
            raise
        except Exception as e:
            values = [os.environ.get(v, "") for v in spec["secrets"].values()]
            raise RemoteError(f"The remote {spec['name']} could not {what}: {_said(e, values)}") from None

    def operator(self, spec: Spec) -> Any:
        """The remote's OpenDAL operator: the one kept for it, unless its scheme, options or secrets' values have
        changed since it was made. RemoteError for a folder whose root (read from a variable, say) overlaps
        the buckets' folder. Blocking."""
        import opendal

        options = self._options(spec)
        if spec["scheme"] == "fs" and "root" in options:
            self._check_root(spec["name"], options["root"], RemoteError)
        made_of = hashlib.sha256(json.dumps([spec["scheme"], options], sort_keys=True).encode()).hexdigest()
        with self._operators_lock:
            held = self._operators.get(spec["name"])
            if held is not None and held[0] == made_of:
                return held[1]
            with self._calling(spec, f"be opened as {spec['scheme']}"):
                op = opendal.Operator(spec["scheme"], **options)
            self._operators[spec["name"]] = (made_of, op)
            return op

    def _test(self, spec: Spec) -> List[Dict[str, Any]]:
        """Write a probe object, read it back, find it in a list and delete it: each step with its time. RemoteError
        for the first that fails, saying what worked before it. Blocking."""
        op = self.operator(spec)
        path = f"{PROBE_DIR}{uuid.uuid4().hex}"
        data = f"A probe of the remote {spec['name']}, written by the toolkit's test and deleted at once".encode()

        def read() -> None:
            if bytes(op.read(path)) != data:
                raise ValueError("the probe object read back is not what was written")

        def listed() -> None:
            if not any(entry.path == path for entry in op.list(PROBE_DIR)):
                raise ValueError(f"the probe object written is not in the list of {PROBE_DIR}")

        steps: List[Dict[str, Any]] = []
        calls = [("write", "write a probe object", lambda: op.write(path, data)),
                 ("read", "read the probe object back", read),
                 ("list", "list the probe object", listed),
                 ("delete", "delete the probe object", lambda: op.delete(path))]
        for step, what, call in calls:
            started = time.perf_counter()
            try:
                with self._calling(spec, what):
                    call()
            except RemoteError as e:
                if steps:
                    e.args = (f"{e} (before it, {', '.join(s['step'] for s in steps)} worked)",)
                    _tidy(op, path)
                raise
            steps.append({"step": step, "ms": round((time.perf_counter() - started) * 1000, 1)})
        # The probe's folder, which a service of folders (fs) keeps; nothing on the others.
        _tidy(op, PROBE_DIR)
        return steps

    async def test(self, name: str) -> Dict[str, Any]:
        """Try the remote now (``_test``): ``{name, scheme, steps: [{step, ms}]}``, or RemoteError (503)."""
        spec = await self.get(name)
        return {"name": name, "scheme": spec["scheme"], "steps": await asyncio.to_thread(self._test, spec)}

    # ---------------------------------------------------- copies.py's calls
    def write_file(self, spec: Spec, op: Any, key: str, source: BinaryIO, stop: threading.Event) -> None:
        """Copy the open file ``source`` to ``key`` at the remote, READ_BYTES at a time, the writer sending parts of
        WRITE_CHUNK: never the whole file in memory. Stopped once ``stop`` is set. Then, or on a failure, the writer
        is dropped unclosed and ``key`` deleted, as far as the remote lets it: a folder (fs) writes each part to the
        key as it is sent, so what was sent would stay there looking like the file; S3 and what speaks its API keep
        an unfinished multipart upload, which is no object, until the bucket's lifecycle rule aborts it. The file's
        ``copied`` is still null, so the next copy writes it whole. Blocking."""
        with self._calling(spec, f"write {key}"):
            out = op.open(key, "wb", chunk=WRITE_CHUNK)
        try:
            while chunk := source.read(READ_BYTES):
                if stop.is_set():
                    raise Stopped()
                with self._calling(spec, f"write {key}"):
                    out.write(chunk)
            with self._calling(spec, f"write {key}"):
                out.close()
        except BaseException:
            del out
            _tidy(op, key)
            raise

    def read_file(self, spec: Spec, op: Any, key: str, out: BinaryIO, hasher: Any) -> int:
        """Copy ``key`` at the remote into the open file ``out``, READ_BYTES at a time, hashing it on the way: its
        size. Blocking."""
        size = 0
        with self._calling(spec, f"read {key}"):
            source = op.open(key, "rb")
        try:
            while True:
                with self._calling(spec, f"read {key}"):
                    chunk = source.read(READ_BYTES)
                if not chunk:
                    return size
                hasher.update(chunk)
                out.write(chunk)
                size += len(chunk)
        finally:
            _tidy_close(source)

    def file_names(self, spec: Spec, op: Any, prefix: str) -> List[str]:
        """The names of the objects right under ``prefix`` at the remote (folders, and what is in them, left out).
        Blocking."""
        with self._calling(spec, f"list {prefix}"):
            paths = [entry.path for entry in op.list(prefix, recursive=True)]
        return [p[len(prefix):] for p in paths if p.startswith(prefix) and "/" not in p[len(prefix):]
                and len(p) > len(prefix)]

    def delete_file(self, spec: Spec, op: Any, key: str) -> None:
        """Delete ``key`` at the remote (one that is not there is no error). Blocking."""
        with self._calling(spec, f"delete {key}"):
            op.delete(key)


def key_of(workspace: str, bucket: str, file_id: str = "") -> str:
    """Where a bucket's file is at its remote, under the remote's root: ``<workspace>/<bucket>/<file id>`` (the
    bucket's prefix without one)."""
    return f"{workspace}/{bucket}/{file_id}"


def _tidy(op: Any, path: str) -> None:
    """Delete ``path`` at the remote, ignoring a failure: tidying up after a probe, or a file's write that did not
    end."""
    try:
        op.delete(path)
    except Exception:
        pass


def _tidy_close(reader: Any) -> None:
    try:
        reader.close()
    except Exception:
        pass
