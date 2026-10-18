"""Background jobs and the bridge back to the CAD program's main thread.

Network calls run on a daemon ``threading.Thread`` and never touch the CAD
program's API. A job reports progress and its result by calling the host's
``fire(event_id, json_text)`` with a JSON string: Fusion fires a custom event
(``Application.fireCustomEvent``), FreeCAD emits a queued Qt signal. The host
delivers it on the main thread, where ``Bridge.dispatch`` hands it to the
callbacks the job was started with. Callbacks stay on the main-thread side;
only JSON crosses.

``retry`` repeats a call with exponential backoff on transient errors
(network failures, timeouts, 502/503/504).
"""

from __future__ import annotations

import itertools
import json
import threading
import time
from typing import Any, Callable, Optional, TypeVar

from .client import CadError

T = TypeVar("T")

FireFn = Callable[[str, str], Any]  # (event_id, additional_info)


def retry(
    fn: Callable[[], T],
    *,
    attempts: int = 3,
    base_delay: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
    should_stop: Callable[[], bool] = lambda: False,
) -> T:
    """Call ``fn``; on a transient CadError wait 1 s, 2 s, ... and try again."""
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except CadError as exc:
            if not exc.transient or attempt == attempts or should_stop():
                raise
            sleep(base_delay * (2 ** (attempt - 1)))
    raise AssertionError("unreachable")


class Job:
    """What a running job can do from the worker thread: report progress, check cancellation."""

    def __init__(self, job_id: int, post: Callable[[dict[str, Any]], None]) -> None:
        self.id = job_id
        self._post = post
        self._cancelled = threading.Event()

    def progress(self, message: str) -> None:
        self._post({"job": self.id, "kind": "progress", "message": message})

    def cancel(self) -> None:
        self._cancelled.set()

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()


class Bridge:
    """Runs jobs on worker threads and delivers their events on the main thread."""

    def __init__(self, event_id: str, fire: FireFn, *, start_threads: bool = True) -> None:
        self.event_id = event_id
        self._fire = fire
        self._start_threads = start_threads
        self._ids = itertools.count(1)
        self._callbacks: dict[int, dict[str, Callable[..., None]]] = {}
        self._jobs: dict[int, Job] = {}
        self._lock = threading.Lock()

    def _post(self, message: dict[str, Any]) -> None:
        try:
            self._fire(self.event_id, json.dumps(message))
        except Exception:  # noqa: BLE001 - the CAD program may be shutting down; nothing to report to
            pass

    def start(
        self,
        work: Callable[[Job], Any],
        *,
        on_done: Callable[[Any], None],
        on_error: Callable[[str, str], None],
        on_progress: Optional[Callable[[str], None]] = None,
        name: str = "job",
    ) -> Job:
        """Run ``work(job)`` in the background. Its return value must be JSON-serializable."""
        job = Job(next(self._ids), self._post)
        with self._lock:
            self._callbacks[job.id] = {"done": on_done, "error": on_error, "progress": on_progress or (lambda _m: None)}
            self._jobs[job.id] = job

        def run() -> None:
            try:
                result = work(job)
                self._post({"job": job.id, "kind": "done", "result": result})
            except CadError as exc:
                self._post({"job": job.id, "kind": "error", "code": exc.code, "message": exc.message})
            except Exception as exc:  # noqa: BLE001 - every failure must reach the user as a message
                self._post({"job": job.id, "kind": "error", "code": "INTERNAL_ERROR", "message": f"{type(exc).__name__}: {exc}"})

        if self._start_threads:
            threading.Thread(target=run, name=f"MakePartsNow-{name}", daemon=True).start()
        else:
            run()
        return job

    def dispatch(self, additional_info: str) -> None:
        """Main thread: route one fired event to its job's callback."""
        try:
            message = json.loads(additional_info)
        except (TypeError, ValueError):
            return
        if not isinstance(message, dict):
            return
        job_id = message.get("job")
        with self._lock:
            callbacks = self._callbacks.get(job_id) if isinstance(job_id, int) else None
            job = self._jobs.get(job_id) if isinstance(job_id, int) else None
            finished = message.get("kind") in ("done", "error")
            if finished:
                self._callbacks.pop(job_id, None)
                self._jobs.pop(job_id, None)
        if callbacks is None or (job is not None and job.cancelled):
            return
        kind = message.get("kind")
        if kind == "progress":
            callbacks["progress"](str(message.get("message", "")))
        elif kind == "done":
            callbacks["done"](message.get("result"))
        elif kind == "error":
            callbacks["error"](str(message.get("code", "INTERNAL_ERROR")), str(message.get("message", "")))

    def cancel_all(self) -> None:
        """Stop delivering events for running jobs (the add-in is stopping or the user cancelled). Jobs still clean up."""
        with self._lock:
            for job in self._jobs.values():
                job.cancel()
            self._callbacks.clear()
            self._jobs.clear()

    @property
    def busy(self) -> bool:
        with self._lock:
            return bool(self._jobs)
