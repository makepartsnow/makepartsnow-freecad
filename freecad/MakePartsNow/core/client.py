"""Client for the Make Parts Now ``/mcp/cad`` endpoint (MCP over HTTP, JSON-RPC 2.0).

This is the only module that knows the wire format: tool names, argument names
and the field names of every response. The rest of the add-in works with the
dataclasses defined here. Standard library only; it never touches a CAD
program's API, so it runs on the worker thread and in plain-Python tests.

The client is told which add-in it speaks for: ``client_name`` is the
``?client=`` tag and the usage-count client, and ``product`` names the add-in in
the User-Agent. The site answers 403 to urllib's default User-Agent, so every
request sends ``MakePartsNow-<product>/<version>``.

The server is MCP Streamable HTTP in stateless mode and answers with JSON. The
client still accepts a ``text/event-stream`` answer, reading the JSON-RPC
message from its ``data:`` lines.
"""

from __future__ import annotations

import json
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Mapping, Optional, Sequence

PROTOCOL_VERSION = "2025-06-18"
CAD_PATH = "/mcp/cad"
EVENTS_PATH = "/api/agent/v1/client-events"
REQUEST_TIMEOUT = 60.0  # seconds per HTTP request; the server holds a quote call for up to ~20 s
UPLOAD_TIMEOUT = 300.0

# Tool names.
TOOL_GET_OPTIONS = "get_options"
TOOL_CREATE_UPLOAD_LINKS = "create_upload_links"
TOOL_QUOTE_PARTS = "quote_parts"
TOOL_UPDATE_QUOTE = "update_quote"
TOOL_GET_QUOTE = "get_quote"
TOOL_CREATE_CHECKOUT_LINK = "create_checkout_link"
TOOL_DELETE_PARTS = "delete_parts"

# Error codes the client itself produces (the server's codes pass through as-is).
NETWORK_ERROR = "NETWORK_ERROR"
TIMEOUT = "TIMEOUT"
TLS_ERROR = "TLS_ERROR"
PROTOCOL_ERROR = "PROTOCOL_ERROR"
SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
RATE_LIMITED = "RATE_LIMITED"
FILE_TOO_LARGE = "FILE_TOO_LARGE"
UPLOAD_FAILED = "UPLOAD_FAILED"
QUOTE_TIMEOUT = "QUOTE_TIMEOUT"
INVALID_ARGUMENT = "INVALID_ARGUMENT"

TRANSIENT_CODES = frozenset({NETWORK_ERROR, TIMEOUT, SERVICE_UNAVAILABLE})


class CadError(Exception):
    """An error from the server (a tool's ``isError`` result) or from the transport."""

    def __init__(self, code: str, message: str = "", *, transient: Optional[bool] = None) -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message
        self.transient = code in TRANSIENT_CODES if transient is None else transient


# ---------------------------------------------------------------- data shapes


@dataclass(frozen=True)
class Promo:
    code: str
    percent_off: Optional[float]
    note: str


@dataclass(frozen=True)
class Finish:
    id: str
    name: str
    hex: str = ""  # swatch colour, "#rrggbb"; empty when the server sends none


@dataclass(frozen=True)
class Material:
    """One material from ``get_options``: PA12 (SLS) or an FDM plastic, with its finishes or colours."""

    id: str
    name: str
    process: str  # "sls" or "fdm"
    description: str
    build_volume_mm: tuple[float, float, float]
    finishes: tuple[Finish, ...]


DEFAULT_MATERIAL_ID = "pa12"
DEFAULT_MATERIAL_NAME = "PA12 Nylon (Formlabs Nylon 12)"
DEFAULT_BUILD_VOLUME = (165.0, 165.0, 300.0)
DEFAULT_FINISHES = (Finish("gray", "Gray", "#8f9295"), Finish("black", "Black Dyed", "#1d1d1f"))
# Swatch colours for finishes an older server sends without one.
KNOWN_HEX = {"pa12": {"gray": "#8f9295", "black": "#1d1d1f"}}


def default_material() -> Material:
    return Material(
        id=DEFAULT_MATERIAL_ID,
        name=DEFAULT_MATERIAL_NAME,
        process="sls",
        description="Strong, slightly flexible nylon with a matte, lightly textured finish.",
        build_volume_mm=DEFAULT_BUILD_VOLUME,
        finishes=DEFAULT_FINISHES,
    )


