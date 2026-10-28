"""
The errors a request's caller can act on, each knowing what it means over HTTP.

The stores, the job runner, the model cache and the Hub downloads raise these;
api/app.py answers every one with its ``status``, ``headers`` and message, so a
route never turns an error into a status by hand. Each also is the builtin a
caller would have caught (a NotFound is a LookupError, a Refused a ValueError),
so code that caught those still does. No dependency, like procs.py.
"""

from typing import Dict, Optional


def describe(exc: BaseException, message: str, debug: bool) -> str:
    """``message`` with the error's type, or its text too when ``debug`` (DEBUG): what a client is told of an
    exception nobody expected, so a path or a key in it stays in the log."""
    return f"{message}: {exc}" if debug else f"{message} ({type(exc).__name__})"


class ServiceError(Exception):
    """A request refused; ``status`` is its HTTP status (a class's own, or one given)."""

    status = 400

    def __init__(self, message: str = "", *, status: Optional[int] = None, headers: Optional[Dict[str, str]] = None):
        super().__init__(message)
        if status is not None:
            self.status = status
        self.headers = headers

    def __str__(self) -> str:
        # The message as given, not KeyError's quoted repr of it.
        return str(self.args[0]) if self.args else ""


class Refused(ServiceError, ValueError):
    """The request is wrong: a bad value, an unknown name."""

    status = 400


class Unauthorized(ServiceError, PermissionError):
    """No key, or not one of the accepted ones; the header names the scheme."""

    status = 401

    def __init__(self, message: str = "", *, status: Optional[int] = None, headers: Optional[Dict[str, str]] = None):
        super().__init__(message, status=status, headers=headers or {"WWW-Authenticate": "Bearer"})


class Forbidden(ServiceError, PermissionError):
    """The key is good, but this is not allowed: a model outside ALLOWED_MODELS, a read-only namespace."""

    status = 403


class NotFound(ServiceError, LookupError):
    status = 404


class Conflict(ServiceError, ValueError):
    """The request is fine, but not now: the thing is busy, or in the way."""

    status = 409


class TooLarge(ServiceError, ValueError):
    status = 413


class Unavailable(ServiceError, RuntimeError):
    """A part of the service is off, full or down: the request may work later."""

    status = 503
