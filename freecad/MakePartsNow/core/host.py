"""What the core needs to know about the CAD program it runs in.

Each add-in builds one ``Host`` and passes it to the client, the controller,
the settings and the user-facing text. Nothing in the core names a CAD program
itself: the ``?client=`` tag, the User-Agent product, the program's name in
messages, the settings file and the consent version all come from here.

Links and limits that are the same for every add-in are module constants.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

# Public links, the same for every add-in.
DEFAULT_BASE_URL = "https://makepartsnow.com"
PRIVACY_URL = "https://makepartsnow.com/legal/privacy"
TERMS_URL = "https://makepartsnow.com/legal/terms"
SUPPORT_EMAIL = "nathan@makepartsnow.com"  # the support address (owner, 15 Oct 2026)

# Product defaults, used until get_options answers (the server's values win).
DEFAULT_MATERIAL_ID = "pa12"
DEFAULT_FINISH_ID = "gray"
SUBLINE = "SLS nylon and FDM plastics · Standard speed"
MAX_PARTS = 25
DEFAULT_MAX_QUANTITY = 10000
DEFAULT_MAX_FILE_MB = 100
QUOTE_WAIT_SECONDS = 180  # total time an add-in waits for parts to finish analysis

# Usage-count names every add-in may send (the server accepts only these).
COMMON_EVENTS = frozenset(
    {
        "dialog_opened",  # the panel was opened (name kept from Fusion 0.1 for the server's allowlist)
        "consent_accepted",
        "quote_requested",
        "quote_shown",
        "quote_failed",
        "units_mismatch",
        "step_export_failed",
        "stl_fallback_used",
        "order_clicked",
        "files_deleted",
    }
)


# The sentences a host may replace through ``Host.wording``. Error texts are keyed
# "error.<CODE>" and "part_error.<CODE>" (see ``messages``) and may use
# ``{program}``, ``{kind}`` and ``{action}`` like the core's own.
WORDING_KEYS = frozenset(
    {
        "consent.sent_file",  # "What is sent": the STEP file line
        "consent.when_quote",  # "When": only when you click the quote button
        "consent.when_open",  # "When": opening the panel
        "nothing_selected",  # Add selected with nothing selected
        "add_a_part",  # the quote button with no parts
        "save_failed_hint",  # after "Couldn't save to <names>"
    }
)


@dataclass(frozen=True)
class Host:
    """One add-in's identity.

    ``client_name`` is the ``?client=`` tag and the usage-count ``client`` (the
    add-in's tag on the server). ``product`` names the add-in in
    the User-Agent (``MakePartsNow-<product>/<version>``; the site refuses
    urllib's default) and in MCP ``clientInfo``. ``program`` is the CAD
    program's name in text the user reads ("Units match FreeCAD").

    ``wording`` replaces a few sentences the core would otherwise write, by key
    (``WORDING_KEYS``), so an add-in keeps the words its users already know
    ("body" instead of "part", "palette" instead of "panel"). Missing keys keep
    the core's text.
    """

    client_name: str  # "fusion", "freecad"
    product: str  # "Fusion", "FreeCAD"
    version: str
    program: str  # "Fusion", "FreeCAD"
    help_url: str
    repo_url: str
    settings_file: str  # "fusion-addin.json", "freecad-workbench.json"
    base_url_env: str  # "MPN_FUSION_BASE_URL"
    consent_version: int
    not_affiliated: str
    kind: str = "add-in"  # what the user calls it: "add-in", "workbench"
    quote_action: str = "Get instant quote"  # the button that uploads
    select_hint: str = "Select parts in your design"  # how to pick parts, as an instruction
    events: frozenset[str] = field(default=COMMON_EVENTS)
    wording: Mapping[str, str] = field(default_factory=dict, hash=False)

    def text(self, key: str, default: str) -> str:
        """The host's sentence for ``key``, or the core's ``default``."""
        return self.wording.get(key, default)

    @property
    def user_agent(self) -> str:
        return f"MakePartsNow-{self.product}/{self.version}"

    @property
    def client_info_name(self) -> str:
        return f"makepartsnow-{self.product.lower()}"
