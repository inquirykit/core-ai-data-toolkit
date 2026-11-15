"""
How urgent a piece of work is: a queued task's (tasks/runner.py: of a kind's queued tasks the most urgent starts
first) and a model request's (models/cache.py and models/batching.py: the most urgent gets the room and the slot
first). Both carry it, so it is here, below both. A request someone waits on is high, the default; the eval runner's
and the judge's are normal; low is for bulk work its client marks so.
"""

from typing import Literal

LOW, NORMAL, HIGH = 0, 1, 2
PRIORITIES = {"low": LOW, "normal": NORMAL, "high": HIGH}
# The names, as a request or a body gives one.
Priority = Literal["high", "normal", "low"]
