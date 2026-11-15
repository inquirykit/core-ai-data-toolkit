"""
The service's one queue of background work: jobs, eval runs, judging and
workflow runs are kinds of task (runner.py), rows of one table (store.py), and a
finished job or run is POSTed to the webhooks (webhooks.py), each delivery a
task too. How urgent a task is, as a model request is too, is core/priorities.py.
"""
