"""The core controller's ``cad`` adapter for FreeCAD, and the worker-to-GUI-thread signal.

The controller runs on FreeCAD's GUI thread. Network jobs run on the core's
worker threads; each posts a JSON string through ``Relay.fire``, a Qt signal,
which Qt delivers on the GUI thread (a queued connection, since the sender is
another thread), where ``worker.Bridge.dispatch`` runs the job's callback.
"""

from __future__ import annotations

from typing import Any, Callable

import FreeCAD as App
import FreeCADGui as Gui
from PySide import QtCore, QtGui

from .. import export, selection


class Relay(QtCore.QObject):
    """Carries worker events to the GUI thread."""

    event = QtCore.Signal(str, str)

    def fire(self, event_id: str, info: str) -> None:
        self.event.emit(event_id, info)


class FreeCadAdapter:
    """What the controller needs from FreeCAD: the selection, the exporter, the browser, the panel."""

    def __init__(self, view: Any) -> None:
        self.view = view  # the panel: render(state), show_progress(progress), show_toast(text, kind)

    # -- the model

    def selected_parts(self, explicit: bool):
        doc = App.ActiveDocument
        items = Gui.Selection.getSelectionEx("", 1) if doc is not None else []
        result = selection.collect(items, doc)
        if not explicit:
            return result.parts, []
        return result.parts, result.errors

    def export(self, ref: selection.PartRef, path: str, fmt: str) -> bool:
        return export.export_part(ref, path, fmt)

    # -- the panel

    def push(self, state: dict) -> None:
        self.view.render(state)

    def progress(self, progress: Any) -> None:
        self.view.show_progress(progress)

    def toast(self, text: str, kind: str) -> None:
        self.view.show_toast(text, kind)
        writer = App.Console.PrintWarning if kind in ("warn", "error") else App.Console.PrintMessage
        writer(f"Make Parts Now: {text}\n")

    # -- the system

    def open_url(self, url: str) -> bool:
        return bool(QtGui.QDesktopServices.openUrl(QtCore.QUrl(url)))

    def defer(self, fn: Callable[[], None]) -> None:
        QtCore.QTimer.singleShot(0, fn)


class SelectionObserver:
    """Tells the controller when the selection changes, at most once per event-loop turn."""

    def __init__(self, on_change: Callable[[], None]) -> None:
        self._on_change = on_change
        self._pending = False

    def _poke(self, *_args: Any) -> None:
        if self._pending:
            return
        self._pending = True

        def run() -> None:
            self._pending = False
            self._on_change()

        QtCore.QTimer.singleShot(0, run)

    addSelection = _poke  # noqa: N815 - FreeCAD's observer method names
    setSelection = _poke  # noqa: N815
    removeSelection = _poke  # noqa: N815
    clearSelection = _poke  # noqa: N815
