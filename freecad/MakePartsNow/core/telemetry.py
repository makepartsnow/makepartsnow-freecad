"""Opt-in usage counts.

Only counts of allowlisted event names are kept, in memory, and only when the
user opted in. They are sent as ``{client, version, events: {name: count}}``
(``client`` is the host's ``client_name``) from the worker thread, fire and forget: a failed send is dropped, never retried
and never shown. No ids, file names, part data or IP-derived data are collected.
"""

from __future__ import annotations

import threading
from typing import Callable, Mapping, Optional

from .host import COMMON_EVENTS

EVENTS = COMMON_EVENTS  # the default allowlist; a host passes its own (Fusion adds quote_saved_to_part)


class Counter:
    """Thread-safe event counter that is inert unless enabled."""

    def __init__(self, enabled: bool, events: Optional[frozenset[str]] = None) -> None:
        self.enabled = enabled
        self.events = EVENTS if events is None else events
        self._counts: dict[str, int] = {}
        self._lock = threading.Lock()

    def record(self, name: str, count: int = 1) -> None:
        if not self.enabled or name not in self.events or count <= 0:
            return
        with self._lock:
            self._counts[name] = self._counts.get(name, 0) + count

    def take(self) -> dict[str, int]:
        """Return and clear the pending counts."""
        with self._lock:
            counts, self._counts = self._counts, {}
        return counts

    def flush(self, send: Callable[[Mapping[str, int]], bool], start_thread: bool = True) -> None:
        """Send pending counts on a background thread (or inline for tests)."""
        if not self.enabled:
            self.take()
            return
        counts = self.take()
        if not counts:
            return

        def _run() -> None:
            try:
                send(counts)
            except Exception:  # noqa: BLE001 - usage counts must never disturb the user
                pass

        if start_thread:
            threading.Thread(target=_run, name="MakePartsNow-telemetry", daemon=True).start()
        else:
            _run()
