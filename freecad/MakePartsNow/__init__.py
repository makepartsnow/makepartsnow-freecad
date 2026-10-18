"""Make Parts Now for FreeCAD: instant 3D printing quotes for the selected parts.

``init_gui`` registers the workbench and its command when FreeCAD's GUI starts;
nothing here touches the network, starts a thread or reads the model at import.
"""

from .version import __version__  # noqa: F401
