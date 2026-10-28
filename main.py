"""
Main entry point for the Core AI & Data toolkit service
"""

import os
import secrets
import subprocess
import sys

import uvicorn
from config import Config


def _lock_holder(path: str):
    """The PID of the process that has the database open, if lsof can tell."""
    try:
        found = subprocess.run(["lsof", "-t", os.path.join(path, "database.lck")],
                               capture_output=True, text=True, timeout=10).stdout.split()
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
    # Settings the service cannot run with, before any worker starts and fails on its own.
    if Config.fatal():
        for reason in Config.fatal():
            print(f"\n!!  {reason}\n", file=sys.stderr)
        sys.exit(1)
    # Before anything starts: a second copy would only fail once its startup reached the database.
    if Config.ARCADEDB_PATH:
        from vectorstore.engine import open_elsewhere

        if open_elsewhere(Config.ARCADEDB_PATH):
            _warn_running(Config.ARCADEDB_PATH)
            sys.exit(1)
    # One setup code for every worker (and reloader child), which inherit the
    # environment; see api.app.
    if Config.needs_setup():
        os.environ.setdefault("SETUP_CODE", secrets.token_urlsafe(12))
    # Each worker process has its own model cache, so WORKERS > 1 multiplies
    # memory use. The reloader is for development only (RELOAD=true).
    uvicorn.run(
        "api.app:app",
        host=Config.HOST,
        port=Config.PORT,
        reload=Config.RELOAD,
        workers=1 if Config.RELOAD else Config.WORKERS,
        log_level=Config.LOG_LEVEL.lower(),
    )
