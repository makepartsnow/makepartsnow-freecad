"""The panel's brain: one object that owns the state and answers the user's actions.

``Controller`` runs on the CAD program's main thread only. The panel calls its
action methods (``get_quote``, ``set_part``, ``order``...); the CAD program's
selection event calls ``on_selection_changed``; worker results arrive through
``worker.Bridge``. After every change it hands the whole view model to the
panel (``cad.push(state)``), and the panel draws only from that.

It talks to the CAD program through a small ``cad`` adapter, so tests drive it
with a fake:

* ``cad.selected_parts(explicit) -> (parts, notices)``: the selected parts as
  ``PartRef``-like objects (``key``, ``name``, ``local_dims_mm``,
  ``dims_candidates``), plus refusals to show. ``explicit`` is False for a
  selection the user didn't ask to add, which should give no notices.
* ``cad.export(part, path, fmt) -> bool``: write one part as STEP or STL (mm).
* ``cad.push(state)``, ``cad.progress(progress)``, ``cad.toast(text, kind)``:
  the panel.
* ``cad.open_url(url) -> bool``: the system browser.
* ``cad.defer(fn)``: call ``fn`` on the main thread once the program is idle
  (lets the panel repaint between part exports).
* optional ``cad.save_quote(part, payload) -> bool``: save the quote into the
  design (Fusion). Without it, the panel offers no save.

Network work runs on worker threads through ``bridge``; the model is read and
exported only through ``cad``, on the main thread. Nothing is uploaded until the
user accepted the consent text and clicked the quote button.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol, Sequence

from . import consent, export, flow, memory, viewmodel
from . import settings as settings_mod
from .client import (
    CadClient,
    Material,
    Options,
    QuoteItem,
    QuoteView,
    checkout_from_dict,
    delete_from_dict,
    is_older,
    options_from_dict,
    quote_from_dict,
)
from .host import (
    DEFAULT_FINISH_ID,
    DEFAULT_MATERIAL_ID,
    DEFAULT_MAX_FILE_MB,
    DEFAULT_MAX_QUANTITY,
    MAX_PARTS,
    PRIVACY_URL,
    QUOTE_WAIT_SECONDS,
    SUBLINE,
    TERMS_URL,
    Host,
)
from .messages import REUPLOAD_CODES, describe_error, describe_part_error
from .telemetry import Counter
from .text import is_allowed_link, short
from .units import UnitsCheck, best_match, fits_build_volume

# Screens.
CONSENT, PARTS, WORKING, QUOTE, ORDERED, ERROR = "consent", "parts", "working", "quote", "ordered", "error"

PAUSED_CODES = frozenset({"SERVICE_PAUSED"})
OFFLINE_CODES = frozenset({"NETWORK_ERROR", "TIMEOUT", "TLS_ERROR", "SERVICE_UNAVAILABLE"})
FORM_CODES = frozenset({"INVALID_ZIP", "ZIP_REQUIRED"})
PHASES = (("export", "Export"), ("upload", "Upload"), ("analyze", "Analyze"), ("price", "Price"))
UNITS = ("mm", "in")

SAVED_TEXT = (
    "Quote saved to {count} part{s} in this design. Save the design to keep it. "
    "Anyone you share the design with can see the price and quote link."
)


class PartRef(Protocol):
    """What the controller reads from a host's selected part."""

    key: str  # stable while the part exists, unique in the document
    name: str
    local_dims_mm: tuple[float, float, float]  # sorted, smallest first
    dims_candidates: Sequence[Sequence[float]]  # boxes the server's size may match (best_match)


@dataclass
class Line:
    """One part in the panel: what the user chose for it, and the server's last word on it."""

    part: Any  # a PartRef
    quantity: int = 1
    material: str = DEFAULT_MATERIAL_ID
    finish: str = DEFAULT_FINISH_ID
    units: Optional[str] = None  # the user's answer to UNITS_CONFIRMATION_REQUIRED (STL only)
    error: str = ""
    error_code: str = ""
    units_choices: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        return self.part.key

    def choice(self) -> tuple[str, int, str, str]:
        return (self.key, self.quantity, self.material, self.finish)


@dataclass
class ExportRun:
    """Parts being exported one per idle tick, so the panel can show "Exporting 2 of 5"."""

    directory: str
    lines: list[Line]
    zip_code: str
    fmt: str = "step"
    index: int = 0
    files: list[Optional[export.ExportedFile]] = field(default_factory=list)
    failed: list[int] = field(default_factory=list)
    used: set[str] = field(default_factory=set)
    cancelled: bool = False


def make_client(host: Host, current: settings_mod.Settings) -> CadClient:
    return CadClient(current.effective_base_url(host), host.version, client_name=host.client_name, product=host.product)