@dataclass(frozen=True)
class Options:
    """``material_id``, ``material_name`` and ``finishes`` describe the first material (PA12),
    as older servers sent only that one; ``materials`` lists every material."""

    material_id: str
    material_name: str
    finishes: tuple[Finish, ...]
    max_parts: int
    max_quantity: int
    max_file_mb: float
    file_formats: tuple[str, ...]
    build_volume_mm: tuple[float, float, float]
    speed: str
    free_shipping_threshold: Optional[float]
    quote_valid_days: Optional[int]
    promo: Optional[Promo]
    enabled: bool
    min_client_version: Optional[str]
    materials: tuple[Material, ...] = ()

    def material(self, material_id: Optional[str]) -> Material:
        """The material with ``material_id``, or the first (PA12) when unknown."""
        for material in self.materials:
            if material.id == (material_id or DEFAULT_MATERIAL_ID):
                return material
        return self.materials[0] if self.materials else default_material()


@dataclass(frozen=True)
class UploadLink:
    upload_id: str
    put_url: str
    expires_at: str


@dataclass(frozen=True)
class QuoteItem:
    """One line to quote: a new upload or an already uploaded part."""

    quantity: int
    finish: str
    upload_id: Optional[str] = None
    part_id: Optional[str] = None
    units: Optional[str] = None  # "mm" or "in", STL only
    material: Optional[str] = None  # a material id; the server assumes "pa12" when it is missing


@dataclass(frozen=True)
class PartError:
    """A part's or a quote's error. ``allowed_values`` lists valid answers (units, or the latest quote id)."""

    code: str
    message: str
    allowed_values: tuple[str, ...] = ()


@dataclass(frozen=True)
class QuotedPart:
    part_id: str
    delete_token: str
    file_name: str
    status: str
    quantity: int
    finish: str
    units: Optional[str]
    dimensions_mm: Optional[tuple[float, float, float]]
    unit_price: Optional[float]
    line_total: Optional[float]
    warnings: tuple[str, ...]
    error: Optional[PartError]
    material: str = DEFAULT_MATERIAL_ID
    material_name: str = DEFAULT_MATERIAL_NAME


@dataclass(frozen=True)
class Totals:
    parts_subtotal: Optional[float]
    discount: Optional[float]
    shipping: Optional[float]
    shipping_estimated: bool
    free_shipping: bool
    total_price: Optional[float]
    min_order_adjustment: Optional[float] = None


@dataclass(frozen=True)
class QuoteView:
    """A quote answer. Until every part is ready (``processing`` or ``needs_input``) there is
    no quote yet: ``quote_id`` is None and ``totals`` and the dates are None."""

    status: str  # ready | processing | needs_input | manual_review
    quote_id: Optional[str]
    quote_url: str
    price_valid_until: str
    revised_from: Optional[str]
    parts: tuple[QuotedPart, ...]
    totals: Optional[Totals]
    estimated_ship_date: Optional[str]
    estimated_arrival_date: Optional[str]
    tax_note: str
    promo: Optional[Promo]
    next_step: str
    quote_status: Optional[str] = None  # open | ordered | expired, once a quote exists
    error: Optional[PartError] = None

    @property
    def is_processing(self) -> bool:
        """Still analyzing, with nothing for the user to fix yet (one failed part makes it needs_input)."""
        return self.status == "processing"

    @property
    def is_ready(self) -> bool:
        return self.status == "ready" and bool(self.quote_id) and self.totals is not None

    @property
    def failed_parts(self) -> tuple[QuotedPart, ...]:
        return tuple(p for p in self.parts if p.error is not None or p.status in ("failed", "needs_input"))


@dataclass(frozen=True)
class Checkout:
    status: str  # ready | needs_input | manual_review | not_supported
    checkout_url: str
    total_price: Optional[float]
    link_valid_until: str
    quote_url: str
    promo: Optional[Promo]
    error: Optional[PartError] = None

    @property
    def latest_quote_id(self) -> Optional[str]:
        """On QUOTE_REVISED, the newest revision's id."""
        if self.error is not None and self.error.code == "QUOTE_REVISED" and self.error.allowed_values:
            return self.error.allowed_values[0]
        return None


@dataclass(frozen=True)
class DeleteResult:
    deleted: tuple[str, ...]
    errors: tuple[PartError, ...] = field(default_factory=tuple)
    error_part_ids: tuple[str, ...] = field(default_factory=tuple)


# ------------------------------------------------------------ wire -> objects


