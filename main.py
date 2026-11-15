"""
Main entry point for the Core AI & Data toolkit service
"""

import os
import secrets
import subprocess
import sys

import uvicorn
from core.config import Config


def _lock_holder(path: str):
    """The PID of the process that has the database file open, if lsof can tell."""
    try:
        found = subprocess.run(["lsof", "-t", path], capture_output=True, text=True, timeout=10).stdout.split()
    except (OSError, subprocess.SubprocessError):
        return None
    return found[0] if found else None


def _warn_running(path: str) -> None:
    """A banner no one can miss: the service is running already (another process has the database)."""
    pid = _lock_holder(path)
    host = "localhost" if Config.HOST in ("0.0.0.0", "::", "127.0.0.1") else Config.HOST
    lines = [
        "!!  THE SERVICE IS ALREADY RUNNING  !!",
        "",
        f"Another process{f' (PID {pid})' if pid else ''} has the database open: {path}",
        f"It is probably serving http://{host}:{Config.PORT}",
        "",
        f"Use that one, or stop it first{f': kill {pid}' if pid else '.'}",
        "Only one process can open the database, so this one is not starting.",
    ]
    width = max(map(len, lines)) + 4
    box = "\n".join(["╔" + "═" * width + "╗", *(f"║  {line.ljust(width - 4)}  ║" for line in lines), "╚" + "═" * width + "╝"])
    if sys.stderr.isatty():
        box = f"\033[1;97;41m{box}\033[0m"
    print(f"\n{box}\n", file=sys.stderr)


if __name__ == "__main__":
    # Before anything starts: a second copy would only fail once its startup reached the database.
    from db.engine import open_elsewhere

    if open_elsewhere(str(Config.DATABASE_PATH)):
        _warn_running(str(Config.DATABASE_PATH))
        sys.exit(1)
    # The same setup code for the reloader's child, which inherits the environment; see api.app.
    if Config.needs_setup():
        os.environ.setdefault("SETUP_CODE", secrets.token_urlsafe(12))
    # One process: it alone opens the database (the reloader, RELOAD=true, is for development only).
    uvicorn.run(
        "api.app:app",
        host=Config.HOST,
        port=Config.PORT,
        reload=Config.RELOAD,
        log_level=Config.LOG_LEVEL.lower(),
    )
