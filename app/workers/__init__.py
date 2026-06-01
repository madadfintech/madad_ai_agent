"""Asynchronous platform workers.

Celery beat drives the periodic jobs in :mod:`app.workers.jobs` via the task
wrappers in :mod:`app.workers.tasks`. The jobs are plain ``async`` functions with
no Celery dependency, so they are unit-testable in isolation.
"""
