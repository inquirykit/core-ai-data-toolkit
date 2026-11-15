"""The packages import only downward (AGENTS.md, Layers): an import that breaks the layering fails here.

Read from the source, not by importing it: every absolute import in a package's files counts, those inside a
function too, since an upward import made lazily is still the cycle waiting to happen.
"""

import ast
from pathlib import Path
from typing import Iterator, Tuple

import pytest

ROOT = Path(__file__).resolve().parent.parent

# Lowest first. A package may import those in lower layers; the domain packages, sharing a layer, never each other.
LAYERS = [
    {"core"},
    {"db"},
    {"tasks"},
    {"models"},
    {"jobs", "evals", "evidence", "enrichers", "monitoring", "playground", "buckets", "views", "workflows", "access"},
    {"api"},
]
LAYER = {package: level for level, packages in enumerate(LAYERS) for package in packages}
# The web framework is the API layer's alone: the domain raises core/errors.py's refusals instead.
WEB = {"fastapi", "starlette"}
# Who reads the settings; the rest are given theirs by api/app.py's create_app().
READ_CONFIG = {"api", "core", "models"}
# What the packages above may import of the lower packages that have insides: these modules, not the others, so each
# can rearrange its insides without them following. A package's own files import what they like; core/ is a set of
# small modules, each its own.
PUBLIC = {
    "db": {"db", "db.tables", "db.graph", "db.specs"},
    "tasks": {"tasks.runner", "tasks.schedule", "tasks.webhooks"},
    "models": {"models", "models.contracts", "models.events", "models.capture", "models.chat", "models.registry",
               "models.servers", "models.toolcalls"},
}


def _imports(path: Path) -> Iterator[Tuple[str, int]]:
    """Each absolute import in the file: (dotted module, line)."""
    for node in ast.walk(ast.parse(path.read_text(), str(path))):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, node.lineno
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.module, node.lineno


def _is_module(dotted: str) -> bool:
    path = ROOT.joinpath(*dotted.split("."))
    return path.with_suffix(".py").is_file() or (path / "__init__.py").is_file()


def _reaches(path: Path) -> Iterator[Tuple[str, int]]:
    """Each absolute import in the file as the module it reaches: ``from db import tables`` reaches db.tables."""
    for node in ast.walk(ast.parse(path.read_text(), str(path))):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, node.lineno
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            for alias in node.names:
                inner = f"{node.module}.{alias.name}"
                yield (inner if _is_module(inner) else node.module), node.lineno


def _private(package: str, module: str) -> bool:
    """Whether ``package`` importing ``module`` reaches past another package's public modules."""
    top = module.split(".")[0]
    return top in PUBLIC and top != package and module not in PUBLIC[top]


def _breaks(package: str, module: str) -> bool:
    """Whether ``package`` importing ``module`` breaks the layering: a package of ours in its own layer or above."""
    top = module.split(".")[0]
    return top in LAYER and top != package and LAYER[top] >= LAYER[package]


def _files() -> Iterator[Tuple[str, Path]]:
    for package in LAYER:
        for path in sorted((ROOT / package).rglob("*.py")):
            yield package, path


def _where(path: Path, line: int) -> str:
    return f"{path.relative_to(ROOT)}:{line}"


def test_every_package_has_a_layer():
    """A new package gets its place in LAYERS (and in AGENTS.md's table) before it can be checked."""
    packages = {p.name for p in ROOT.iterdir() if (p / "__init__.py").exists() and p.name not in ("tests", "scripts")}
    assert packages - set(LAYER) == set(), "add the new package to LAYERS here and to AGENTS.md's layers"


def test_packages_import_only_lower_layers():
    wrong = [
        f"{_where(path, line)}: {package} imports {module}"
        for package, path in _files()
        for module, line in _imports(path) if _breaks(package, module)
    ]
    assert wrong == [], "imports that break the layering (AGENTS.md, Layers):\n" + "\n".join(wrong)


def test_packages_reach_lower_ones_through_their_public_modules():
    """The packages above import db/, tasks/ and models/ by the modules in PUBLIC; a name they need of another module
    there is imported into a public one (db/tables.py, tasks/runner.py, models/servers/__init__.py say which)."""
    wrong = [
        f"{_where(path, line)}: {package} imports {module}"
        for package, path in _files()
        for module, line in _reaches(path) if _private(package, module)
    ]
    assert wrong == [], "imports past a package's public modules (tests/test_layers.py, PUBLIC):\n" + "\n".join(wrong)


@pytest.mark.parametrize("source, package, private", [
    ("from db.schema import NAME", "evals", ["db.schema"]),
    ("from db import engine", "views", ["db.engine"]),
    ("from db import Database\nfrom db.tables import TableStore", "evals", []),
    ("from tasks.store import public", "api", ["tasks.store"]),
    ("from models.servers import ModelEntry", "api", []),
    ("from .store import X\nfrom db.schema import NAME", "db", []),                 # its own insides
])
def test_the_check_finds_what_reaches_past_the_public_modules(tmp_path, source, package, private):
    path = tmp_path / "x.py"
    path.write_text(source)
    assert [module for module, _ in _reaches(path) if _private(package, module)] == private


def test_models_import_only_core():
    """models/ sits above db/ and tasks/ in LAYERS but imports neither: a model and a request need nothing of the
    database or the queue (AGENTS.md's layer table), so tasks/ stays free to never know the models."""
    wrong = [
        f"{_where(path, line)}: {module}"
        for package, path in _files() if package == "models"
        for module, line in _imports(path) if module.split(".")[0] in set(LAYER) - {"core", "models"}
    ]
    assert wrong == [], "models/ importing more than core/:\n" + "\n".join(wrong)


def test_only_the_api_imports_the_web_framework():
    wrong = [
        f"{_where(path, line)}: {module}"
        for package, path in _files() if package != "api"
        for module, line in _imports(path) if module.split(".")[0] in WEB
    ]
    assert wrong == [], "FastAPI or Starlette outside api/ (raise core/errors.py's refusals instead):\n" + "\n".join(wrong)


def test_only_the_api_and_models_read_the_settings():
    wrong = [
        f"{_where(path, line)}: {module}"
        for package, path in _files() if package not in READ_CONFIG
        for module, line in _imports(path) if module == "core.config"
    ]
    assert wrong == [], "settings read outside api/, core/ and models/ (take them as arguments):\n" + "\n".join(wrong)


def test_only_the_models_package_imports_a_family():
    """A route asks the registry and BaseModel's hooks (``operator_chose``, ``weights_source``), never a model file, so
    a family can be added, changed or removed without touching the API."""
    wrong = [
        f"{_where(path, line)}: {module}"
        for package, path in _files() if package != "models"
        for module, line in _imports(path) if module == "models.families" or module.startswith("models.families.")
    ]
    assert wrong == [], "a model family imported outside models/ (ask the registry instead):\n" + "\n".join(wrong)


@pytest.mark.parametrize("source, package, broken", [
    ("from api.deps import Jobs", "evals", ["api.deps"]),                          # upward
    ("def f():\n    import evidence.store", "enrichers", ["evidence.store"]),    # sideways, inside a function
    ("from models import BaseModel", "db", ["models"]),                            # upward
    ("from db.engine import Store\nfrom .store import X\nimport json", "evals", []),  # lower, relative, not ours
])
def test_the_check_finds_what_breaks_the_layering(tmp_path, source, package, broken):
    path = tmp_path / "x.py"
    path.write_text(source)
    assert [module for module, _ in _imports(path) if _breaks(package, module)] == broken
