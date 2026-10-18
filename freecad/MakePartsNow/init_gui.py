"""Registers the Make Parts Now workbench and its command when FreeCAD's GUI starts.

FreeCAD imports this module (``freecad.MakePartsNow.init_gui``) at startup for
every add-on, so it stays cheap: it defines the workbench and adds one command.
Nothing here touches the network, starts a thread, reads the model or imports
the panel; that happens only when the person runs the command.
"""

import os

import FreeCADGui as Gui

from . import commands
from .constants import COMMAND_ID, WORKBENCH_NAME

ICONS = os.path.join(os.path.dirname(__file__), "resources", "icons")


class MakePartsNowWorkbench(Gui.Workbench):
    MenuText = WORKBENCH_NAME
    ToolTip = "Instant 3D printing quotes for your parts, from makepartsnow.com"
    Icon = os.path.join(ICONS, "MakePartsNow.svg")

    def GetClassName(self):
        return "Gui::PythonWorkbench"

    def Initialize(self):
        self.appendToolbar(WORKBENCH_NAME, [COMMAND_ID])
        self.appendMenu(WORKBENCH_NAME, [COMMAND_ID])

    def Activated(self):
        pass

    def Deactivated(self):
        pass

    def ContextMenu(self, recipient):
        if recipient == "Tree":
            self.appendContextMenu("", [COMMAND_ID])


commands.register()
Gui.addWorkbench(MakePartsNowWorkbench())
