"""
Importing a package's files so their registrations run: the model files
(models/registry.py) and the job files (jobs/registry.py) are found this way,
so adding one means adding a file. No dependency, like procs.py.
"""

import importlib
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def import_files(package_dir: Path, package: str, what: str) -> int:
    """Import every ``.py`` file in the subfolders of ``package_dir`` (the package ``package``); how many.

    Files and folders whose names start with "_" or "." are skipped, so
    helpers can live in e.g. ``_utils.py``. A file that fails to import is
    logged (as a ``what`` file) and skipped instead of taking the service down.
    """
    imported = 0
    for path in sorted(package_dir.glob("*/**/*.py")):
        parts = path.relative_to(package_dir).with_suffix("").parts
        if any(part.startswith(("_", ".")) for part in parts):
            continue
        module = ".".join((package, *parts))
        try:
            importlib.import_module(module)
            imported += 1
        except Exception:
            logger.warning("Skipping %s file %s: import failed", what, module, exc_info=True)
    return imported
