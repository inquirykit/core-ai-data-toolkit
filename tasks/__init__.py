"""
The service's one queue of background work: jobs, eval runs, judging and Hub
downloads are kinds of task (runner.py), rows of one table (store.py), and a
finished job or run is POSTed to the webhooks (webhooks.py), each delivery a
task too.

The priorities are here, with nothing imported, since the model requests
carry them too (models/events.py) and models/ must not load the queue.
"""

# How urgent a piece of work is. Of a kind's queued tasks the most urgent starts first (runner.py), and a request
# carries its priority when models compete for room in the cache (models/cache.py). A request someone waits on is
# high, the default; the eval runner's and the judge's are normal; low is for bulk work its client marks so.
LOW, NORMAL, HIGH = 0, 1, 2
PRIORITIES = {"low": LOW, "normal": NORMAL, "high": HIGH}
