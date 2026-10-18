"""Which selected objects can be quoted, and their size in millimeters.

Accepted: PartDesign Bodies, Part features with exactly one solid, and closed
meshes. A selected face, edge or vertex, or a feature inside a Body, stands for
its whole object. Refused, each with a short reason: assemblies and App::Part
containers, App::Link, compounds of several solids, sketches and other shapes
without a solid, hidden objects (an earlier step of a Body, a Cut's tool) and
meshes with holes. Only what the person selected is considered: never
``doc.Objects`` or everything visible.

FreeCAD's geometry is in millimeters whatever the display units, so sizes are
the shape's bounding box with its global placement (the placement the export
uses), sorted smallest first.

This module reads the document only; it never changes or saves it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional

import FreeCAD as App

ASSEMBLY_TYPES = ("App::Part", "Assembly::AssemblyObject", "App::DocumentObjectGroup")


@dataclass(frozen=True)
class PartRef:
    """A part to quote, found again by document and object name when it is exported."""

    doc_name: str
    obj_name: str
    name: str  # the object's label, as the person sees it in the tree
    fmt: str  # "step" for a solid, "stl" for a mesh
    local_dims_mm: tuple[float, float, float]
    dims_candidates: tuple[tuple[float, float, float], ...] = field(default=())

    @property
    def key(self) -> str:
        return f"{self.doc_name}:{self.obj_name}"


@dataclass
class SelectionResult:
    parts: list[PartRef] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class Refused(Exception):
    """An object that can't be quoted; the message says why, for the person."""


def _sorted_dims(box: Any) -> tuple[float, float, float]:
    dims = sorted((float(box.XLength), float(box.YLength), float(box.ZLength)))
    return (dims[0], dims[1], dims[2])


def _finite(box: Any) -> bool:
    values = (box.XMin, box.YMin, box.ZMin, box.XMax, box.YMax, box.ZMax)
    return all(math.isfinite(float(v)) for v in values)


def _type(obj: Any) -> str:
    return str(getattr(obj, "TypeId", ""))


def owning_body(obj: Any) -> Optional[Any]:
    """The PartDesign Body that holds ``obj`` (a feature, sketch or datum), if any."""
    try:
        parent = obj.getParentGeoFeatureGroup()
    except Exception:  # noqa: BLE001 - older objects without the method
        parent = None
    return parent if parent is not None and _type(parent) == "PartDesign::Body" else None


def _visible(obj: Any) -> bool:
    try:
        return bool(obj.Visibility)
    except Exception:  # noqa: BLE001
        return True


def resolve(obj: Any) -> Any:
    """The object a selection stands for: a feature inside a Body stands for the Body.

    An earlier, hidden step of a Body (not its tip) is refused: it isn't what the
    Body looks like, and quoting it by mistake is easy from the tree.
    """
    body = owning_body(obj)
    if body is None:
        return obj
    if obj is body.Tip or _visible(obj):
        return body
    raise Refused(f"{obj.Label} is an earlier step of {body.Label}, not the finished part. Select {body.Label} instead.")


def shape_with_placement(obj: Any) -> Any:
    """A copy of the object's shape moved to its global placement (a Body or feature inside an App::Part moves with it).

    The placement is baked into the geometry (``transformShape``, a rigid move that keeps planes and cylinders
    exact), not left as a location: ``Import.export`` from the temporary document drops a shape's location, so
    a rotated part would otherwise export unrotated.
    """
    shape = obj.Shape.copy()
    if shape.isNull():  # an empty Body or a feature that made nothing: OCC can't transform it, check_object refuses it
        return shape
    shape.Placement = App.Placement()
    shape.transformShape(obj.getGlobalPlacement().toMatrix(), True)
    return shape


def mesh_with_placement(obj: Any) -> Any:
    """A copy of the mesh with its global placement baked into the points (STL has no placement)."""
    mesh = obj.Mesh.copy()
    mesh.Placement = App.Placement()
    mesh.transform(obj.getGlobalPlacement().toMatrix())
    return mesh


