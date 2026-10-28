"""
Reads and writes the .env file the service is configured from.

Precedence: a non-empty environment variable wins over the file, and the file
wins over the defaults in config.py. An empty environment variable counts as
unset, because the compose files pass e.g. API_KEY=${API_KEY:-}.
"""

import os
import re
import tempfile
from pathlib import Path
from typing import Dict, List, Union

_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")

PathLike = Union[str, Path]


def _match(line: str):
    if line.lstrip().startswith("#"):
        return None
    return _LINE.match(line)


def parse(text: str) -> Dict[str, str]:
    """KEY=value lines; blank lines and # comments are skipped, quotes removed."""
    values: Dict[str, str] = {}
    for line in text.splitlines():
        match = _match(line)
        if not match:
            continue
        key, value = match.groups()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].rstrip()
        values[key] = value
    return values


def load(path: PathLike) -> List[str]:
    """Copy the file's settings into os.environ where not already set. Returns the keys set."""
    path = Path(path)
    if not path.is_file():
        return []
    loaded = []
    for key, value in parse(path.read_text()).items():
        if not os.environ.get(key):
            os.environ[key] = value
            loaded.append(key)
    return loaded


def update(path: PathLike, values: Dict[str, str]) -> None:
    """Set ``values`` in the file, keeping every other line and comment.

    The file is replaced atomically and is readable by its owner only, since
    it holds the API keys.
    """
    path = Path(path)
    lines = path.read_text().splitlines() if path.is_file() else []
    written = set()
    out = []
    for line in lines:
        match = _match(line)
        if match and match.group(1) in values:
            key = match.group(1)
            out.append(f"{key}={values[key]}")
            written.add(key)
        else:
            out.append(line)
    missing = [key for key in values if key not in written]
    if missing:
        if out and out[-1].strip():
            out.append("")
        out.extend(f"{key}={values[key]}" for key in missing)

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")  # created with mode 0600
    try:
        with os.fdopen(fd, "w") as f:
            f.write("\n".join(out) + "\n")
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
