"""The consumer side of the job queue.

``app/platform/queue`` can *hold* work and nothing else: a port with two adapters and
no reader is a queue that silently accumulates. This package is the reader — a separate
process (``python -m app.workers.main``) that dequeues jobs and runs the handler
registered for each name, so the API never blocks on work it is not responsible for.

Nothing here imports from a module's ``presentation`` layer: the worker is a second
entry point onto the application, not a second API.
"""
