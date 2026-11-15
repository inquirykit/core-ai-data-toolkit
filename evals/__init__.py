"""
Evaluations: datasets of examples, given or copied from the requests the
service kept (monitoring/); runs of a model over a dataset; and scores from
metrics or an LLM judge. Stored in the embedded DuckDB database (see
evals/store.py).
"""

from .runner import RunExecutor
from .store import EvalStore

__all__ = ["EvalStore", "RunExecutor"]
