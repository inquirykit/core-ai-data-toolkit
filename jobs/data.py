"""
The files around the jobs, as the service handles them: the input files in DATA_DIR/datasets that job params name
(``DataFiles``: uploaded, listed, summarized, deleted, and an eval dataset's training file written there), and what a
job's own folder holds (``listing``, ``existing``). A path a client names is resolved inside its folder or refused
(``inside``), and DataFiles' reads and writes go on threads, never on the event loop.

A data file's summary, for choosing what to train on: its row count, each column's type and spread (DuckDB's
SUMMARIZE: min, max, approximate distinct count, share missing, mean), and its first rows. The service's own database
reads no files (db/engine.py turns external access off) and stays so. Each summary opens an in-memory DuckDB of its
own, confined to the folder the files are in (allowed_directories, then external access off, in that order: DuckDB
refuses the folder once access is off), with a small memory limit and two threads, and reads the one path it is given
as a parameter.

A CSV's delimiter and decimal mark are the ones the training jobs read it with (core/csvfiles.py), and its columns
are typed as numbers, dates and text only: DuckDB would type a yes/no column as BOOLEAN where pandas, and so
AutoGluon, reads text. The types are a guide; the trained predictor's are its own.
"""

import asyncio
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, AsyncIterator, BinaryIO, Dict, List, Tuple

from core.csvfiles import dialect
from core.errors import NotFound, Refused, TooLarge

# An uploaded file's name: no folders, no hidden files.
FILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,200}$")
# The files a listing names at most.
MAX_LISTED = 2000

MEMORY_LIMIT = "256MB"
THREADS = 2
HEAD_ROWS = 20
_CSV = ("read_csv($p, delim = $delim, decimal_separator = $decimal, "
        "auto_type_candidates = ['BIGINT', 'DOUBLE', 'DATE', 'TIMESTAMP', 'VARCHAR'])")
READERS = {".csv": _CSV, ".tsv": _CSV, ".txt": _CSV, ".parquet": "read_parquet($p)", ".pq": "read_parquet($p)",
           ".jsonl": "read_json($p)", ".ndjson": "read_json($p)", ".json": "read_json($p)"}


def inside(base: Path, relative: str) -> Path:
    """``relative`` resolved inside ``base``; Refused when it would lead out of it."""
    path = (base / relative).resolve()
    if not path.is_relative_to(base.resolve()):
        raise Refused("Path must stay inside the directory")
    return path


def existing(base: Path, relative: str) -> Path:
    """The file ``relative`` inside ``base``: Refused outside it, NotFound when there is none."""
    path = inside(base, relative)
    if not path.is_file():
        raise NotFound("File not found")
    return path


def listing(base: Path) -> List[Dict[str, Any]]:
    """The files under ``base`` (MAX_LISTED at most), each with its path there and its size. Blocking."""
    if not base.is_dir():
        return []
    files = []
    for path in sorted(base.rglob("*")):
        if path.is_file():
            files.append({"path": path.relative_to(base).as_posix(), "bytes": path.stat().st_size})
            if len(files) >= MAX_LISTED:
                break
    return files


def _number(value: Any) -> Any:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def summarize(path: Path, folder: Path) -> Dict[str, Any]:
    """The rows, columns and first rows of ``path``, a file inside ``folder``. Refused for a file DuckDB cannot
    read as a table. Blocking."""
    import duckdb

    reader = READERS.get(path.suffix.lower())
    if reader is None:
        raise Refused(f"{path.name}: a summary reads CSV, TSV, Parquet and JSON Lines files")
    con = duckdb.connect(":memory:", config={"memory_limit": MEMORY_LIMIT, "threads": THREADS})
    try:
        con.execute("SET allowed_directories = ['" + str(folder.resolve()).replace("'", "''") + "/']")
        con.execute("SET enable_external_access = false")
        source = {"p": str(path)}
        if reader == _CSV:
            source["delim"], source["decimal"] = dialect(str(path), "\t" if path.suffix.lower() == ".tsv" else ",")
        summary = con.execute(f"SUMMARIZE SELECT * FROM {reader}", source).fetchall()
        head = con.execute(f"SELECT to_json(t) FROM (SELECT * FROM {reader} LIMIT {HEAD_ROWS}) t", source).fetchall()
    except (duckdb.Error, OSError) as e:
        raise Refused(f"{path.name} could not be read as a table: {str(e).splitlines()[0][:300]}") from None
    finally:
        con.close()
    columns = [
        {"name": name, "type": kind, "min": low, "max": high, "distinct": distinct, "mean": _number(mean),
         "missing": _number(missing) / 100 if missing is not None else 0.0}
        for name, kind, low, high, distinct, mean, _std, _q25, _q50, _q75, _count, missing in summary
    ]
    return {"rows": summary[0][10] if summary else 0, "columns": columns, "head": [json.loads(r[0]) for r in head]}


def _open_temp(root: Path) -> Tuple[BinaryIO, Path]:
    root.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=root, prefix=".upload-")
    return os.fdopen(fd, "wb"), Path(temp)


def _discard(out: BinaryIO, temp: Path) -> None:
    try:
        out.close()
    finally:
        temp.unlink(missing_ok=True)


def _write_lines(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


class DataFiles:
    """The jobs' input files, in ``root`` (DATA_DIR/datasets), each uploaded one at most ``max_bytes``
    (JOB_MAX_UPLOAD_BYTES)."""

    def __init__(self, root: Path, max_bytes: int):
        self.root = Path(root)
        self.max_bytes = max_bytes

    async def list(self) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(listing, self.root)

    async def summary(self, path: str) -> Dict[str, Any]:
        """``path``'s summary (``summarize``): NotFound when there is no such file."""
        return await asyncio.to_thread(lambda: summarize(existing(self.root, path), self.root))

    async def save(self, name: str, chunks: AsyncIterator[bytes]) -> Dict[str, Any]:
        """Keep what ``chunks`` yields as the file ``name``, replacing one of that name once all of it is there: its
        path and size. TooLarge past ``max_bytes``, and nothing stays of an upload that does not end."""
        if not FILE_NAME.match(name):
            raise Refused("File name: letters, digits, '.', '_' and '-' only")
        # Made here, not on a thread: a cancel comes only at an await, so none comes between the file's making and the
        # try that removes it.
        out, temp = _open_temp(self.root)
        size = 0
        try:
            async for chunk in chunks:
                size += len(chunk)
                if size > self.max_bytes:
                    raise TooLarge("File is larger than JOB_MAX_UPLOAD_BYTES")
                await asyncio.to_thread(out.write, chunk)
            await asyncio.to_thread(out.close)
            await asyncio.to_thread(os.replace, temp, self.root / name)
        except BaseException:
            # Run to its end on its thread even if this request is cancelled meanwhile.
            await asyncio.to_thread(_discard, out, temp)
            raise
        return {"path": name, "bytes": size}

    async def delete(self, path: str) -> None:
        await asyncio.to_thread(lambda: existing(self.root, path).unlink())

    async def write_lines(self, path: str, rows: List[Dict[str, Any]]) -> None:
        """Write ``rows`` as JSON Lines to ``path`` here, in place of what was there."""
        await asyncio.to_thread(lambda: _write_lines(inside(self.root, path), rows))
