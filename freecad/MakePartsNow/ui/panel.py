"""The Make Parts Now task panel, in Qt widgets through FreeCAD's PySide wrapper.

FreeCAD's Windows build has no QtWebEngine, so the Fusion palette's HTML can't
be reused; this panel draws the same screens from the core controller's view
model (``Controller.state()``): consent, parts, working, quote, ordered and
error. Every user action calls a controller method, and the controller pushes a
new state back through ``render``. All text from the server goes into plain
labels (``Qt.PlainText``), never rich text.

Opening the panel fetches only the options (materials, limits, promotion code).
Nothing is exported or uploaded until the person accepted the consent text and
clicked Get instant quote.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

import FreeCAD as App
import FreeCADGui as Gui
from PySide import QtCore, QtGui, QtWidgets

from .. import constants
from ..core import controller as ctl
from ..core import settings, worker
from .adapter import FreeCadAdapter, Relay, SelectionObserver

BRAND = "#cb1217"
BRAND_DARK = "#a50e12"
PLAIN = QtCore.Qt.PlainText

_panel: Optional["QuotePanel"] = None


def show() -> None:
    """Open the task panel, or bring it back if it is already open."""
    global _panel
    if _panel is not None and Gui.Control.activeDialog():
        return
    if Gui.Control.activeDialog():
        QtWidgets.QMessageBox.information(
            Gui.getMainWindow(), constants.PANEL_TITLE, "Close the open task (the Tasks panel) first, then quote again."
        )
        return
    _panel = QuotePanel()
    Gui.Control.showDialog(_panel)
    _panel.start()


def _is_dark(widget: QtWidgets.QWidget) -> bool:
    """Whether FreeCAD shows a dark theme. FreeCAD 1.1's themes set the main window's palette, not the application's
    (Windows, FreeCAD Dark: main window #191919, application #f0f0f0), and the task panel is built before it is
    parented, so its own palette is the application's. Ask the main window, falling back to the widget."""
    palette = widget.palette()
    try:
        main = Gui.getMainWindow()
        if main is not None:
            palette = main.palette()
    except Exception:  # noqa: BLE001  no GUI (tests) or not ready yet
        pass
    return palette.color(QtGui.QPalette.Window).lightness() < 128


def _label(text: str = "", *, wrap: bool = True, bold: bool = False, small: bool = False, color: str = "") -> QtWidgets.QLabel:
    label = QtWidgets.QLabel(text)
    label.setTextFormat(PLAIN)
    label.setWordWrap(wrap)
    style = []
    if bold:
        style.append("font-weight: 600")
    if small:
        style.append("font-size: 90%")
    if color:
        style.append(f"color: {color}")
    if style:
        label.setStyleSheet("; ".join(style))
    return label


def _link_button(text: str, on_click: Callable[[], None]) -> QtWidgets.QPushButton:
    button = QtWidgets.QPushButton(text)
    button.setFlat(True)
    button.setCursor(QtGui.QCursor(QtCore.Qt.PointingHandCursor))
    button.setStyleSheet("QPushButton { text-align: left; border: none; padding: 0; color: palette(link); text-decoration: underline; }")
    button.clicked.connect(on_click)
    return button


def _primary(text: str, on_click: Callable[[], None]) -> QtWidgets.QPushButton:
    button = QtWidgets.QPushButton(text)
    button.setMinimumHeight(30)
    button.setStyleSheet(
        f"QPushButton {{ background: {BRAND}; color: white; font-weight: 600; border: none; border-radius: 4px; padding: 6px 12px; }}"
        f"QPushButton:hover {{ background: {BRAND_DARK}; }}"
        "QPushButton:disabled { background: #9a9a9a; color: #eeeeee; }"
    )
    button.clicked.connect(on_click)
    return button


def _swatch(hex_color: str) -> QtGui.QIcon:
    pixmap = QtGui.QPixmap(14, 14)
    pixmap.fill(QtGui.QColor(hex_color if QtGui.QColor.isValidColor(hex_color) else "#cccccc"))
    return QtGui.QIcon(pixmap)


def _clear(layout: QtWidgets.QLayout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widget.deleteLater()
        elif item.layout() is not None:
            _clear(item.layout())


class _WheelOnlyWhenFocused:
    """A spin box or combo box that ignores the mouse wheel until it has focus.

    Qt's default lets the wheel change a hovered box, so scrolling the panel over a part's material or quantity
    changed it without a click (found on Windows, 17 Oct 2026). Ignored, the wheel scrolls the panel instead.
    """

    def _setup_wheel(self) -> None:
        self.setFocusPolicy(QtCore.Qt.StrongFocus)

    def wheelEvent(self, event) -> None:  # noqa: N802 - Qt's name
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()


class SpinBox(_WheelOnlyWhenFocused, QtWidgets.QSpinBox):
    def __init__(self) -> None:
        super().__init__()
        self._setup_wheel()


class ComboBox(_WheelOnlyWhenFocused, QtWidgets.QComboBox):
    def __init__(self) -> None:
        super().__init__()
        self._setup_wheel()


class PartCard(QtWidgets.QFrame):
    """One part on the parts screen: name and size, quantity, material, finish, and its problem if any."""

    def __init__(self, panel: "QuotePanel", key: str) -> None:
        super().__init__()
        self.panel, self.key = panel, key
        self.setFrameShape(QtWidgets.QFrame.StyledPanel)
        grid = QtWidgets.QGridLayout(self)
        grid.setContentsMargins(6, 6, 6, 6)
        self.name = _label(bold=True)
        self.dims = _label(small=True)
        remove = QtWidgets.QToolButton()
        remove.setText("✕")
        remove.setToolTip("Remove this part from the quote")
        remove.clicked.connect(lambda: panel.c.remove_part(key))
        self.quantity = SpinBox()
        self.quantity.setKeyboardTracking(False)  # commit on Enter, focus out or the arrows
        self.quantity.setRange(1, 10000)
        self.quantity.setPrefix("Qty ")
        self.quantity.setAccessibleName("Quantity")
        self.quantity.valueChanged.connect(lambda v: panel.c.set_part(key, quantity=int(v)))
        self.material = ComboBox()
        self.material.setAccessibleName("Material")
        self.material.activated.connect(self._material_chosen)
        self.finish = ComboBox()
        self.finish.setAccessibleName("Finish or color")
        self.finish.activated.connect(self._finish_chosen)
        self.units = ComboBox()
        self.units.addItem("Millimeters", "mm")
        self.units.addItem("Inches", "in")
        self.units.activated.connect(lambda _i: panel.c.set_part(key, units=self.units.currentData()))
        self.error = _label(small=True)
        grid.addWidget(self.name, 0, 0, 1, 2)
        grid.addWidget(remove, 0, 2, QtCore.Qt.AlignRight)
        grid.addWidget(self.dims, 1, 0, 1, 3)
        grid.addWidget(self.quantity, 2, 0)
        grid.addWidget(self.material, 2, 1, 1, 2)
        grid.addWidget(self.finish, 3, 1, 1, 2)
        grid.addWidget(self.units, 3, 0)
        grid.addWidget(self.error, 4, 0, 1, 3)
        grid.setColumnStretch(1, 1)
        self._materials_sig: tuple = ()

    def _material_chosen(self, index: int) -> None:
        material_id = self.material.itemData(index)
        if material_id:
            self.panel.c.set_part(self.key, material=material_id)

    def _finish_chosen(self, index: int) -> None:
        finish_id = self.finish.itemData(index)
        if finish_id:
            self.panel.c.set_part(self.key, finish=finish_id)

    def update_from(self, line: dict, state: dict, colors: dict) -> None:
        materials = {m["id"]: m for m in state["materials"]}
        signature = tuple((m["id"], m["short"]) for m in state["materials"])
        for widget in (self.quantity, self.material, self.finish, self.units):
            widget.blockSignals(True)
        try:
            self.name.setText(line["name"])
            self.dims.setText(line["dims"])
            self.quantity.setMaximum(int(state["max_quantity"]))
            self.quantity.setValue(int(line["quantity"]))
            if signature != self._materials_sig:
                self.material.clear()
                for group in state["groups"]:
                    self.material.addItem(group["label"])
                    head = self.material.model().item(self.material.count() - 1)
                    if head is not None:
                        head.setEnabled(False)
                    for material_id in group["materials"]:
                        m = materials[material_id]
                        self.material.addItem(f"   {m['short']} ({m['process_label']})", material_id)
                        self.material.setItemData(self.material.count() - 1, m["description"], QtCore.Qt.ToolTipRole)
                self._materials_sig = signature
            self.material.setCurrentIndex(max(0, self.material.findData(line["material"])))
            self.finish.clear()
            for finish in materials.get(line["material"], {}).get("finishes", []):
                self.finish.addItem(_swatch(finish["hex"]), finish["name"], finish["id"])
            self.finish.setCurrentIndex(max(0, self.finish.findData(line["finish"])))
            choices = line.get("units_choices") or []
            self.units.setVisible(bool(choices))
            if choices:
                self.units.setCurrentIndex(max(0, self.units.findData(line.get("units") or "mm")))
            self.error.setText(line["error"])
            self.error.setStyleSheet(f"color: {colors['fail']}; font-size: 90%")
            self.error.setVisible(bool(line["error"]))
        finally:
            for widget in (self.quantity, self.material, self.finish, self.units):
                widget.blockSignals(False)


class QuotePanel:
    """FreeCAD calls ``getStandardButtons``, ``reject`` and ``accept``; ``form`` is the widget it shows."""

    def __init__(self) -> None:
        self.form = QtWidgets.QWidget()
        self.form.setWindowTitle(constants.PANEL_TITLE)
        self.form.setWindowIcon(QtGui.QIcon(constants_icon()))
        self.form.setMinimumWidth(320)
        self.relay = Relay()
        self.bridge = worker.Bridge("MakePartsNow", self.relay.fire)
        self.relay.event.connect(lambda _eid, info: self.bridge.dispatch(info))
        self.adapter = FreeCadAdapter(self)
        data_dir = settings.settings_dir(App.getUserAppDataDir())
        host = constants.HOST
        self.c = ctl.Controller(
            host,
            self.adapter,
            self.bridge,
            load_settings=lambda: settings.load(host, data_dir),
            save_settings=lambda s: settings.save(s, host, data_dir),
        )
        self.observer = SelectionObserver(self.c.on_selection_changed)
        self.cards: dict[str, PartCard] = {}
        self.state: dict[str, Any] = {}
        self._build()

    # ------------------------------------------------------------ FreeCAD's task dialog

    def getStandardButtons(self):  # noqa: N802 - FreeCAD's name
        return QtWidgets.QDialogButtonBox.Close

    def reject(self) -> bool:
        self.close()
        return True

    def accept(self) -> bool:
        self.close()
        return True

    def needsFullSpace(self) -> bool:  # noqa: N802
        return True

    def start(self) -> None:
        Gui.Selection.addObserver(self.observer)
        self.c.ready()
        # The command runs only with a selection: add it now (after consent; before, accepting adds it).
        self.c.on_selection_changed()

    def close(self) -> None:
        global _panel
        try:
            Gui.Selection.removeObserver(self.observer)
        except Exception:  # noqa: BLE001
            pass
        self.c.shutdown()
        Gui.Control.closeDialog()
        _panel = None

    # ------------------------------------------------------------ layout

    @property
    def colors(self) -> dict[str, str]:
        if _is_dark(self.form):
            return {"ok": "#6fcf82", "warn": "#f0b45a", "fail": "#ff7b7b", "muted": "#aab2ba", "info": "#8cc4ff"}
        return {"ok": "#267337", "warn": "#8a5a14", "fail": "#b3141a", "muted": "#5c666f", "info": "#1a65b0"}

    def _build(self) -> None:
        outer = QtWidgets.QVBoxLayout(self.form)
        outer.setContentsMargins(6, 6, 6, 6)
        head = QtWidgets.QHBoxLayout()
        logo = QtWidgets.QLabel()
        logo.setPixmap(QtGui.QIcon(constants_icon()).pixmap(28, 28))
        head.addWidget(logo)
        titles = QtWidgets.QVBoxLayout()
        titles.addWidget(_label("Make Parts Now", bold=True))
        self.subline = _label(small=True)
        titles.addWidget(self.subline)
        head.addLayout(titles, 1)
        self.test_badge = _label("TEST", bold=True, color="white")
        self.test_badge.setStyleSheet("background: #8a5a14; color: white; font-weight: 600; padding: 2px 6px; border-radius: 3px")
        head.addWidget(self.test_badge, 0, QtCore.Qt.AlignTop)
        outer.addLayout(head)
        self.stack = QtWidgets.QStackedWidget()
        outer.addWidget(self.stack, 1)
        self.toast = _label(small=True)
        self.toast.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        self.toast.hide()
        outer.addWidget(self.toast)
        self.pages: dict[str, QtWidgets.QWidget] = {}
        for name, build in (
            (ctl.CONSENT, self._build_consent),
            (ctl.PARTS, self._build_parts),
            (ctl.WORKING, self._build_working),
            (ctl.QUOTE, self._build_quote),
            (ctl.ORDERED, self._build_ordered),
            (ctl.ERROR, self._build_error),
        ):
            page = QtWidgets.QWidget()
            build(page)
            self.pages[name] = page
            self.stack.addWidget(page)

    def _scroll(self, page: QtWidgets.QWidget) -> QtWidgets.QVBoxLayout:
        """A page whose content scrolls; returns the content's layout."""
        layout = QtWidgets.QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        area = QtWidgets.QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QtWidgets.QFrame.NoFrame)
        inner = QtWidgets.QWidget()
        area.setWidget(inner)
        layout.addWidget(area)
        content = QtWidgets.QVBoxLayout(inner)
        content.setContentsMargins(2, 2, 2, 2)
        return content

    def _build_consent(self, page: QtWidgets.QWidget) -> None:
        self.consent_box = self._scroll(page)

    def _build_parts(self, page: QtWidgets.QWidget) -> None:
        box = self._scroll(page)
        buttons = QtWidgets.QHBoxLayout()
        add = QtWidgets.QPushButton("Add selected")
        add.setToolTip("Add the bodies selected in the 3D view or the tree")
        add.clicked.connect(self.c.add_selected)
        clear = QtWidgets.QPushButton("Clear")
        clear.clicked.connect(self.c.clear_parts)
        buttons.addWidget(add)
        buttons.addWidget(clear)
        buttons.addStretch(1)
        box.addLayout(buttons)
        self.empty = _label("Select bodies in the 3D view or the tree; they are added here. Each can have its own quantity, material and finish.")
        box.addWidget(self.empty)
        self.notices = _label(small=True)
        box.addWidget(self.notices)
        self.cards_box = QtWidgets.QVBoxLayout()
        box.addLayout(self.cards_box)
        zip_row = QtWidgets.QHBoxLayout()
        zip_row.addWidget(_label("Ship to ZIP", wrap=False))
        self.zip = QtWidgets.QLineEdit()
        self.zip.setMaxLength(10)
        self.zip.setPlaceholderText("94107")
        self.zip.setAccessibleName("Ship to ZIP code")
        self.zip.editingFinished.connect(lambda: self.c.set_zip(self.zip.text()))
        self.zip.returnPressed.connect(self._get_quote)
        zip_row.addWidget(self.zip, 1)
        box.addLayout(zip_row)
        self.form_error = _label()
        box.addWidget(self.form_error)
        self.promo_parts = _label(small=True)
        self.promo_parts.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        box.addWidget(self.promo_parts)
        self.quote_button = _primary("Get instant quote", self._get_quote)
        box.addWidget(self.quote_button)
        self.options_note = _label("Loading materials...", small=True)
        box.addWidget(self.options_note)
        self.back_to_quote = _link_button("Back to the last quote", self.c.back_to_quote)
        box.addWidget(self.back_to_quote)
        box.addStretch(1)
        self.footer_parts = QtWidgets.QVBoxLayout()
        box.addLayout(self.footer_parts)

    def _get_quote(self) -> None:
        self.c.get_quote(self.zip.text())

    def _build_working(self, page: QtWidgets.QWidget) -> None:
        box = QtWidgets.QVBoxLayout(page)
        self.phases = _label(small=True)
        box.addWidget(self.phases)
        self.progress_text = _label()
        box.addWidget(self.progress_text)
        self.busy = QtWidgets.QProgressBar()
        self.busy.setRange(0, 0)
        box.addWidget(self.busy)
        self.question = QtWidgets.QWidget()
        q = QtWidgets.QVBoxLayout(self.question)
        q.setContentsMargins(0, 0, 0, 0)
        self.question_text = _label()
        q.addWidget(self.question_text)
        row = QtWidgets.QHBoxLayout()
        send = QtWidgets.QPushButton("Send as STL")
        send.clicked.connect(lambda: self.c.stl_choice(True))
        stop = QtWidgets.QPushButton("Stop")
        stop.clicked.connect(lambda: self.c.stl_choice(False))
        row.addWidget(send)
        row.addWidget(stop)
        q.addLayout(row)
        box.addWidget(self.question)
        self.cancel_button = QtWidgets.QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.c.cancel)
        box.addWidget(self.cancel_button)
        box.addStretch(1)

    def _build_quote(self, page: QtWidgets.QWidget) -> None:
        self.quote_box = self._scroll(page)

    def _build_ordered(self, page: QtWidgets.QWidget) -> None:
        self.ordered_box = self._scroll(page)

    def _build_error(self, page: QtWidgets.QWidget) -> None:
        self.error_box = self._scroll(page)

    # ------------------------------------------------------------ drawing

    def render(self, state: dict) -> None:
        self.state = state
        screen = state["screen"]
        self.subline.setText(state["subline"])
        self.test_badge.setVisible(bool(state.get("test")))
        if state.get("test"):
            self.test_badge.setToolTip(f"Talking to {state['test']['base_url']}, not makepartsnow.com")
        if screen == ctl.CONSENT:
            self._render_consent(state)
        elif screen == ctl.PARTS:
            self._render_parts(state)
        elif screen == ctl.WORKING:
            self._render_working(state)
        elif screen == ctl.QUOTE:
            self._render_quote(state)
        elif screen == ctl.ORDERED:
            self._render_ordered(state)
        elif screen == ctl.ERROR:
            self._render_error(state)
        self.stack.setCurrentWidget(self.pages[screen])

    def show_progress(self, progress: Any) -> None:
        if progress:
            self.progress_text.setText(progress.get("text", ""))

    def show_toast(self, text: str, kind: str) -> None:
        color = {"error": "fail", "warn": "warn", "ok": "ok"}.get(kind, "info")
        self.toast.setText(text)
        self.toast.setStyleSheet(f"color: {self.colors[color]}; font-size: 90%")
        self.toast.show()

    def _footer(self, box: QtWidgets.QVBoxLayout, state: dict) -> None:
        _clear(box)
        if state["uploaded_count"]:
            count = state["uploaded_count"]
            delete = QtWidgets.QPushButton(f"Delete my uploaded files ({count})")
            delete.setEnabled(bool(state["can_delete"]))
            delete.clicked.connect(self.c.delete_files)
            box.addWidget(delete)
        links = QtWidgets.QHBoxLayout()
        for label, key in (("Privacy", "privacy"), ("Terms", "terms"), ("Help", "help")):
            url = state["links"][key]
            links.addWidget(_link_button(label, lambda u=url: self.c.open_link(u)))
        links.addStretch(1)
        version = _label(f"v{state['version']}", wrap=False, small=True, color=self.colors["muted"])
        links.addWidget(version)
        box.addLayout(links)
        box.addWidget(_label(state["not_affiliated"], small=True, color=self.colors["muted"]))

    def _render_consent(self, state: dict) -> None:
        box = self.consent_box
        _clear(box)
        view = state["consent"]
        box.addWidget(_label(view["title"], bold=True))
        box.addWidget(_label(view["intro"]))
        for section in view["sections"]:
            box.addWidget(_label(section["heading"], bold=True))
            for item in section["items"]:
                box.addWidget(_label("•  " + item))
        links = QtWidgets.QHBoxLayout()
        for link in view["links"]:
            links.addWidget(_link_button(link["label"], lambda u=link["url"]: self.c.open_link(u)))
        links.addStretch(1)
        box.addLayout(links)
        telemetry = QtWidgets.QCheckBox(view["telemetry_label"])
        telemetry.setChecked(False)
        box.addWidget(telemetry)
        box.addWidget(_label(view["telemetry_hint"], small=True, color=self.colors["muted"]))
        box.addWidget(_primary("Accept and continue", lambda: self.c.accept_consent(telemetry.isChecked())))
        box.addWidget(_label(view["not_affiliated"], small=True, color=self.colors["muted"]))
        box.addStretch(1)

    def _render_parts(self, state: dict) -> None:
        parts = state["parts"]
        keys = [p["key"] for p in parts]
        for key in list(self.cards):
            if key not in keys:
                self.cards.pop(key).deleteLater()
        for index, line in enumerate(parts):
            card = self.cards.get(line["key"])
            if card is None:
                card = PartCard(self, line["key"])
                self.cards[line["key"]] = card
            self.cards_box.insertWidget(index, card)
            card.update_from(line, state, self.colors)
        self.empty.setVisible(not parts)
        self.notices.setText("\n".join(state["notices"]))
        self.notices.setStyleSheet(f"color: {self.colors['warn']}; font-size: 90%")
        self.notices.setVisible(bool(state["notices"]))
        if not self.zip.hasFocus():
            self.zip.setText(state["zip"])
        self.form_error.setText(state["form_error"])
        self.form_error.setStyleSheet(f"color: {self.colors['fail']}")
        self.form_error.setVisible(bool(state["form_error"]))
        promo = state.get("promo")
        self.promo_parts.setText(f"{promo['text']} {promo['code']}" if promo else "")
        self.promo_parts.setVisible(bool(promo))
        self.quote_button.setEnabled(bool(state["can_quote"]))
        self.options_note.setVisible(bool(state["options_loading"]))
        self.back_to_quote.setVisible(bool(state.get("has_quote")))
        self._footer(self.footer_parts, state)

    def _render_working(self, state: dict) -> None:
        progress = state.get("progress") or {}
        self.phases.setText("  ›  ".join(
            ("● " if p["state"] == "active" else ("✓ " if p["state"] == "done" else "○ ")) + p["label"] for p in progress.get("phases", [])
        ))
        self.progress_text.setText(progress.get("text", ""))
        question = state.get("question")
        self.question.setVisible(bool(question))
        self.busy.setVisible(not question)
        self.cancel_button.setVisible(not question)
        if question:
            names = ", ".join(question["names"])
            self.question_text.setText(
                f"FreeCAD could not export {names} as STEP. Send an STL mesh instead? STL is less exact; "
                "check the part's size on the quote."
            )

    def _render_quote(self, state: dict) -> None:
        box = self.quote_box
        _clear(box)
        quote = state["quote"]
        colors = self.colors
        notice = quote.get("notice")
        if notice:
            box.addWidget(_label(notice["text"], color=colors["warn" if notice["kind"] == "warn" else "info"]))
        for card in quote["cards"]:
            frame = QtWidgets.QFrame()
            frame.setFrameShape(QtWidgets.QFrame.StyledPanel)
            inner = QtWidgets.QVBoxLayout(frame)
            inner.setContentsMargins(6, 6, 6, 6)
            top = QtWidgets.QHBoxLayout()
            top.addWidget(_label(card["name"], bold=True), 1)
            top.addWidget(_label(card["line_total"], wrap=False, bold=True))
            inner.addLayout(top)
            inner.addWidget(_label(f"{card['dims']}", small=True, color=colors["muted"]))
            finish = QtWidgets.QHBoxLayout()
            swatch = QtWidgets.QLabel()
            swatch.setPixmap(_swatch(card["finish_hex"]).pixmap(12, 12))
            finish.addWidget(swatch)
            finish.addWidget(_label(f"{card['material']} ({card['process_label']}), {card['finish_name']}  ·  {card['quantity']} × {card['unit_price']}", small=True), 1)
            inner.addLayout(finish)
            for check in card["checks"]:
                mark = {"ok": "✓", "warn": "!", "fail": "✕"}[check["state"]]
                text = f"{mark}  {check['text']}" + (f": {check['detail']}" if check["detail"] else "")
                inner.addWidget(_label(text, small=True, color=colors["ok" if check["state"] == "ok" else check["state"]]))
            box.addWidget(frame)
        summary = quote.get("summary")
        if summary:
            grid = QtWidgets.QGridLayout()
            rows = [(summary["parts_label"], summary["parts"])]
            if summary.get("discount"):
                rows.append(("Discount", summary["discount"]))
            if summary.get("min_order"):
                rows.append(("Minimum order", summary["min_order"]))
            rows.append(("Shipping", summary["shipping"]))
            for i, (name, value) in enumerate(rows):
                grid.addWidget(_label(name), i, 0)
                grid.addWidget(_label(value, wrap=False), i, 1, QtCore.Qt.AlignRight)
            grid.addWidget(_label("Total", bold=True), len(rows), 0)
            grid.addWidget(_label(summary["total"], wrap=False, bold=True), len(rows), 1, QtCore.Qt.AlignRight)
            box.addLayout(grid)
            dates = [f"Ships by {summary['ships_by']}" if summary["ships_by"] else "", f"arrives by {summary['arrives_by']}" if summary["arrives_by"] else ""]
            box.addWidget(_label(", ".join(d for d in dates if d), small=True))
            box.addWidget(_label(f"{summary['tax_note']}. Prices valid until {summary['valid_until']}.", small=True, color=colors["muted"]))
        promo = state.get("promo")
        if promo:
            code = _label(f"{promo['text']} {promo['code']}", small=True)
            code.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
            box.addWidget(code)
        order = _primary("Order", self.c.order)
        order.setEnabled(bool(quote["can_order"]) and self.c.job is None)
        order.setToolTip(quote["order_blocked"] or "Opens checkout on makepartsnow.com in your browser")
        box.addWidget(order)
        if quote["order_blocked"]:
            box.addWidget(_label(quote["order_blocked"], small=True, color=colors["fail"]))
        row = QtWidgets.QHBoxLayout()
        edit = QtWidgets.QPushButton("Edit parts")
        edit.clicked.connect(self.c.edit_parts)
        row.addWidget(edit)
        if quote.get("url"):
            row.addWidget(_link_button("Open the quote on makepartsnow.com", lambda u=quote["url"]: self.c.open_link(u)))
        row.addStretch(1)
        box.addLayout(row)
        box.addStretch(1)
        footer = QtWidgets.QVBoxLayout()
        box.addLayout(footer)
        self._footer(footer, state)

    def _render_ordered(self, state: dict) -> None:
        box = self.ordered_box
        _clear(box)
        checkout = state["checkout"] or {}
        box.addWidget(_label("Checkout opened" if checkout.get("opened") else "Checkout is ready", bold=True))
        box.addWidget(_label(
            "Finish your order on makepartsnow.com in your browser. Make Parts Now emails you when the order is placed; "
            "this panel can't tell whether you paid."
            if checkout.get("opened")
            else "Your browser didn't open. Copy the link below into it to finish your order."
        ))
        if checkout.get("total"):
            box.addWidget(_label(f"Total {checkout['total']} before tax", bold=True))
        # The /pay link is one long token with no spaces: a word-wrapped label can't break it and would widen the
        # whole panel past the task pane (seen on Windows, 17 Oct 2026). A read-only line scrolls inside its own box.
        link_row = QtWidgets.QHBoxLayout()
        url = QtWidgets.QLineEdit(checkout.get("url", ""))
        url.setReadOnly(True)
        url.setCursorPosition(0)
        url.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Fixed)
        url.setToolTip("The checkout link for this quote")
        link_row.addWidget(url, 1)
        copy = QtWidgets.QPushButton("Copy link")
        copy.clicked.connect(lambda: self._copy_link(checkout.get("url", "")))
        link_row.addWidget(copy)
        box.addLayout(link_row)
        if checkout.get("valid_until"):
            box.addWidget(_label(f"The link works until {checkout['valid_until']}.", small=True, color=self.colors["muted"]))
        again = QtWidgets.QPushButton("Open checkout again")
        again.clicked.connect(lambda: self.c.open_link(checkout.get("url", "")))
        box.addWidget(again)
        back = QtWidgets.QPushButton("Back to the quote")
        back.clicked.connect(self.c.back_to_quote)
        box.addWidget(back)
        box.addStretch(1)
        footer = QtWidgets.QVBoxLayout()
        box.addLayout(footer)
        self._footer(footer, state)

    def _copy_link(self, url: str) -> None:
        QtWidgets.QApplication.clipboard().setText(url)
        self.show_toast("Checkout link copied.", "ok")

    def _render_error(self, state: dict) -> None:
        box = self.error_box
        _clear(box)
        error = state["error"] or {}
        box.addWidget(_label(error.get("title", "Something went wrong"), bold=True))
        box.addWidget(_label(error.get("message", "")))
        row = QtWidgets.QHBoxLayout()
        retry = _primary("Try again", self.c.retry)
        row.addWidget(retry)
        dismiss = QtWidgets.QPushButton("Back")
        dismiss.clicked.connect(self.c.dismiss)
        row.addWidget(dismiss)
        box.addLayout(row)
        link = error.get("link")
        if link:
            box.addWidget(_link_button(link["label"], lambda u=link["url"]: self.c.open_link(u)))
        box.addStretch(1)


def constants_icon() -> str:
    import os

    return os.path.join(os.path.dirname(os.path.dirname(__file__)), "resources", "icons", "MakePartsNow.svg")