def _num(value: Any) -> Optional[float]:
    """Money and sizes may arrive as numbers or numeric strings; anything else is None."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _str(value: Any) -> str:
    return "" if value is None else str(value)


def version_tuple(version: str) -> tuple[int, ...]:
    """Turn "1.2.10" into (1, 2, 10); non-numeric pieces count as 0."""
    out = []
    for piece in str(version).strip().lstrip("vV").split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out)


def is_older(current: str, minimum: Optional[str]) -> bool:
    """True when ``current`` is below the server's ``min_client_version``."""
    if not minimum:
        return False
    a, b = version_tuple(current), version_tuple(minimum)
    width = max(len(a), len(b))
    return a + (0,) * (width - len(a)) < b + (0,) * (width - len(b))


def parse_promo(data: Any) -> Optional[Promo]:
    if not isinstance(data, Mapping) or not data.get("code"):
        return None
    return Promo(code=_str(data.get("code")), percent_off=_num(data.get("percent_off")), note=_str(data.get("note")))


def _parse_volume(value: Any, fallback: tuple[float, float, float]) -> tuple[float, float, float]:
    dims = _parse_dims(value)
    return dims if dims is not None and all(d > 0 for d in dims) else fallback


_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")


def _parse_hex(value: Any) -> str:
    """A ``#rgb`` or ``#rrggbb`` colour, or "" (the palette draws it, so nothing else gets through)."""
    text = _str(value).strip()
    if text.startswith("#") and len(text) in (4, 7) and all(ch in _HEX_DIGITS for ch in text[1:]):
        return text.lower()
    return ""


def parse_material(data: Mapping[str, Any], fallback_volume: tuple[float, float, float]) -> Optional[Material]:
    material_id = _str(data.get("id"))
    if not material_id:
        return None
    known = KNOWN_HEX.get(material_id, {})
    finishes = tuple(
        Finish(id=_str(f.get("id")), name=_str(f.get("name")) or _str(f.get("id")), hex=_parse_hex(f.get("hex")) or known.get(_str(f.get("id")), ""))
        for f in (data.get("finishes") or [])
        if isinstance(f, Mapping) and f.get("id")
    )
    if not finishes and material_id == DEFAULT_MATERIAL_ID:
        finishes = DEFAULT_FINISHES
    process = _str(data.get("process")).lower() or ("sls" if material_id == DEFAULT_MATERIAL_ID else "fdm")
    return Material(
        id=material_id,
        name=_str(data.get("name")) or material_id,
        process=process,
        description=_str(data.get("description")),
        build_volume_mm=_parse_volume(data.get("build_volume_mm"), fallback_volume),
        finishes=finishes,
    )


def parse_options(data: Mapping[str, Any]) -> Options:
    volume = _parse_volume(data.get("build_volume_mm"), DEFAULT_BUILD_VOLUME)
    materials = tuple(
        m for m in (parse_material(raw, volume) for raw in (data.get("materials") or []) if isinstance(raw, Mapping))
        if m is not None and m.finishes
    ) or (default_material(),)
    first = materials[0]
    return Options(
        material_id=first.id,
        material_name=first.name,
        finishes=first.finishes,
        max_parts=int(data.get("max_parts") or 25),
        max_quantity=int(data.get("max_quantity") or 1000),
        max_file_mb=float(data.get("max_file_mb") or 100),
        file_formats=tuple(_str(f).lower() for f in (data.get("file_formats") or ["step", "stp", "stl"])),
        build_volume_mm=volume,
        speed=_str(data.get("speed")) or "standard",
        free_shipping_threshold=_num(data.get("free_shipping_threshold")),
        quote_valid_days=int(data["quote_valid_days"]) if data.get("quote_valid_days") is not None else None,
        promo=parse_promo(data.get("promo")),
        enabled=bool(data.get("enabled", True)),
        min_client_version=_str(data.get("min_client_version")) or None,
        materials=materials,
    )


def parse_upload_links(data: Mapping[str, Any]) -> list[UploadLink]:
    return [
        UploadLink(upload_id=_str(u.get("upload_id")), put_url=_str(u.get("put_url")), expires_at=_str(u.get("expires_at")))
        for u in (data.get("uploads") or [])
        if isinstance(u, Mapping)
    ]


def _parse_error(data: Any) -> Optional[PartError]:
    if not isinstance(data, Mapping):
        return None
    allowed = data.get("allowed_values") or ()
    return PartError(
        code=_str(data.get("code")),
        message=_str(data.get("message")),
        allowed_values=tuple(_str(v) for v in allowed) if isinstance(allowed, (list, tuple)) else (),
    )


