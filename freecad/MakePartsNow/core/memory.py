"""A quote line as a compact JSON value, for an add-in that saves the quote into the design.

Only the payload is shared. Writing it is host code (Fusion stores it as a
component attribute, as one undoable step; FreeCAD v1 doesn't save quotes). The
value holds the price, quantity, material, finish, date and the quote link; it
never holds the delete token.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

ATTR_GROUP = "MakePartsNow"
ATTR_NAME = "quote"
PAYLOAD_VERSION = 1
MAX_VALUE_BYTES = 2 * 1024 * 1024  # Fusion's documented limit per attribute value; a sane cap for any host


def build_payload(
    *,
    quote_id: str,
    quote_url: str,
    price_valid_until: str,
    part_id: str,
    file_name: str,
    quantity: int,
    finish: str,
    unit_price: Optional[float],
    line_total: Optional[float],
    material: str,
    saved_at: Optional[datetime] = None,
) -> str:
    """The attribute value: compact JSON, without the delete token."""
    data: dict[str, Any] = {
        "v": PAYLOAD_VERSION,
        "saved_at": (saved_at or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
        "quote_id": quote_id,
        "quote_url": quote_url,
        "price_valid_until": price_valid_until,
        "part_id": part_id,
        "file_name": file_name,
        "material": material,
        "finish": finish,
        "quantity": int(quantity),
        "unit_price": unit_price,
        "line_total": line_total,
        "currency": "USD",
        "prices_before_tax": True,
    }
    value = json.dumps(data, separators=(",", ":"), sort_keys=True)
    if len(value.encode("utf-8")) > MAX_VALUE_BYTES:
        raise ValueError("The saved quote is larger than a design attribute allows")
    return value

