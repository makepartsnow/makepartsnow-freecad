"""Names, links and the core ``Host`` for the FreeCAD workbench."""

from __future__ import annotations

from .core.host import COMMON_EVENTS, Host
from .version import __version__

VERSION = __version__
WORKBENCH_NAME = "Make Parts Now"
COMMAND_ID = "MakePartsNow_Quote"
COMMAND_NAME = "Quote with Make Parts Now"
COMMAND_TOOLTIP = (
    "Instant prices for 3D printing the selected bodies in SLS nylon or FDM plastics, "
    "then order them on makepartsnow.com. No account needed."
)
PANEL_TITLE = "Make Parts Now"
REPO_URL = "https://github.com/makepartsnow/makepartsnow-freecad"
HELP_URL = "https://makepartsnow.com/how-it-works"  # a /freecad page comes with the public release
NOT_AFFILIATED = "Make Parts Now is not affiliated with the FreeCAD project. FreeCAD is a trademark of the FreeCAD project association."
CONSENT_VERSION = 1

HOST = Host(
    client_name="freecad",  # the server's rates.CAD_CLIENT_SOURCES key: source freecad_addin
    product="FreeCAD",  # User-Agent MakePartsNow-FreeCAD/<version>; Cloudflare refuses urllib's default
    version=VERSION,
    program="FreeCAD",
    help_url=HELP_URL,
    repo_url=REPO_URL,
    settings_file="freecad-workbench.json",
    base_url_env="MPN_FREECAD_BASE_URL",
    consent_version=CONSENT_VERSION,
    not_affiliated=NOT_AFFILIATED,
    kind="workbench",
    quote_action="Get instant quote",
    select_hint="Select bodies in the 3D view or the tree",
    events=COMMON_EVENTS,  # not quote_saved_to_part: v1 doesn't save quotes into the document
)

# Mesh fallback for a solid whose STEP export fails, only when the person agrees (benchmark before changing).
MESH_LINEAR_DEFLECTION_MM = 0.05
MESH_ANGULAR_DEFLECTION_RAD = 0.35