def _parse_dims(value: Any) -> Optional[tuple[float, float, float]]:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return None
    nums = [_num(v) for v in value]
    if any(n is None for n in nums):
        return None
    return (nums[0], nums[1], nums[2])  # type: ignore[return-value]


def parse_part(data: Mapping[str, Any]) -> QuotedPart:
    return QuotedPart(
        part_id=_str(data.get("part_id")),
        delete_token=_str(data.get("delete_token")),
        file_name=_str(data.get("file_name")),
        status=_str(data.get("status")) or "processing",
        quantity=int(data.get("quantity") or 0),
        finish=_str(data.get("finish")),
        units=_str(data.get("units")) or None,
        dimensions_mm=_parse_dims(data.get("dimensions_mm")),
        unit_price=_num(data.get("unit_price")),
        line_total=_num(data.get("line_total")),
        warnings=tuple(_str(w) for w in (data.get("warnings") or []) if w),
        error=_parse_error(data.get("error")),
        material=_str(data.get("material")) or DEFAULT_MATERIAL_ID,
        material_name=_str(data.get("material_name")) or (DEFAULT_MATERIAL_NAME if not data.get("material") else _str(data.get("material"))),
    )


def _parse_totals(totals: Any) -> Optional[Totals]:
    if not isinstance(totals, Mapping):
        return None
    return Totals(
        parts_subtotal=_num(totals.get("parts_subtotal")),
        discount=_num(totals.get("discount")),
        shipping=_num(totals.get("shipping")),
        shipping_estimated=bool(totals.get("shipping_estimated")),
        free_shipping=bool(totals.get("free_shipping")),
        total_price=_num(totals.get("total_price")),
        min_order_adjustment=_num(totals.get("min_order_adjustment")),
    )


def parse_quote(data: Mapping[str, Any]) -> QuoteView:
    return QuoteView(
        status=_str(data.get("status")),
        quote_id=_str(data.get("quote_id")) or None,
        quote_url=_str(data.get("quote_url")),
        price_valid_until=_str(data.get("price_valid_until")),
        revised_from=_str(data.get("revised_from")) or None,
        parts=tuple(parse_part(p) for p in (data.get("parts") or []) if isinstance(p, Mapping)),
        totals=_parse_totals(data.get("totals")),
        estimated_ship_date=_str(data.get("estimated_ship_date")) or None,
        estimated_arrival_date=_str(data.get("estimated_arrival_date")) or None,
        tax_note=_str(data.get("tax_note")),
        promo=parse_promo(data.get("promo")),
        next_step=_str(data.get("next_step")),
        quote_status=_str(data.get("quote_status")) or None,
        error=_parse_error(data.get("error")),
    )


def parse_checkout(data: Mapping[str, Any]) -> Checkout:
    return Checkout(
        status=_str(data.get("status")),
        checkout_url=_str(data.get("checkout_url")),
        total_price=_num(data.get("total_price")),
        link_valid_until=_str(data.get("link_valid_until")),
        quote_url=_str(data.get("quote_url")),
        promo=parse_promo(data.get("promo")),
        error=_parse_error(data.get("error")),
    )


def parse_delete(data: Mapping[str, Any]) -> DeleteResult:
    errors = [e for e in (data.get("errors") or []) if isinstance(e, Mapping)]
    return DeleteResult(
        deleted=tuple(_str(p) for p in (data.get("deleted") or [])),
        errors=tuple(PartError(code=_str(e.get("code")), message=_str(e.get("message"))) for e in errors),
        error_part_ids=tuple(_str(e.get("part_id")) for e in errors),
    )


def item_to_wire(item: QuoteItem) -> dict[str, Any]:
    out: dict[str, Any] = {"quantity": int(item.quantity), "finish": item.finish}
    if item.part_id:
        out["part_id"] = item.part_id
    elif item.upload_id:
        out["upload_id"] = item.upload_id
    else:
        raise ValueError("A quote item needs an upload_id or a part_id")
    if item.units:
        out["units"] = item.units
    if item.material:
        out["material"] = item.material
    return out


