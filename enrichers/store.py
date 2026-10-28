"""
Where enrichers come from: the built-in files and the operator's files.

Built-in enrichers ship as JSON files in enrichers/builtin/. The operator's
own live in ENRICHERS_DIR (default data/enrichers), one .json (or .toml) file
per enricher, written by the dashboard; a file there with a built-in's name
replaces it, and deleting the file restores the built-in.
"""

import json
import logging
import os
import tomllib
from pathlib import Path
from typing import Dict, List, Optional

from pydantic import ValidationError

from errors import Unavailable

from .spec import EnricherSpec

logger = logging.getLogger(__name__)

BUILTIN_DIR = Path(__file__).resolve().parent / "builtin"


def _read(path: Path) -> dict:
    if path.suffix == ".toml":
        return tomllib.loads(path.read_text())
    return json.loads(path.read_text())


def load_dir(directory: Path, *, strict: bool) -> Dict[str, EnricherSpec]:
    """The specs in ``directory``, by name. A bad file is an error for built-ins, a logged skip otherwise."""
    found: Dict[str, EnricherSpec] = {}
    if not directory.is_dir():
        return found
    for path in sorted(directory.iterdir()):
        if path.suffix not in (".json", ".toml") or path.name.startswith(("_", ".")):
            continue
        try:
            spec = EnricherSpec.model_validate(_read(path))
        except (ValueError, ValidationError, OSError) as e:
            if strict:
                raise
            logger.warning("Skipping enricher file %s: %s", path, e)
            continue
        if spec.name != path.stem:
            message = f"enricher file {path.name} holds an enricher named {spec.name!r}"
            if strict:
                raise ValueError(message)
            logger.warning("Skipping %s", message)
            continue
        found[spec.name] = spec
    return found


class EnricherStore:
    """The effective set of enrichers, re-read from disk on every listing (the files are small)."""

    def __init__(self, user_dir: Optional[Path], builtin_dir: Path = BUILTIN_DIR):
        self.user_dir = Path(user_dir) if user_dir else None
        self.builtin_dir = builtin_dir
        self._builtin: Optional[Dict[str, EnricherSpec]] = None

    def builtin(self) -> Dict[str, EnricherSpec]:
        if self._builtin is None:
            self._builtin = load_dir(self.builtin_dir, strict=True)
        return self._builtin

    def saved(self) -> Dict[str, EnricherSpec]:
        """The operator's files."""
        return load_dir(self.user_dir, strict=False) if self.user_dir else {}

    def all(self) -> Dict[str, "Entry"]:
        """Every enricher by name: the built-ins, each replaced by a saved file of the same name, and the rest saved."""
        saved = self.saved()
        entries: Dict[str, Entry] = {}
        for name, spec in self.builtin().items():
            entries[name] = Entry(saved.get(name, spec), builtin=True, overridden=name in saved)
        for name, spec in saved.items():
            if name not in entries:
                entries[name] = Entry(spec, builtin=False, overridden=False)
        return dict(sorted(entries.items()))

    def get(self, name: str) -> Optional["Entry"]:
        return self.all().get(name)

    def save(self, spec: EnricherSpec) -> "Entry":
        if self.user_dir is None:
            raise Unavailable("ENRICHERS_DIR is empty: enrichers cannot be saved")
        self.user_dir.mkdir(parents=True, exist_ok=True)
        path = self.user_dir / f"{spec.name}.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(spec.model_dump(mode="json"), indent=2) + "\n")
        os.replace(tmp, path)
        stale = self.user_dir / f"{spec.name}.toml"
        if stale.exists():
            stale.unlink()
        return self.get(spec.name)

    def delete(self, name: str) -> bool:
        """Remove the saved file; True if there was one."""
        if self.user_dir is None:
            return False
        removed = False
        for suffix in (".json", ".toml"):
            path = self.user_dir / f"{name}{suffix}"
            if path.exists():
                path.unlink()
                removed = True
        return removed


class Entry:
    """An enricher and where it came from."""

    def __init__(self, spec: EnricherSpec, builtin: bool, overridden: bool):
        self.spec = spec
        self.builtin = builtin
        self.overridden = overridden

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def secret_set(self) -> bool:
        return bool(self.spec.secret and os.environ.get(self.spec.secret))

    def needs_key(self) -> bool:
        return self.spec.kind == "http" and self.spec.uses_secret and not self.spec.secret_optional

    def usable(self, terms_allowed: List[str]) -> bool:
        return self.spec.enabled and self.spec.terms in terms_allowed and (not self.needs_key() or self.secret_set)