def check_object(obj: Any) -> PartRef:
    """A PartRef for ``obj`` (already resolved), or Refused with the reason."""
    kind = _type(obj)
    label = obj.Label
    if kind == "App::Link" or kind.startswith("App::Link"):
        raise Refused(f"{label} is a link. Select the original body instead.")
    if kind in ASSEMBLY_TYPES or obj.isDerivedFrom("App::Part"):
        raise Refused(f"{label} is an assembly or group. Select the bodies inside it instead.")
    if not _visible(obj):
        raise Refused(f"{label} is hidden (it may be used by another feature). Select the visible result instead.")
    if obj.isDerivedFrom("Mesh::Feature"):
        mesh = mesh_with_placement(obj)
        if mesh.CountFacets == 0:
            raise Refused(f"{label} is an empty mesh.")
        if not mesh.isSolid() or mesh.hasNonManifolds():
            raise Refused(f"{label} is a mesh with holes or open edges. Repair it (Mesh workbench > Analyze) first.")
        box = mesh.BoundBox
        if not _finite(box) or mesh.Volume <= 0:
            raise Refused(f"{label} is a mesh with no volume.")
        bare = obj.Mesh.copy()
        bare.Placement = App.Placement()
        local = _sorted_dims(bare.BoundBox)  # the mesh in its own axes, should the server measure it unrotated
        placed = _sorted_dims(box)
        return PartRef(obj.Document.Name, obj.Name, label, "stl", placed, (placed, local))
    if obj.isDerivedFrom("Part::Part2DObject") or kind.startswith("Sketcher::"):
        raise Refused(f"{label} is a sketch. Select the solid made from it.")
    if not obj.isDerivedFrom("Part::Feature") and kind != "PartDesign::Body":
        raise Refused(f"{label} isn't a solid body.")
    if "Invalid" in obj.State or "Error" in obj.State:
        raise Refused(f"{label} has a recompute error. Fix it in FreeCAD first.")
    shape = shape_with_placement(obj)
    if shape.isNull():
        raise Refused(f"{label} has no shape.")
    solids = len(shape.Solids)
    if solids == 0:
        raise Refused(f"{label} has no solid (it may be a surface or a sketch). Select a solid body.")
    if solids > 1:
        raise Refused(f"{label} has {solids} separate solids. Quote each as its own body, or fuse them.")
    box = shape.BoundBox
    if not _finite(box) or shape.Volume <= 0:
        raise Refused(f"{label} has no volume.")
    bare = obj.Shape.copy()
    bare.Placement = App.Placement()
    local = _sorted_dims(bare.BoundBox)  # the part in its own axes, should the server measure it unrotated
    placed = _sorted_dims(box)
    return PartRef(obj.Document.Name, obj.Name, label, "step", placed, (placed, local))


def recompute_if_needed(doc: Any) -> list[str]:
    """Recompute a document that has pending changes; return the labels of objects that failed."""
    try:
        touched = any("Touched" in o.State for o in doc.Objects)
    except Exception:  # noqa: BLE001
        touched = False
    if touched:
        doc.recompute()
    return [o.Label for o in doc.Objects if "Invalid" in o.State or "Error" in o.State]


def collect(selection: list[Any], doc: Any, *, max_parts: int = 10**6) -> SelectionResult:
    """Turn ``Gui.Selection.getSelectionEx()`` items (or plain objects) into parts and reasons.

    Duplicates (two faces of one body, a body and its tip) give one part.
    """
    result = SelectionResult()
    if doc is None:
        result.errors.append("Open a document to quote its parts.")
        return result
    failed = recompute_if_needed(doc)
    seen: set[str] = set()
    picked_subelements: list[str] = []
    for item in selection:
        obj = getattr(item, "Object", item)
        subs = tuple(getattr(item, "SubElementNames", ()) or ())
        try:
            target = resolve(obj)
            if target.Name in seen:
                continue
            if target.Document is not doc:
                raise Refused(f"{target.Label} is in another document. Quote one document at a time.")
            if failed and target.Label in failed:
                raise Refused(f"{target.Label} has a recompute error. Fix it in FreeCAD first.")
            part = check_object(target)
        except Refused as exc:
            text = str(exc)
            if text not in result.errors:
                result.errors.append(text)
            continue
        except Exception as exc:  # noqa: BLE001  one unreadable object must not hide the rest of the selection
            label = getattr(obj, "Label", "An object")
            text = f"{label} couldn't be read as a part ({type(exc).__name__}). Select a solid body."
            if text not in result.errors:
                result.errors.append(text)
            continue
        seen.add(target.Name)
        if len(result.parts) >= max_parts:
            result.errors.append(f"Quote up to {max_parts} parts at a time; {part.name} was not added.")
            continue
        result.parts.append(part)
        if any(s for s in subs) or target is not obj:
            picked_subelements.append(part.name)
    if picked_subelements:
        names = ", ".join(dict.fromkeys(picked_subelements))
        result.errors.append(f"You selected a face, edge or feature of {names}: the whole part is quoted.")
    return result


def find(ref: PartRef) -> Any:
    """The live object for ``ref``, or Refused when its document or object is gone."""
    doc = App.listDocuments().get(ref.doc_name)
    obj = doc.getObject(ref.obj_name) if doc is not None else None
    if obj is None:
        raise Refused(f"{ref.name} is no longer in the document.")
    return obj
