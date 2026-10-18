"""Export one part to STEP or STL from a snapshot, never from the person's document.

The core calls ``export_part(ref, path, fmt)`` on FreeCAD's main thread (the only
safe one for documents). It finds the object again, copies its shape with its
global placement, checks the copy, and writes the file:

* STEP: the copy goes into a hidden temporary document as a plain
  ``Part::Feature`` labelled like the original, and ``Import.export`` writes it.
  The core then blanks the author and organization in the STEP header.
* STL from a mesh: the mesh copy is written as binary STL, in mm.
* STL from a solid: only when the person chose the fallback after a STEP export
  failed. ``MeshPart.meshFromShape`` with ``MESH_LINEAR_DEFLECTION_MM`` and
  ``MESH_ANGULAR_DEFLECTION_RAD``.

The source document is never saved, changed or recomputed here, and the
temporary document is closed whatever happens. The file goes to the core's
private temp folder and is deleted after the upload.
"""

from __future__ import annotations

import os
from typing import Any

import FreeCAD as App

from . import selection
from .constants import MESH_ANGULAR_DEFLECTION_RAD, MESH_LINEAR_DEFLECTION_MM

TEMP_DOC = "MakePartsNowExport"


def _check_shape(shape: Any, label: str) -> None:
    if shape.isNull() or len(shape.Solids) != 1 or shape.Volume <= 0 or not selection._finite(shape.BoundBox):
        raise selection.Refused(f"{label} has no valid solid to export.")


def _new_temp_doc() -> Any:
    try:
        return App.newDocument(TEMP_DOC, hidden=True, temp=True)
    except TypeError:  # an older FreeCAD without the hidden and temp arguments
        return App.newDocument(TEMP_DOC)


def _restore_active(name: str) -> None:
    if not name or name not in App.listDocuments():
        return
    try:
        App.setActiveDocument(name)
        import FreeCADGui as Gui  # noqa: PLC0415 - absent in FreeCADCmd

        if Gui.getDocument(name) is not None:
            Gui.ActiveDocument = Gui.getDocument(name)
    except Exception:  # noqa: BLE001 - FreeCADCmd has no GUI; nothing else to restore
        pass


def write_step(shape: Any, label: str, path: str) -> None:
    import Import  # noqa: PLC0415 - loaded only when exporting

    active = App.ActiveDocument.Name if App.ActiveDocument is not None else ""
    doc = _new_temp_doc()
    try:
        feature = doc.addObject("Part::Feature", "Part")
        feature.Label = label
        feature.Shape = shape
        Import.export([feature], path)
    finally:
        App.closeDocument(doc.Name)
        _restore_active(active)


def write_stl_from_mesh(mesh: Any, path: str) -> None:
    mesh.write(path)  # binary STL for a .stl path; FreeCAD meshes are in mm


def write_stl_from_shape(shape: Any, path: str) -> None:
    import Mesh  # noqa: PLC0415, F401 - registers the mesh writer
    import MeshPart  # noqa: PLC0415

    mesh = MeshPart.meshFromShape(
        Shape=shape,
        LinearDeflection=MESH_LINEAR_DEFLECTION_MM,
        AngularDeflection=MESH_ANGULAR_DEFLECTION_RAD,
        Relative=False,
    )
    mesh.write(path)


def export_part(ref: selection.PartRef, path: str, fmt: str) -> bool:
    """The core's exporter: write ``ref`` to ``path`` as ``fmt``. Raises Refused for a part that is gone or broken."""
    obj = selection.find(ref)
    if ref.fmt == "stl":
        mesh = selection.mesh_with_placement(obj)
        write_stl_from_mesh(mesh, path)
    else:
        shape = selection.shape_with_placement(obj)
        _check_shape(shape, ref.name)
        if fmt == "step":
            write_step(shape, ref.name, path)
        else:
            write_stl_from_shape(shape, path)
    return os.path.isfile(path) and os.path.getsize(path) > 0