def requote_items(view: QuoteView, fallback: Sequence[QuoteItem] = ()) -> list[QuoteItem]:
    """The quote's lines as ``part_id`` items, to ask for the same quote again or revise it."""
    items: list[QuoteItem] = []
    for index, part in enumerate(view.parts):
        if not part.part_id:
            raise CadError(PROTOCOL_ERROR, "A quoted part has no part_id.", transient=False)
        old = fallback[index] if index < len(fallback) else None
        items.append(
            QuoteItem(
                quantity=part.quantity or (old.quantity if old else 1),
                finish=part.finish or (old.finish if old else ""),
                part_id=part.part_id,
                units=(old.units if old else None) or None,
                material=(old.material if old and old.material else None) or (part.material if part.material != DEFAULT_MATERIAL_ID else None),
            )
        )
    return items


def split_error_text(text: str) -> tuple[str, str]:
    """``"CODE: message"`` -> ``("CODE", "message")``; text without a code becomes ``("SERVER_ERROR", text)``."""
    head, sep, rest = text.partition(":")
    head = head.strip()
    if sep and head and head.replace("_", "").isalnum() and head.upper() == head:
        return head, rest.strip()
    return "SERVER_ERROR", text.strip()


# ------------------------------------------------------------------ transport



def _same_site(url: str, base_url: str) -> bool:
    """Whether a signed upload link may receive a design: the same scheme, host and port as ``base_url``, or https on
    makepartsnow.com."""
    try:
        parsed = urllib.parse.urlparse(url)
        base = urllib.parse.urlparse(base_url)
        port, base_port = parsed.port, base.port
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    if parsed.scheme == "https" and (host == "makepartsnow.com" or host.endswith(".makepartsnow.com")):
        return True
    return bool(host) and parsed.scheme == base.scheme and host == (base.hostname or "").lower() and port == base_port

@dataclass
class HttpResponse:
    status: int
    headers: dict[str, str]
    body: bytes


Transport = Callable[[str, str, Mapping[str, str], Optional[bytes], float], HttpResponse]


# CA bundles to try when Python's own OpenSSL finds none. FreeCAD's Linux AppImage is built with conda, whose OpenSSL
# looks for certificates at a path on FreeCAD's build machine, so its default context trusts nothing.
SYSTEM_CA_FILES = (
    "/etc/ssl/certs/ca-certificates.crt",  # Debian, Ubuntu, Arch
    "/etc/pki/tls/certs/ca-bundle.crt",  # Fedora, RHEL
    "/etc/ssl/ca-bundle.pem",  # openSUSE
    "/etc/ssl/cert.pem",  # macOS, Alpine
)
_tls_context: Optional[ssl.SSLContext] = None


def _certifi_bundle() -> Optional[str]:
    """certifi's cacert.pem, found without importing certifi (FreeCAD bundles it; the core needs only the stdlib)."""
    import importlib.util  # noqa: PLC0415
    import os.path  # noqa: PLC0415

    try:
        spec = importlib.util.find_spec("certifi")
    except (ImportError, ValueError):
        return None
    if spec is None or not spec.origin:
        return None
    path = os.path.join(os.path.dirname(spec.origin), "cacert.pem")
    return path if os.path.isfile(path) else None


def tls_context(ca_files: Sequence[str] = SYSTEM_CA_FILES, certifi: Callable[[], Optional[str]] = _certifi_bundle) -> ssl.SSLContext:
    """A verifying TLS context: the system's certificates, or, when Python's OpenSSL knows none, the first CA bundle
    found in ``ca_files`` or certifi's. Verification is never turned off."""
    import os.path  # noqa: PLC0415

    context = ssl.create_default_context()
    if context.cert_store_stats().get("x509_ca", 0):
        return context
    for path in [*ca_files, certifi() or ""]:
        if path and os.path.isfile(path):
            try:
                context.load_verify_locations(cafile=path)
            except (OSError, ssl.SSLError):
                continue
            if context.cert_store_stats().get("x509_ca", 0):
                return context
    return context  # still verifying: the request fails with TLS_ERROR rather than trusting anyone


