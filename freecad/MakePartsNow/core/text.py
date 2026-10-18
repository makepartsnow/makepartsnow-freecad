"""Small text and link helpers every panel uses."""

from __future__ import annotations

import urllib.parse
from typing import Any

MAX_TEXT = 200


def short(text: Any) -> str:
    """Server or design text, trimmed for display."""
    value = "" if text is None else str(text)
    return value if len(value) <= MAX_TEXT else value[: MAX_TEXT - 1] + "…"


def is_allowed_link(url: str, base_url: str) -> bool:
    """Links a panel may open in the browser: https on makepartsnow.com (or the dev server)."""
    try:
        parsed = urllib.parse.urlparse(url)
        base = urllib.parse.urlparse(base_url)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    if parsed.scheme == "https" and (host == "makepartsnow.com" or host.endswith(".makepartsnow.com")):
        return True
    return bool(host) and parsed.scheme == base.scheme and host == (base.hostname or "").lower() and parsed.port == base.port
