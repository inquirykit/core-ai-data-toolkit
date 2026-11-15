"""
Where enrichers come from: the built-in files and the ones saved from /admin.

Built-in enrichers ship as JSON files in enrichers/builtin/. The operator's own are specs in the database
(``tk_enricher``, a SpecStore), saved by the dashboard; one with a built-in's name replaces it, and deleting it
restores the built-in. The saved ones are read once and kept: this store is their only writer, so a lookup (one per
value enriched, one per row of a workflow's Enrich node) reads no file and no table. Its methods run on the database's
threads, several at once: a save or a delete holds the store's lock and puts a new dict in place of the kept one, so a
lookup reads the one it took whole.
"""

import json
import logging
import os
import threading
from pathlib import Path
from typing import Dict, List, Optional

from pydantic import ValidationError

from db import Database, on_pool
from db.specs import SpecStore

from .spec import EnricherSpec

logger = logging.getLogger(__name__)

BUILTIN_DIR = Path(__file__).resolve().parent / "builtin"
# The saved specs' scope: the service's own.
SCOPE = ""


def load_builtin(directory: Path) -> Dict[str, EnricherSpec]:
    """The specs in ``directory``, by name; a bad file is an error (they ship with the code)."""
    found: Dict[str, EnricherSpec] = {}
    for path in sorted(directory.glob("*.json")):
        if path.name.startswith(("_", ".")):
            continue
        spec = EnricherSpec.model_validate(json.loads(path.read_text()))
        if spec.name != path.stem:
            raise ValueError(f"enricher file {path.name} holds an enricher named {spec.name!r}")
        found[spec.name] = spec
    return found


class EnricherStore(SpecStore):
    """The effective set of enrichers; see the module docstring."""

    TABLE = "tk_enricher"

    def __init__(self, database: Database, builtin_dir: Path = BUILTIN_DIR):
        super().__init__(database)
        self.builtin = load_builtin(builtin_dir)
        self._saved: Optional[Dict[str, EnricherSpec]] = None
        self._lock = threading.Lock()

    def _load(self) -> Dict[str, EnricherSpec]:
        """The saved ones, read from the table the first time. Never changed in place (see the module docstring)."""
        saved = self._saved
        if saved is not None:
            return saved
        with self._lock:
            if self._saved is not None:
                return self._saved
            saved = {}
            for row in self._all(SCOPE):
                try:
                    spec = EnricherSpec.model_validate({k: v for k, v in row.items() if k not in ("created", "updated")})
                except ValidationError as e:
                    logger.warning("Skipping the saved enricher %r: %s", row.get("name"), e)
                    continue
                saved[spec.name] = spec
            self._saved = saved
            return saved

    @on_pool
    def all(self) -> Dict[str, "Entry"]:
        """Every enricher by name: the built-ins, each replaced by a saved one of the same name, and the rest saved."""
        saved = self._load()
        entries: Dict[str, Entry] = {}
        for name, spec in self.builtin.items():
            entries[name] = Entry(saved.get(name, spec), builtin=True, overridden=name in saved)
        for name, spec in saved.items():
            if name not in entries:
                entries[name] = Entry(spec, builtin=False, overridden=False)
        return dict(sorted(entries.items()))

    async def get(self, name: str) -> Optional["Entry"]:
        return (await self.all()).get(name)

    @on_pool
    def _put(self, spec: EnricherSpec) -> None:
        self._load()
        with self._lock:
            self._save(SCOPE, spec.name, spec.model_dump(mode="json"))
            self._saved = {**self._saved, spec.name: spec}

    async def save(self, spec: EnricherSpec) -> "Entry":
        await self._put(spec)
        return await self.get(spec.name)

    @on_pool
    def delete(self, name: str) -> bool:
        """Remove the saved one; True if there was one."""
        self._load()
        with self._lock:
            removed = self._remove(SCOPE, name)
            self._saved = {k: v for k, v in self._saved.items() if k != name}
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
        return self.spec.uses_secret and not self.spec.secret_optional

    def usable(self, terms_allowed: List[str]) -> bool:
        return self.spec.enabled and self.spec.terms in terms_allowed and (not self.needs_key() or self.secret_set)
