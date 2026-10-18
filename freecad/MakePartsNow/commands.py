"""The "Quote with Make Parts Now" command.

It is enabled when a document is open and something is selected, and opens the
task panel. The panel and the core are imported only when it runs.
"""

import os

import FreeCAD as App
import FreeCADGui as Gui

from .constants import COMMAND_ID, COMMAND_NAME, COMMAND_TOOLTIP

ICON = os.path.join(os.path.dirname(__file__), "resources", "icons", "Quote.svg")


class QuoteCommand:
    def GetResources(self):
        return {"Pixmap": ICON, "MenuText": COMMAND_NAME, "ToolTip": COMMAND_TOOLTIP}

    def IsActive(self):
        try:
            return App.ActiveDocument is not None and bool(Gui.Selection.getSelectionEx("", 0))
        except Exception:  # noqa: BLE001 - FreeCAD asks often; never raise from here
            return False

    def Activated(self):
        from .ui import panel

        panel.show()


def register():
    Gui.addCommand(COMMAND_ID, QuoteCommand())