class Controller:
    def __init__(
        self,
        host: Host,
        cad: Any,
        bridge: Any,
        *,
        load_settings: Callable[[], settings_mod.Settings],
        save_settings: Callable[[settings_mod.Settings], Any],
        client_factory: Optional[Callable[[settings_mod.Settings], CadClient]] = None,
    ) -> None:
        self.host = host
        self.cad = cad
        self.bridge = bridge
        self._save_settings = save_settings
        self.client_factory = client_factory or (lambda current: make_client(host, current))
        self.settings = load_settings()
        self.counter = Counter(self.settings.telemetry_opt_in, host.events)
        self.seq = 0
        self.screen = PARTS if self.settings.has_consent(host) else CONSENT
        self.options: Optional[Options] = None
        self.options_loading = False
        self.lines: list[Line] = []
        self.notices: list[str] = []
        self.form_error = ""
        self.zip = self.settings.zip
        self.quote: Optional[QuoteView] = None
        self.quote_keys: list[str] = []
        self.quote_lines: list[Line] = []  # copies of the lines as they were quoted
        self.units: list[Optional[UnitsCheck]] = []
        self.quote_notice: Optional[dict[str, str]] = None
        self.part_cache: dict[str, tuple[str, tuple[float, float, float]]] = {}
        self.uploaded: dict[str, str] = {}  # part_id -> first non-empty delete token
        self.job: Optional[str] = None  # "quote" | "checkout" | "delete" while one runs
        self.progress: Optional[dict[str, Any]] = None
        self.question: Optional[dict[str, Any]] = None
        self.error: Optional[dict[str, Any]] = None
        self.checkout: Optional[dict[str, Any]] = None
        self.export_run: Optional[ExportRun] = None
        self.ready_seen = False

    # ------------------------------------------------------------- plumbing

    def push(self) -> dict[str, Any]:
        """Hand the whole view model to the panel; returns it too."""
        state = self.state()
        try:
            self.cad.push(state)
        except Exception:  # noqa: BLE001 - the panel may be closed; the next ready() pushes again
            pass
        return state

    def toast(self, text: str, kind: str = "info") -> None:
        try:
            self.cad.toast(text, kind)
        except Exception:  # noqa: BLE001
            pass

    def _send_progress(self) -> None:
        try:
            self.cad.progress(self.progress)
        except Exception:  # noqa: BLE001
            pass

    def describe(self, code: str, message: str = "") -> str:
        return describe_error(code, message, self.host)

    @property
    def can_save(self) -> bool:
        return callable(getattr(self.cad, "save_quote", None))

    # ------------------------------------------------------------ the state

    @property
    def max_quantity(self) -> int:
        return self.options.max_quantity if self.options else DEFAULT_MAX_QUANTITY

    @property
    def max_parts(self) -> int:
        return self.options.max_parts if self.options else MAX_PARTS

    def material(self, material_id: str) -> Material:
        items = viewmodel.materials(self.options)
        for item in items:
            if item.id == material_id:
                return item
        return items[0]

    def state(self) -> dict[str, Any]:
        self.seq += 1
        items = viewmodel.materials(self.options)
        promo = (self.quote.promo if self.quote is not None and self.quote.promo else None) or (self.options.promo if self.options else None)
        test = self.settings.is_test(self.host)
        return {
            "seq": self.seq,
            "version": self.host.version,
            "screen": self.screen,
            "subline": SUBLINE,
            "test": {"on": True, "base_url": self.settings.effective_base_url(self.host)} if test else None,
            "consent": consent.consent_view(self.host) if self.screen == CONSENT else None,
            "materials": [viewmodel.material_view(m) for m in items],
            "groups": viewmodel.groups_view(items),
            "parts": [self._line_view(line) for line in self.lines],
            "max_parts": self.max_parts,
            "max_quantity": self.max_quantity,
            "notices": list(self.notices),
            "form_error": self.form_error,
            "zip": self.zip,
            "can_quote": self.job is None and self.export_run is None and bool(self.lines),
            "has_quote": self.quote is not None,
            "options_loading": self.options_loading,
            "progress": self.progress,
            "question": self.question,
            "quote": self._quote_view(),
            "checkout": self.checkout,
            "error": self.error,
            "uploaded_count": len(self.uploaded),
            "can_delete": bool(self.uploaded) and self.job is None,
            "promo": viewmodel.promo_view(promo),
            "links": {"help": self.host.help_url, "privacy": PRIVACY_URL, "terms": TERMS_URL},
            "not_affiliated": self.host.not_affiliated,
        }

    def _line_view(self, line: Line) -> dict[str, Any]:
        material = self.material(line.material)
        too_big = not fits_build_volume(line.part.local_dims_mm, material.build_volume_mm)
        error = line.error
        if not error and too_big:
            error = f"Too large for {viewmodel._short_name(material)} ({viewmodel.volume_text(material.build_volume_mm)}). Split it, or choose another material."
        return {
            "key": line.key,
            "name": short(line.part.name),
            "dims": viewmodel.dims_text(line.part.local_dims_mm),
            "quantity": line.quantity,
            "material": material.id,
            "finish": line.finish,
            "error": error,
            "units": line.units,
            "units_choices": list(line.units_choices),
        }

    # ---------------------------------------------------------- the panel

    def ready(self) -> None:
        """The panel is shown: fetch the options (after consent) and draw."""
        self.ready_seen = True
        if self.screen != CONSENT:
            self._ensure_options()
        self.push()

    def accept_consent(self, telemetry: bool = False) -> None:
        if self.screen != CONSENT:
            return
        self.settings.accept_consent(self.host, bool(telemetry))
        self._save()
        self.counter = Counter(self.settings.telemetry_opt_in, self.host.events)
        self.counter.record("consent_accepted")
        self.screen = PARTS
        self._ensure_options()
        self.add_from_selection(explicit=False, quiet=True)
        self.push()
        self.send_counts()

    def add_selected(self) -> None:
        if self.screen == PARTS and self.export_run is None:
            self.add_from_selection(explicit=True)
            self.push()

    def remove_part(self, key: str) -> None:
        if self.screen != PARTS or self.export_run is not None:
            return
        self.lines = [line for line in self.lines if line.key != key]
        self.notices = []
        self.form_error = ""
        self.push()

    def clear_parts(self) -> None:
        if self.screen == PARTS and self.export_run is None:
            self.lines, self.notices, self.form_error = [], [], ""
            self.push()

    def set_part(
        self,
        key: str,
        *,
        quantity: Optional[int] = None,
        material: Optional[str] = None,
        finish: Optional[str] = None,
        units: Optional[str] = None,
    ) -> None:
        line = self._line(key)
        if line is None or self.screen != PARTS:
            return
        if quantity is not None and not isinstance(quantity, bool):
            line.quantity = max(1, min(self.max_quantity, int(quantity)))
        if material is not None:
            chosen = self.material(material)
            if chosen.id != line.material:
                line.material = chosen.id
                ids = [f.id for f in chosen.finishes]
                if line.finish not in ids:
                    line.finish = ids[0] if ids else DEFAULT_FINISH_ID
        if finish is not None and finish in [f.id for f in self.material(line.material).finishes]:
            line.finish = finish
        if units is not None and units in (line.units_choices or UNITS):
            line.units = units
            if line.error_code == "UNITS_CONFIRMATION_REQUIRED":
                line.error, line.error_code = "", ""
        self.form_error = ""
        self.push()

    def set_zip(self, value: str) -> None:
        self.zip = settings_mod.normalize_zip(value if isinstance(value, str) else "")[:20]
        if settings_mod.is_valid_zip(self.zip) and self.zip != self.settings.zip:
            self.settings.zip = self.zip
            self._save()
        if self.form_error and settings_mod.is_valid_zip(self.zip):
            self.form_error = ""
        self.push()

    def open_link(self, url: str) -> None:
        if not isinstance(url, str) or not is_allowed_link(url, self.settings.effective_base_url(self.host)):
            return
        try:
            opened = bool(self.cad.open_url(url))
        except Exception:  # noqa: BLE001
            opened = False
        if not opened:
            self.toast(f"Couldn't open your browser. The link is {url}", "warn")

    def retry(self) -> None:
        if self.screen != ERROR:
            return
        retry = (self.error or {}).get("retry")
        self.error = None
        self.screen = PARTS
        if retry == "options" or self.options is None:
            self.options = None
            self._ensure_options()
        self.push()

    def dismiss(self) -> None:
        if self.screen == ERROR:
            self.error = None
            self.screen = PARTS
            self.push()

    def edit_parts(self) -> None:
        if self.screen in (QUOTE, ORDERED) and self.job is None:
            self.screen = PARTS
            self.checkout = None
            self.push()

    def back_to_quote(self) -> None:
        """Back to the last quote, from the parts list or the checkout screen. A changed part list shows it stale."""
        if self.screen in (PARTS, ORDERED) and self.quote is not None and self.job is None and self.export_run is None:
            self.screen = QUOTE
            self.push()

    # ------------------------------------------------------------ settings

    def _save(self) -> None:
        try:
            self._save_settings(self.settings)
        except OSError:
            pass  # the choice still holds for this session

    def send_counts(self) -> None:
        """Fire-and-forget the opt-in usage counts on a background thread."""
        if not self.settings.telemetry_opt_in:
            self.counter.take()
            return
        client = self.client_factory(self.settings)
        self.counter.flush(client.send_client_events)

    # -------------------------------------------------------------- options

    def _ensure_options(self) -> None:
        if self.options is not None or self.options_loading:
            return
        self.options_loading = True
        self.counter.record("dialog_opened")
        self.bridge.start(
            lambda job: flow.fetch_options(self.client_factory(self.settings), job),
            on_done=self._options_loaded,
            on_error=self._options_failed,
            name="options",
        )

    def _options_loaded(self, data: Any) -> None:
        self.options_loading = False
        self.options = options_from_dict(data)
        if not self.options.enabled:
            self._show_error("SERVICE_PAUSED", "", retry="options")
        elif is_older(self.host.version, self.options.min_client_version):
            self.screen = ERROR
            self.error = {
                "kind": "update",
                "title": f"Update the {self.host.kind}",
                "message": (
                    f"This {self.host.kind} is version {self.host.version}. Install version {short(self.options.min_client_version)} "
                    "or newer to get quotes."
                ),
                "code": "",
                "retry": "options",
                "link": {"label": "Get the latest version", "url": self.host.help_url},
            }
        for line in self.lines:  # a material the server no longer offers falls back to the first
            material = self.material(line.material)
            line.material = material.id
            if line.finish not in [f.id for f in material.finishes] and material.finishes:
                line.finish = material.finishes[0].id
        self.push()

    def _options_failed(self, code: str, message: str) -> None:
        self.options_loading = False
        if self.screen in (PARTS, CONSENT) and self.job is None:
            self._show_error(code, message, retry="options")
            self.push()

    # ------------------------------------------------------------ selection

    def on_selection_changed(self) -> None:
        """The CAD program's selection changed: selected parts are added (parts screen only)."""
        if self.screen != PARTS or self.export_run is not None:
            return
        if self.add_from_selection(explicit=False, quiet=True):
            self.push()

    def add_from_selection(self, *, explicit: bool, quiet: bool = False) -> int:
        """Add the selected parts; returns how many were added or refreshed."""
        try:
            parts, notices = self.cad.selected_parts(explicit)
        except Exception:  # noqa: BLE001 - no document, or the selection is in flux
            parts, notices = [], []
        if not parts and not notices:
            if explicit and not quiet:
                self.notices = [self.host.text("nothing_selected", f"Nothing is selected. {self.host.select_hint}, then click Add selected.")]
            return 0
        return self.add_parts(list(parts), list(notices))

    def add_parts(self, parts: list[Any], notices: list[str]) -> int:
        """Add ``parts`` and show ``notices`` (the host's refusals; it gives none for a selection the user didn't ask
        to add). The notices replace the ones shown. Returns how many parts were added or changed size, plus the
        notices, so the caller knows whether to redraw."""
        known = {line.key for line in self.lines}
        added = 0
        changed = 1 if self.notices and not notices else 0
        for part in parts:
            if part.key in known:
                # Refresh the size and the part (it may have been edited, or picked through another instance).
                for line in self.lines:
                    if line.key == part.key:
                        if line.part.local_dims_mm != part.local_dims_mm:
                            changed += 1
                        line.part = part
                continue
            if len(self.lines) >= self.max_parts:
                notices.append(f"Quote up to {self.max_parts} parts at a time; {short(part.name)} was not added.")
                continue
            default = self.material(DEFAULT_MATERIAL_ID)
            ids = [f.id for f in default.finishes]
            finish = DEFAULT_FINISH_ID if DEFAULT_FINISH_ID in ids else (ids[0] if ids else DEFAULT_FINISH_ID)
            self.lines.append(Line(part=part, material=default.id, finish=finish))
            known.add(part.key)
            added += 1
        self.notices = notices
        if added:
            self.form_error = ""
        return added + changed + len(notices)

    def _line(self, key: str) -> Optional[Line]:
        for line in self.lines:
            if line.key == key:
                return line
        return None

    # ---------------------------------------------------------------- quote

    def _blocking_problem(self) -> str:
        if not self.lines:
            return self.host.text("add_a_part", f"Add at least one part: {self.host.select_hint[0].lower()}{self.host.select_hint[1:]}.")
        if not settings_mod.is_valid_zip(self.zip):
            return "Enter the US ZIP code the parts ship to (5 digits)."
        too_big = [line.part.name for line in self.lines if not fits_build_volume(line.part.local_dims_mm, self.material(line.material).build_volume_mm)]
        if too_big:
            return "Too large to print in one piece in the chosen material: " + ", ".join(short(n) for n in too_big) + "."
        unanswered = [line.part.name for line in self.lines if line.error_code == "UNITS_CONFIRMATION_REQUIRED" and not line.units]
        if unanswered:
            return "Choose millimeters or inches for: " + ", ".join(short(n) for n in unanswered) + "."
        return ""

    def get_quote(self, zip_code: Optional[str] = None) -> None:
        if self.screen != PARTS or self.job is not None or self.export_run is not None:
            return
        if zip_code:
            self.set_zip(zip_code)
        if self.options is not None and not self.options.enabled:
            self._show_error("SERVICE_PAUSED", "", retry="options")
            self.push()
            return
        problem = self._blocking_problem()
        if problem:
            self.form_error = problem
            self.push()
            return
        self.form_error = ""
        self.notices = []
        self.quote_notice = None
        self.counter.record("quote_requested")
        keys = [line.key for line in self.lines]
        reusable = all(self._cached_part_id(line) for line in self.lines)
        if self.quote is not None and self.quote.is_ready and keys == self.quote_keys and reusable:
            self._start_update()
            return
        to_upload = [line for line in self.lines if not self._cached_part_id(line)]
        if not to_upload:
            self._start_quote_job([], None)
            return
        self.export_run = ExportRun(directory=export.make_temp_dir(), lines=to_upload, zip_code=self.zip)
        self._set_progress("export", f"Exporting 1 of {len(to_upload)}: {short(to_upload[0].part.name)}")
        self.screen = WORKING
        self.push()
        self.cad.defer(self._export_next)

    def _cached_part_id(self, line: Line) -> Optional[str]:
        """The uploaded part to reuse for this line, if its size in the design hasn't changed since."""
        cached = self.part_cache.get(line.key)
        if cached and cached[1] == line.part.local_dims_mm:
            return cached[0]
        return None

    @staticmethod
    def _wire_material(material_id: str) -> Optional[str]:
        """Older servers know only PA12 and may refuse the field, so PA12 is sent as no material."""
        return None if material_id == DEFAULT_MATERIAL_ID else material_id

    def _set_progress(self, phase: str, text: str) -> None:
        order = [p for p, _label in PHASES]
        current = order.index(phase) if phase in order else -1
        self.progress = {
            "phase": phase,
            "text": text,
            "phases": [
                {"id": pid, "label": label, "state": "done" if i < current else ("active" if i == current else "todo")}
                for i, (pid, label) in enumerate(PHASES)
            ],
        }

    def _export_next(self) -> None:
        """Export one part (main thread), then yield so the panel can repaint."""
        run = self.export_run
        if run is None or run.cancelled:
            return
        targets = run.lines if run.fmt == "step" else [run.lines[i] for i in run.failed]
        if run.index >= len(targets):
            self._export_finished()
            return
        line = targets[run.index]
        verb = "Exporting" if run.fmt == "step" else "Exporting as STL"
        self._set_progress("export", f"{verb} {run.index + 1} of {len(targets)}: {short(line.part.name)}")
        self.push()
        fmt = run.fmt if run.fmt == "stl" else getattr(line.part, "fmt", "step")
        try:
            exported: Optional[export.ExportedFile] = export.export_part(
                self.cad.export, line.part, line.part.name, run.directory, run.used, fmt, self.host.program
            )
        except export.ExportError:
            exported = None
        except Exception:  # noqa: BLE001 - a deleted part, a closed document
            exported = None
        if run.fmt == "step":
            run.files.append(exported)
            if exported is None:
                run.failed.append(run.index)
        else:
            if exported is None:
                self._abort_export(f'{self.host.program} could not export "{short(line.part.name)}" as STEP or STL. Nothing was uploaded.')
                return
            run.files[run.failed[run.index]] = exported
        run.index += 1
        self.cad.defer(self._export_next)

    def _export_finished(self) -> None:
        run = self.export_run
        assert run is not None
        if run.fmt == "step" and run.failed:
            self.counter.record("step_export_failed", len(run.failed))
            self.question = {"kind": "stl", "names": [short(run.lines[i].part.name) for i in run.failed]}
            self.progress = None
            self.push()
            return
        limit_mb = self.options.max_file_mb if self.options else DEFAULT_MAX_FILE_MB
        files: list[flow.UploadFile] = []
        for line, item in zip(run.lines, run.files):
            assert item is not None
            if item.size > limit_mb * 1024 * 1024:
                self._abort_export(
                    f'"{short(line.part.name)}" exports to {item.size / 1048576:.1f} MB, over the {limit_mb:g} MB limit. Nothing was uploaded.'
                )
                return
            units = (line.units or "mm") if item.fmt == "stl" else None
            files.append(flow.UploadFile(item.path, item.file_name, line.quantity, line.finish, units, self._wire_material(line.material)))
        if run.fmt == "stl":
            self.counter.record("stl_fallback_used", len(run.failed))
        self.export_run = None
        self._start_quote_job(files, run.directory)

    def stl_choice(self, send: bool) -> None:
        run = self.export_run
        if run is None or self.question is None:
            return
        self.question = None
        if not send:
            self._abort_export("Quote stopped: nothing was uploaded.")
            return
        run.fmt, run.index = "stl", 0
        self._set_progress("export", "Exporting as STL...")
        self.push()
        self.cad.defer(self._export_next)

    def _abort_export(self, text: str) -> None:
        run, self.export_run = self.export_run, None
        if run is not None:
            run.cancelled = True
            export.cleanup(run.directory)
        self.question = None
        self.progress = None
        self.screen = PARTS
        self.form_error = text
        self.push()

    def _start_quote_job(self, files: list[flow.UploadFile], directory: Optional[str]) -> None:
        uploads = iter(files)
        lines: list[flow.Line] = []
        for line in self.lines:
            part_id = self._cached_part_id(line)
            if part_id:
                lines.append(flow.KnownPart(part_id, line.quantity, line.finish, self._wire_material(line.material)))
            else:
                lines.append(next(uploads))
        snapshot = [dataclasses.replace(line) for line in self.lines]
        zip_code = self.zip
        self.job = "quote"
        self.screen = WORKING
        self._set_progress("upload" if files else "analyze", "Uploading..." if files else "Analyzing your parts...")
        self.push()
        self.bridge.start(
            lambda job: flow.run_new_quote(self.client_factory(self.settings), job, lines, zip_code, directory, max_wait=QUOTE_WAIT_SECONDS),
            on_done=lambda data: self._quote_done(data, snapshot, by_order=True),
            on_error=self._job_failed,
            on_progress=self._job_progress,
            name="quote",
        )

    def _start_update(self) -> None:
        assert self.quote is not None and self.quote.quote_id
        items = [
            QuoteItem(quantity=line.quantity, finish=line.finish, part_id=self._cached_part_id(line), material=self._wire_material(line.material))
            for line in self.lines
        ]
        snapshot = [dataclasses.replace(line) for line in self.lines]
        quote_id, zip_code = self.quote.quote_id, self.zip
        self.job = "quote"
        self.screen = WORKING
        self._set_progress("price", "Pricing your parts...")
        self.push()
        self.bridge.start(
            lambda job: flow.run_update_quote(self.client_factory(self.settings), job, quote_id, items, zip_code, max_wait=QUOTE_WAIT_SECONDS),
            on_done=lambda data: self._quote_done(data, snapshot, by_order=False),
            on_error=self._job_failed,
            on_progress=self._job_progress,
            name="update",
        )

    def _job_progress(self, message: str) -> None:
        text = short(message)
        lower = text.lower()
        if lower.startswith(("uploading", "preparing upload")):
            phase = "upload"
        elif lower.startswith("analyzing"):
            phase = "analyze"
        elif lower.startswith("pricing"):
            phase = "price"
        else:
            phase = (self.progress or {}).get("phase", "")
        if self.job == "quote":
            self._set_progress(phase, text)
        else:
            self.progress = {"phase": phase, "text": text, "phases": []}
        self._send_progress()

    def cancel(self) -> None:
        if self.export_run is not None:
            self._abort_export("Quote stopped: nothing was uploaded.")
            return
        if self.job == "quote":
            self.bridge.cancel_all()
            self.options_loading = False
            self.job = None
            self.progress = None
            self.screen = PARTS
            self.form_error = "Quote stopped."
            self.push()

    def _job_failed(self, code: str, message: str) -> None:
        kind, self.job = self.job, None
        self.progress = None
        if code == flow.CANCELLED:
            return
        if kind == "quote":
            self.counter.record("quote_failed")
            if code in FORM_CODES or code == "INVALID_ARGUMENT":
                self.screen = PARTS
                self.form_error = self.describe(code, message)
            else:
                self._show_error(code, message, retry="")
        else:
            # Order or delete failed: stay where we are and say why.
            self.toast(self.describe(code, message), "error")
        self.push()
        self.send_counts()

    def _show_error(self, code: str, message: str, *, retry: str) -> None:
        if code in PAUSED_CODES:
            kind, title = "paused", "Quoting is paused"
        elif code in OFFLINE_CODES:
            kind, title = "offline", "Can't reach makepartsnow.com"
        elif code == "RATE_LIMITED":
            kind, title = "rate_limited", "Too many requests"
        else:
            kind, title = "generic", "Something went wrong"
        self.screen = ERROR
        self.error = {
            "kind": kind,
            "title": title,
            "message": self.describe(code, message),
            "code": code,
            "retry": retry,
            "link": {"label": "Quote on makepartsnow.com", "url": "https://makepartsnow.com"} if kind == "paused" else None,
        }

    def _quote_done(self, data: Any, snapshot: list[Line], *, by_order: bool, notice: Optional[dict[str, str]] = None) -> None:
        """Show a quote answer. ``snapshot`` holds the lines as they were when the quote started.

        A ``quote_parts`` answer lists parts in the order they were sent; a revision is
        matched back to the lines by part id.
        """
        self.job = None
        self.progress = None
        view = quote_from_dict(data)
        if not by_order:
            by_id = {p.part_id: p for p in view.parts}
            wanted = [self.part_cache.get(line.key, ("", ()))[0] for line in snapshot]
            ordered = [by_id.get(pid) for pid in wanted]
            if all(ordered) and len(ordered) == len(view.parts):
                view = dataclasses.replace(view, parts=tuple(ordered))  # type: ignore[arg-type]
        failed = False
        for line, part in zip(snapshot, view.parts):
            live = self._line(line.key)
            if part.part_id and part.delete_token:
                self.uploaded.setdefault(part.part_id, part.delete_token)  # keep the first token
            if part.error is not None or part.status in ("failed", "needs_input") or not part.part_id:
                failed = True
                if part.error is None or part.error.code in REUPLOAD_CODES or not part.part_id:
                    self.part_cache.pop(line.key, None)  # upload it again after a fix
                if live is not None:
                    code = part.error.code if part.error else ""
                    live.error = describe_part_error(code, part.error.message if part.error else "", self.host) if part.error else "This part needs attention."
                    live.error_code = code
                    live.units_choices = tuple(v for v in (part.error.allowed_values if part.error else ()) if v in UNITS) or (UNITS if code == "UNITS_CONFIRMATION_REQUIRED" else ())
            else:
                self.part_cache[line.key] = (part.part_id, line.part.local_dims_mm)
                if live is not None and live.error_code != "UNITS_CONFIRMATION_REQUIRED":
                    live.error, live.error_code, live.units_choices = "", "", ()
        units = [
            best_match(line.part.dims_candidates, part.dimensions_mm) if part.dimensions_mm else None
            for line, part in zip(snapshot, view.parts)
        ]
        if any(u is not None and not u.ok for u in units):
            self.counter.record("units_mismatch")
        if (view.is_ready and not failed and len(view.parts) == len(snapshot)) or view.status == "manual_review":
            self.counter.record("quote_shown")
            self.quote = view
            self.quote_keys = [line.key for line in snapshot]
            self.quote_lines = snapshot
            self.units = units
            self.quote_notice = notice
            if view.status == "manual_review":
                self.quote_notice = {"kind": "warn", "text": "A person will review this quote before it can be ordered. We'll email you."}
            elif view.quote_status == "ordered":
                self.quote_notice = {"kind": "warn", "text": "This quote has already been ordered. Edit parts and get a new quote."}
            elif view.quote_status == "expired":
                self.quote_notice = {"kind": "warn", "text": "This quote has expired. Edit parts and get a new quote for current prices."}
            self.screen = QUOTE
        else:
            self.counter.record("quote_failed")
            self.screen = PARTS
            if view.error is not None:
                self.form_error = self.describe(view.error.code, view.error.message)
            else:
                self.form_error = self.describe("NEEDS_INPUT")
        self.push()
        self.send_counts()

    # ------------------------------------------------------------ the quote

    @property
    def is_stale(self) -> bool:
        """The part list, a quantity, material or finish changed since the shown quote."""
        if self.quote is None:
            return False
        return [line.choice() for line in self.lines] != [line.choice() for line in self.quote_lines]

    @property
    def order_blocked(self) -> str:
        """Why Order is disabled, or "" when it can be clicked."""
        quote = self.quote
        program = self.host.program
        if quote is None or not quote.is_ready:
            return "The quote isn't ready to order yet."
        if quote.quote_status not in (None, "open"):
            return "Get a new quote first."
        if self.is_stale:
            return f"The parts changed since this quote. Edit parts, then click {self.host.quote_action} to refresh it."
        if len(self.units) != len(quote.parts) or any(u is None or not u.ok for u in self.units):
            return f"Ordering is blocked: a part's quoted size doesn't match its size in {program}. Check its units, then quote again."
        if any(not fits_build_volume(p.dimensions_mm or line.part.local_dims_mm, self.material(line.material).build_volume_mm) for p, line in zip(quote.parts, self.quote_lines)):
            return "A part is larger than its material's build volume."
        if self.options is not None and not self.options.enabled:
            return f"Quoting from {program} is paused."
        if self.job is not None:
            return "Wait for the current step to finish."
        return ""

    def _quote_view(self) -> Optional[dict[str, Any]]:
        quote = self.quote
        if quote is None:
            return None
        cards = []
        for index, part in enumerate(quote.parts):
            line = self.quote_lines[index] if index < len(self.quote_lines) else None
            material = self.material(part.material or (line.material if line else DEFAULT_MATERIAL_ID))
            cards.append(
                viewmodel.quote_card(
                    line.key if line else part.part_id,
                    line.part.name if line else part.file_name,
                    part,
                    self.units[index] if index < len(self.units) else None,
                    material,
                    line.part.local_dims_mm if line else None,
                    self.host,
                )
            )
        blocked = self.order_blocked
        notice = self.quote_notice
        if self.is_stale and notice is None:
            notice = {"kind": "warn", "text": "The parts changed since this quote, so these prices are out of date."}
        return {
            "id": quote.quote_id or "",
            "url": quote.quote_url if quote.quote_id else "",
            "notice": notice,
            "stale": self.is_stale,
            "cards": cards,
            "summary": viewmodel.summary(quote),
            "can_order": not blocked,
            "order_blocked": blocked,
            "can_save": self.can_save and (not blocked or blocked.startswith("Wait")),
        }

    # ---------------------------------------------------------------- order

    def order(self) -> None:
        if self.screen not in (QUOTE, ORDERED) or self.quote is None or self.job is not None:
            return
        blocked = self.order_blocked
        if blocked:
            self.toast(blocked, "error")
            return
        quote_id = self.quote.quote_id
        self.counter.record("order_clicked")
        self.job = "checkout"
        self.push()
        self.bridge.start(
            lambda job: flow.run_checkout(self.client_factory(self.settings), job, quote_id, self.cad.open_url),
            on_done=self._checkout_done,
            on_error=self._job_failed,
            on_progress=lambda _m: None,
            name="checkout",
        )

    def _checkout_done(self, data: Any) -> None:
        self.job = None
        checkout = checkout_from_dict(data["checkout"])
        latest = data.get("latest_quote")
        if latest:
            # QUOTE_REVISED: show the newest revision's prices; the user clicks Order again.
            self._quote_done(
                latest,
                list(self.quote_lines),
                by_order=False,
                notice={"kind": "info", "text": "This quote was revised. These are the newest prices; click Order again to check out."},
            )
            return
        view = viewmodel.checkout_view(checkout, bool(data.get("opened")), self.host)
        if view["ready"]:
            self.checkout = view
            self.screen = ORDERED
        else:
            self.quote_notice = {"kind": "warn", "text": view["reason"] + (" " + view["error"] if view["error"] else "")}
        self.push()
        self.send_counts()

    # ----------------------------------------------------------------- save

    def save_to_design(self) -> tuple[int, list[str]]:
        """Write the quote into each quoted part through ``cad.save_quote`` (hosts that have it)."""
        quote = self.quote
        if not self.can_save:
            return 0, []
        if quote is None or (self.order_blocked and not self.order_blocked.startswith("Wait")):
            self.toast("Nothing was saved: " + (self.order_blocked if quote is not None else "there is no current quote."), "error")
            return 0, []
        saved, failed = 0, []
        for index, line in enumerate(self.quote_lines):
            part = quote.parts[index]
            material = self.material(part.material or line.material)
            payload = memory.build_payload(
                quote_id=quote.quote_id or "",
                quote_url=quote.quote_url,
                price_valid_until=quote.price_valid_until,
                part_id=part.part_id,
                file_name=part.file_name,
                quantity=part.quantity,
                finish=viewmodel.finish_of(material, part.finish)[0],
                unit_price=part.unit_price,
                line_total=part.line_total,
                material=part.material_name or material.name,
            )
            try:
                if self.cad.save_quote(line.part, payload):
                    saved += 1
                else:
                    failed.append(line.part.name)
            except Exception:  # noqa: BLE001 - e.g. a read-only referenced component
                failed.append(line.part.name)
        if saved:
            self.counter.record("quote_saved_to_part", saved)
            text = SAVED_TEXT.format(count=saved, s="" if saved == 1 else "s")
        else:
            text = "The quote was not saved."
        if failed:
            text += " Couldn't save to " + ", ".join(short(n) for n in failed) + " " + self.host.text("save_failed_hint", "(a linked or read-only part can't be changed here).")
        self.toast(text, "ok" if saved and not failed else "warn")
        self.send_counts()
        return saved, failed

    # --------------------------------------------------------------- delete

    def delete_files(self) -> None:
        if not self.uploaded or self.job is not None:
            return
        parts = list(self.uploaded.items())
        self.job = "delete"
        self.push()
        self.bridge.start(
            lambda job: flow.run_delete(self.client_factory(self.settings), job, parts),
            on_done=self._delete_done,
            on_error=self._job_failed,
            on_progress=lambda _m: None,
            name="delete",
        )

    def _delete_done(self, data: Any) -> None:
        self.job = None
        result = delete_from_dict(data)
        for part_id in result.deleted:
            self.uploaded.pop(part_id, None)
        deleted = set(result.deleted)
        self.part_cache = {k: v for k, v in self.part_cache.items() if v[0] not in deleted}
        if result.deleted:
            self.counter.record("files_deleted", len(result.deleted))
            self.quote = None
            self.quote_keys, self.quote_lines, self.units = [], [], []
            self.checkout = None
            if self.screen in (QUOTE, ORDERED):
                self.screen = PARTS
        count = len(result.deleted)
        text = f"Deleted {count} uploaded file{'' if count == 1 else 's'} from Make Parts Now."
        problems = [self.describe(e.code, e.message) for e in result.errors]
        if problems:
            text += " " + " ".join(dict.fromkeys(problems))
        self.toast(text, "warn" if problems else "ok")
        self.push()
        self.send_counts()

    # ---------------------------------------------------------------- close

    def shutdown(self) -> None:
        """The panel or program is closing: stop jobs, drop exports, send pending counts."""
        if self.export_run is not None:
            self.export_run.cancelled = True
            export.cleanup(self.export_run.directory)
            self.export_run = None
        self.bridge.cancel_all()
        self.send_counts()
