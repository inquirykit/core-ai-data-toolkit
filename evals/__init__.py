"""
Traces and evaluations: what each request was and what came back, kept for
debugging; datasets built from them; runs of a model over a dataset; and
scores from metrics or an LLM judge. Stored in the embedded ArcadeDB
database (see evals/store.py).
"""

from .capture import compact
from .runner import RunExecutor
from .store import EvalError, EvalStore, NotFound

__all__ = ["EvalError", "EvalStore", "NotFound", "RunExecutor", "compact"]
