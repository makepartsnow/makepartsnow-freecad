"""Plain-data pieces of the panel's view model: money, sizes, dates, materials, the quote.

Everything a panel shows is built here or in ``controller`` as JSON-ready
dicts of display text. The panel only lays it out (and escapes it, for HTML),
so the formatting is tested in Python.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Optional, Sequence

from .client import Checkout, Material, Options, Promo, QuotedPart, QuoteView, default_material
from .host import Host
from .messages import describe_error, describe_part_error
from .text import short
from .units import UnitsCheck, fits_build_volume

TIMES = "×"
DASH = "—"
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
TAX_NOTE = "Prices in USD before sales tax"
GROUPS = (("sls", "SLS nylon"), ("fdm", "FDM plastics"))
PROCESS_SHORT = {"sls": "SLS", "fdm": "FDM"}


def money(value: Optional[float]) -> str:
    return DASH if value is None else f"${value:,.2f}"


def dims_text(dims: Optional[Sequence[float]]) -> str:
    if not dims:
        return ""
    return f" {TIMES} ".join(f"{d:.1f}" for d in dims) + " mm"


def volume_text(volume: Sequence[float]) -> str:
    return f" {TIMES} ".join(f"{v:g}" for v in volume) + " mm"


def date_text(value: Optional[str]) -> str:
    """"2026-10-19" -> "Mon, Oct 19" (English, whatever the computer's locale)."""
    if not value:
        return ""
    try:
        day = date.fromisoformat(value[:10])
    except ValueError:
        return short(value)
    return f"{DAYS[day.weekday()]}, {MONTHS[day.month - 1]} {day.day}"


def promo_view(promo: Optional[Promo]) -> Optional[dict[str, str]]:
    if promo is None or not promo.code:
        return None
    lead = f"{promo.percent_off:g}% off your first order" if promo.percent_off else "A discount on your first order"
    return {"code": short(promo.code), "text": f"{lead} {DASH} use code"}


# ------------------------------------------------------------------ materials


def materials(options: Optional[Options]) -> list[Material]:
    if options is not None and options.materials:
        return list(options.materials)
    return [default_material()]


def material_view(material: Material) -> dict[str, Any]:
    return {
        "id": material.id,
        "name": short(material.name),
        "short": short(_short_name(material)),
        "process": material.process,
        "process_label": PROCESS_SHORT.get(material.process, material.process.upper()),
        "description": short(material.description),
        "build_volume": volume_text(material.build_volume_mm),
        "finishes": [{"id": f.id, "name": short(f.name), "hex": f.hex or "#cccccc"} for f in material.finishes],
    }


def _short_name(material: Material) -> str:
    """"PA12 Nylon (Formlabs Nylon 12)" -> "PA12 Nylon"; FDM names are short already."""
    return material.name.split(" (")[0]


def groups_view(items: Sequence[Material]) -> list[dict[str, Any]]:
    out = []
    for process, label in GROUPS:
        ids = [m.id for m in items if m.process == process]
        if ids:
            out.append({"id": process, "label": label, "materials": ids})
    others = [m.id for m in items if m.process not in dict(GROUPS)]
    if others:
        out.append({"id": "other", "label": "Other", "materials": others})
    return out


def finish_of(material: Material, finish_id: str) -> tuple[str, str]:
    """(name, hex) of a finish of ``material``; falls back to the id itself."""
    for finish in material.finishes:
        if finish.id == finish_id:
            return finish.name, finish.hex or "#cccccc"
    return finish_id, "#cccccc"


# ---------------------------------------------------------------------- quote


def check_rows(
    part: QuotedPart,
    units: Optional[UnitsCheck],
    material: Material,
    local_dims: Optional[Sequence[float]],
    host: Optional[Host] = None,
) -> list[dict[str, str]]:
    """The DFM checklist for one quoted part: ok, warn or fail rows."""
    program = host.program if host is not None else "your design"
    rows: list[dict[str, str]] = []
    if units is None:
        rows.append({"state": "fail", "text": f"Units match {program}", "detail": "The server did not report this part's size."})
    elif units.ok:
        rows.append({"state": "ok", "text": f"Units match {program}", "detail": ""})
    else:
        detail = f"{program} {dims_text(units.local_mm)}, quoted {dims_text(units.server_mm)}"
        if units.hint:
            detail += f": {units.hint}"
        rows.append({"state": "fail", "text": f"Units don't match {program}", "detail": detail + ". Check the part's units, then quote again."})
    size = part.dimensions_mm or local_dims
    volume = volume_text(material.build_volume_mm)
    if size is not None and fits_build_volume(size, material.build_volume_mm):
        rows.append({"state": "ok", "text": f"Fits the {volume} build volume", "detail": ""})
    else:
        rows.append({"state": "fail", "text": f"Larger than the {volume} build volume", "detail": "Split the part, or choose another material."})
    rows.append({"state": "ok", "text": "One solid body", "detail": ""})
    for warning in part.warnings:
        rows.append({"state": "warn", "text": short(warning), "detail": ""})
    if part.error is not None:
        rows.append({"state": "fail", "text": short(describe_part_error(part.error.code, part.error.message, host)), "detail": ""})
    return rows


def quote_card(
    key: str,
    name: str,
    part: QuotedPart,
    units: Optional[UnitsCheck],
    material: Material,
    local_dims: Optional[Sequence[float]],
    host: Optional[Host] = None,
) -> dict[str, Any]:
    finish_name, finish_hex = finish_of(material, part.finish)
    return {
        "key": key,
        "name": short(name),
        "dims": dims_text(part.dimensions_mm or local_dims),
        "material": short(_short_name(material) if part.material == material.id else part.material_name),
        "process_label": PROCESS_SHORT.get(material.process, material.process.upper()),
        "finish_name": short(finish_name),
        "finish_hex": finish_hex,
        "quantity": part.quantity,
        "unit_price": money(part.unit_price),
        "line_total": money(part.line_total),
        "checks": check_rows(part, units, material, local_dims, host),
    }


def summary(view: QuoteView) -> Optional[dict[str, Any]]:
    totals = view.totals
    if totals is None:
        return None
    count = sum(p.quantity for p in view.parts)
    return {
        "parts_label": f"Parts ({count} unit{'' if count == 1 else 's'})",  # quantities summed, not part cards
        "parts": money(totals.parts_subtotal),
        "discount": f"-{money(abs(totals.discount))}" if totals.discount else None,
        "min_order": money(totals.min_order_adjustment) if totals.min_order_adjustment else None,
        "shipping": "Free shipping" if totals.free_shipping else money(totals.shipping) + (" (estimated)" if totals.shipping_estimated else ""),
        "free_shipping": bool(totals.free_shipping),
        "total": money(totals.total_price),
        "ships_by": date_text(view.estimated_ship_date),
        "arrives_by": date_text(view.estimated_arrival_date),
        "tax_note": TAX_NOTE,
        "valid_until": date_text(view.price_valid_until),
    }


def checkout_view(checkout: Checkout, opened: bool, host: Optional[Host] = None) -> dict[str, Any]:
    reasons = {
        "needs_input": "The quote needs attention before it can be ordered.",
        "manual_review": "A person needs to review this quote before it can be ordered. We'll email you.",
        "not_supported": "This quote can't be ordered online.",
    }
    ready = checkout.status == "ready"
    return {
        "ready": ready,
        "opened": opened,
        "url": checkout.checkout_url if ready else "",
        "total": money(checkout.total_price) if checkout.total_price is not None else "",
        "valid_until": date_text(checkout.link_valid_until),
        "reason": "" if ready else reasons.get(checkout.status, "Checkout is not available for this quote."),
        "error": describe_error(checkout.error.code, checkout.error.message, host) if checkout.error is not None and not ready else "",
        "quote_url": checkout.quote_url,
    }
