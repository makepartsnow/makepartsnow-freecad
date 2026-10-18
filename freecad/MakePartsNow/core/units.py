"""Units check: the part's size in the CAD program against the size the server measured.

The host gives the local bounding box in mm (Fusion converts from its internal
centimeters; FreeCAD is in mm already). Both sides are sorted, which makes the
check independent of axis order. A mismatch beyond ``max(0.1 mm, 0.5%)`` on any axis blocks
ordering, and common scale mistakes (10x, 25.4x) are named.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

ABS_TOLERANCE_MM = 0.1
REL_TOLERANCE = 0.005

KNOWN_SCALES = (
    (10.0, "10x larger: the file may have been read as centimeters instead of millimeters"),
    (0.1, "10x smaller: the file may have been read as millimeters instead of centimeters"),
    (25.4, "25.4x larger: the file may have been read as inches instead of millimeters"),
    (1 / 25.4, "25.4x smaller: the file may have been read as millimeters instead of inches"),
)


def sorted_dims(dims: Sequence[float]) -> tuple[float, float, float]:
    values = sorted(float(d) for d in dims)
    return (values[0], values[1], values[2])


def tolerance_mm(size_mm: float) -> float:
    return max(ABS_TOLERANCE_MM, REL_TOLERANCE * abs(size_mm))


@dataclass(frozen=True)
class UnitsCheck:
    ok: bool
    local_mm: tuple[float, float, float]
    server_mm: Optional[tuple[float, float, float]]
    worst_delta_mm: float
    hint: str = ""

    def summary(self, program: str) -> str:
        """One line for a log or a tooltip; ``program`` is the CAD program's name."""
        if self.server_mm is None:
            return "Units check: the server did not report a size."
        local = " x ".join(f"{d:.2f}" for d in self.local_mm)
        server = " x ".join(f"{d:.2f}" for d in self.server_mm)
        if self.ok:
            return f"Units check passed ({program} {local} mm, quoted {server} mm)."
        text = f"Units check FAILED: {program} {local} mm, quoted {server} mm"
        return f"{text} ({self.hint})." if self.hint else f"{text}."


def _scale_hint(local: Sequence[float], server: Sequence[float]) -> str:
    if not local[2]:
        return ""
    ratio = server[2] / local[2]
    for scale, text in KNOWN_SCALES:
        if abs(ratio - scale) <= scale * 0.02:
            return f"the quoted part is {text}"
    return ""


def compare(local_mm: Sequence[float], server_mm: Optional[Sequence[float]]) -> UnitsCheck:
    """Compare sorted local and server extents with ``max(0.1 mm, 0.5%)`` per axis."""
    local = sorted_dims(local_mm)
    if server_mm is None:
        return UnitsCheck(ok=False, local_mm=local, server_mm=None, worst_delta_mm=float("inf"))
    server = sorted_dims(server_mm)
    ok = True
    worst = 0.0
    for a, b in zip(local, server):
        delta = abs(a - b)
        worst = max(worst, delta)
        if delta > tolerance_mm(a) + 1e-9:
            ok = False
    return UnitsCheck(ok=ok, local_mm=local, server_mm=server, worst_delta_mm=worst, hint="" if ok else _scale_hint(local, server))


def best_match(candidates: Sequence[Sequence[float]], server_mm: Optional[Sequence[float]]) -> UnitsCheck:
    """Check against several local boxes (component space, world space) and keep the best result.

    A STEP export of a component is expected to be in the component's own
    coordinates, but if the CAD program writes it in assembly coordinates a
    rotated occurrence changes the axis-aligned box. Either one matching passes.
    """
    checks = [compare(c, server_mm) for c in candidates]
    passing = [c for c in checks if c.ok]
    if passing:
        return min(passing, key=lambda c: c.worst_delta_mm)
    return min(checks, key=lambda c: c.worst_delta_mm)


def fits_build_volume(dims_mm: Sequence[float], volume_mm: Sequence[float]) -> bool:
    """True when the part fits the build volume in some axis-aligned orientation."""
    return all(d <= v + 1e-9 for d, v in zip(sorted_dims(dims_mm), sorted_dims(volume_mm)))