def urllib_transport(method: str, url: str, headers: Mapping[str, str], body: Optional[bytes], timeout: float) -> HttpResponse:
    """Send one HTTP request with urllib, verifying TLS against the system's certificates (see ``tls_context``).

    HTTP error statuses come back as responses; only network-level failures raise.
    """
    global _tls_context
    request = urllib.request.Request(url, data=body, method=method, headers=dict(headers))
    if _tls_context is None:
        _tls_context = tls_context()
    context = _tls_context
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as resp:
            return HttpResponse(resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read())
    except urllib.error.HTTPError as exc:
        data = exc.read() if exc.fp is not None else b""
        return HttpResponse(exc.code, {k.lower(): v for k, v in (exc.headers or {}).items()}, data)
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, ssl.SSLError):
            raise CadError(TLS_ERROR, str(reason), transient=False) from exc
        if isinstance(reason, (socket.timeout, TimeoutError)):
            raise CadError(TIMEOUT, "The server did not answer in time.") from exc
        raise CadError(NETWORK_ERROR, str(reason)) from exc
    except (socket.timeout, TimeoutError) as exc:
        raise CadError(TIMEOUT, "The server did not answer in time.") from exc
    except (ConnectionError, OSError) as exc:
        raise CadError(NETWORK_ERROR, str(exc)) from exc


def _status_error(status: int, body: bytes) -> CadError:
    detail = ""
    try:
        parsed = json.loads(body.decode("utf-8"))
        if isinstance(parsed, Mapping):
            detail = _str(parsed.get("detail") or parsed.get("message") or parsed.get("error"))
    except (ValueError, UnicodeDecodeError):
        detail = body[:200].decode("utf-8", "replace")
    if status == 429:
        return CadError(RATE_LIMITED, detail or "Too many requests.", transient=False)
    if status == 413:
        return CadError(FILE_TOO_LARGE, detail or "File is too large.", transient=False)
    if status in (502, 503, 504):
        return CadError(SERVICE_UNAVAILABLE, detail or f"HTTP {status}")
    if status >= 500:
        return CadError(SERVICE_UNAVAILABLE, detail or f"HTTP {status}", transient=False)
    return CadError(PROTOCOL_ERROR, f"HTTP {status}: {detail}".strip(), transient=False)


def parse_sse(body: str, request_id: Any) -> Optional[dict[str, Any]]:
    """Find the JSON-RPC response with ``request_id`` in a ``text/event-stream`` body."""
    found: Optional[dict[str, Any]] = None
    for event in body.replace("\r\n", "\n").split("\n\n"):
        data = "\n".join(line[5:].lstrip() for line in event.split("\n") if line.startswith("data:"))
        if not data:
            continue
        try:
            message = json.loads(data)
        except ValueError:
            continue
        if isinstance(message, dict) and message.get("id") == request_id and ("result" in message or "error" in message):
            found = message
    return found


# --------------------------------------------------------------------- client


class CadClient:
    """Calls the ``/mcp/cad`` tools. Not thread-safe: use one client per worker job."""

    def __init__(
        self,
        base_url: str,
        version: str,
        *,
        client_name: str,
        product: str,
        transport: Optional[Transport] = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.version = version
        self.client_name = client_name
        self.product = product
        self.endpoint = f"{self.base_url}{CAD_PATH}?client={urllib.parse.quote(client_name)}"
        self._transport = transport or urllib_transport
        self._sleep = sleep
        self._clock = clock
        self._next_id = 0
        self._initialized = False
        self._session_id: Optional[str] = None

    @property
    def user_agent(self) -> str:
        return f"MakePartsNow-{self.product}/{self.version}"

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
            "User-Agent": self.user_agent,
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        return headers

    def _post(self, payload: dict[str, Any]) -> HttpResponse:
        body = json.dumps(payload).encode("utf-8")
        resp = self._transport("POST", self.endpoint, self._headers(), body, REQUEST_TIMEOUT)
        session = resp.headers.get("mcp-session-id")
        if session:
            self._session_id = session
        return resp

    def _rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._next_id += 1
        request_id = self._next_id
        resp = self._post({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        if resp.status >= 400:
            raise _status_error(resp.status, resp.body)
        text = resp.body.decode("utf-8", "replace")
        if "text/event-stream" in resp.headers.get("content-type", ""):
            message = parse_sse(text, request_id)
            if message is None:
                raise CadError(PROTOCOL_ERROR, "The server's event stream had no answer.", transient=True)
        else:
            try:
                message = json.loads(text)
            except ValueError as exc:
                raise CadError(PROTOCOL_ERROR, "The server's answer was not JSON.", transient=False) from exc
        if not isinstance(message, dict):
            raise CadError(PROTOCOL_ERROR, "Unexpected answer from the server.", transient=False)
        if "error" in message:
            err = message.get("error") or {}
            raise CadError(PROTOCOL_ERROR, _str(err.get("message")) or "JSON-RPC error", transient=False)
        result = message.get("result")
        if not isinstance(result, dict):
            raise CadError(PROTOCOL_ERROR, "The server's answer had no result.", transient=False)
        return result

    def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        self._rpc(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": f"makepartsnow-{self.product.lower()}", "version": self.version},
            },
        )
        # A notification has no id and no answer body (202 Accepted).
        resp = self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        if resp.status >= 400 and resp.status != 405:
            raise _status_error(resp.status, resp.body)
        self._initialized = True

    def call_tool(self, name: str, arguments: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """Call one tool and return its structured result, raising CadError for an ``isError`` result."""
        self._ensure_initialized()
        result = self._rpc("tools/call", {"name": name, "arguments": arguments or {}})
        content = result.get("content") or []
        text = ""
        if content and isinstance(content[0], Mapping):
            text = _str(content[0].get("text"))
        if result.get("isError"):
            code, message = split_error_text(text or "SERVER_ERROR: The server reported an error.")
            if code == "SERVER_ERROR" and text:
                code = INVALID_ARGUMENT  # the MCP SDK's own argument-validation text carries no code
            raise CadError(code, message, transient=code == SERVICE_UNAVAILABLE)
        structured = result.get("structuredContent")
        if isinstance(structured, dict):
            return structured
        try:
            parsed = json.loads(text)
        except ValueError as exc:
            raise CadError(PROTOCOL_ERROR, "The tool's answer was not JSON.", transient=False) from exc
        if not isinstance(parsed, dict):
            raise CadError(PROTOCOL_ERROR, "The tool's answer was not an object.", transient=False)
        return parsed

    # -- tools

    def get_options(self) -> Options:
        return parse_options(self.call_tool(TOOL_GET_OPTIONS))

    def create_upload_links(self, count: int, file_names: Optional[Sequence[str]] = None) -> list[UploadLink]:
        args: dict[str, Any] = {"count": int(count)}
        if file_names:
            args["file_names"] = list(file_names)
        links = parse_upload_links(self.call_tool(TOOL_CREATE_UPLOAD_LINKS, args))
        if len(links) != count:
            raise CadError(PROTOCOL_ERROR, f"Asked for {count} upload links, got {len(links)}.", transient=False)
        return links

    def quote_parts(self, items: Sequence[QuoteItem], ship_to_zip: str) -> QuoteView:
        args = {"items": [item_to_wire(i) for i in items], "ship_to_zip": ship_to_zip}
        return parse_quote(self.call_tool(TOOL_QUOTE_PARTS, args))

    def update_quote(self, quote_id: str, items: Optional[Sequence[QuoteItem]] = None, ship_to_zip: Optional[str] = None) -> QuoteView:
        args: dict[str, Any] = {"quote_id": quote_id}
        if items is not None:
            args["items"] = [item_to_wire(i) for i in items]
        if ship_to_zip:
            args["ship_to_zip"] = ship_to_zip
        return parse_quote(self.call_tool(TOOL_UPDATE_QUOTE, args))

    def get_quote(self, quote_id: str) -> QuoteView:
        return parse_quote(self.call_tool(TOOL_GET_QUOTE, {"quote_id": quote_id}))

    def create_checkout_link(self, quote_id: str) -> Checkout:
        return parse_checkout(self.call_tool(TOOL_CREATE_CHECKOUT_LINK, {"quote_id": quote_id}))

    def delete_parts(self, parts: Sequence[tuple[str, str]]) -> DeleteResult:
        """``parts`` is a list of ``(part_id, delete_token)``."""
        args = {"parts": [{"part_id": pid, "delete_token": tok} for pid, tok in parts]}
        return parse_delete(self.call_tool(TOOL_DELETE_PARTS, args))

    def wait_for_quote(
        self,
        first: QuoteView,
        items: Sequence[QuoteItem],
        ship_to_zip: str,
        *,
        max_wait: float,
        progress: Optional[Callable[[QuoteView, float], None]] = None,
        poll_pause: float = 2.0,
    ) -> QuoteView:
        """Re-ask ``quote_parts`` with the returned part ids while any part is ``processing``.

        Each part in the answer carries its own quantity and finish, so the next call
        is built from the parts; ``items`` (the lines sent first, in order) only fill
        in a value the answer left out.
        """
        view = first
        start = self._clock()
        while view.is_processing:
            elapsed = self._clock() - start
            if elapsed >= max_wait:
                raise CadError(QUOTE_TIMEOUT, "The parts are still being analyzed.", transient=False)
            if progress:
                progress(view, elapsed)
            self._sleep(poll_pause)
            view = self.quote_parts(requote_items(view, items), ship_to_zip)
        return view

    # -- uploads and usage counts (plain HTTP, not MCP)

    def put_file(self, link: UploadLink, data: bytes, file_name: str) -> None:
        """PUT one file's raw bytes to its signed link (no auth header). A link works once."""
        if not _same_site(link.put_url, self.base_url):
            # Design files go only to the server this client talks to (or makepartsnow.com), never to a host a
            # response names, whatever it says.
            raise CadError(UPLOAD_FAILED, "The upload link isn't on makepartsnow.com.", transient=False)
        headers = {
            "Content-Type": "application/octet-stream",
            "X-File-Name": urllib.parse.quote(file_name),
            "User-Agent": self.user_agent,
        }
        resp = self._transport("PUT", link.put_url, headers, data, UPLOAD_TIMEOUT)
        if resp.status in (200, 201, 204):
            return
        error = _status_error(resp.status, resp.body)
        if error.code == PROTOCOL_ERROR:
            error = CadError(UPLOAD_FAILED, error.message, transient=False)
        raise error

    def send_client_events(self, events: Mapping[str, int]) -> bool:
        """POST opt-in usage counts. Returns False on any failure; never raises."""
        payload = {"client": self.client_name, "version": self.version, "events": {k: int(v) for k, v in events.items()}}
        headers = {"Content-Type": "application/json", "User-Agent": self.user_agent}
        try:
            resp = self._transport("POST", f"{self.base_url}{EVENTS_PATH}", headers, json.dumps(payload).encode("utf-8"), 15.0)
        except Exception:  # noqa: BLE001 - fire and forget
            return False
        return 200 <= resp.status < 300


# ------------------------------------------------- JSON round trip (worker -> main thread)


def to_jsonable(obj: Any) -> Any:
    """A dataclass from this module as plain JSON data (for the worker's event to the main thread)."""
    return asdict(obj)


def _promo_from(data: Any) -> Optional[Promo]:
    return Promo(**data) if isinstance(data, Mapping) else None


def _material_from(data: Mapping[str, Any]) -> Material:
    values = dict(data)
    values["finishes"] = tuple(Finish(**f) for f in data.get("finishes") or ())
    values["build_volume_mm"] = tuple(data.get("build_volume_mm") or DEFAULT_BUILD_VOLUME)
    return Material(**values)


def options_from_dict(data: Mapping[str, Any]) -> Options:
    values = dict(data)
    values["finishes"] = tuple(Finish(**f) for f in data.get("finishes") or ())
    values["file_formats"] = tuple(data.get("file_formats") or ())
    values["build_volume_mm"] = tuple(data.get("build_volume_mm") or DEFAULT_BUILD_VOLUME)
    values["promo"] = _promo_from(data.get("promo"))
    values["materials"] = tuple(_material_from(m) for m in data.get("materials") or ())
    return Options(**values)


def quote_from_dict(data: Mapping[str, Any]) -> QuoteView:
    parts = []
    for p in data.get("parts") or ():
        values = dict(p)
        values["dimensions_mm"] = tuple(p["dimensions_mm"]) if p.get("dimensions_mm") else None
        values["warnings"] = tuple(p.get("warnings") or ())
        values["error"] = _error_from(p.get("error"))
        parts.append(QuotedPart(**values))
    values = dict(data)
    values["parts"] = tuple(parts)
    values["totals"] = Totals(**data["totals"]) if data.get("totals") else None
    values["promo"] = _promo_from(data.get("promo"))
    values["error"] = _error_from(data.get("error"))
    return QuoteView(**values)


def _error_from(data: Any) -> Optional[PartError]:
    if not isinstance(data, Mapping):
        return None
    return PartError(code=data["code"], message=data["message"], allowed_values=tuple(data.get("allowed_values") or ()))


def checkout_from_dict(data: Mapping[str, Any]) -> Checkout:
    values = dict(data)
    values["promo"] = _promo_from(data.get("promo"))
    values["error"] = _error_from(data.get("error"))
    return Checkout(**values)


def delete_from_dict(data: Mapping[str, Any]) -> DeleteResult:
    return DeleteResult(
        deleted=tuple(data.get("deleted") or ()),
        errors=tuple(_error_from(e) for e in data.get("errors") or ()),
        error_part_ids=tuple(data.get("error_part_ids") or ()),
    )
