"""The consent text shown once per install, before anything can be uploaded.

It says what is sent, when, how long files are kept, how to delete them, and
links to the privacy policy, terms and help. Usage counts are a separate,
unchecked checkbox. The add-in's panel draws these sections; nothing here is
HTML, so a page escapes it like any other text. Raise the host's
``consent_version`` when this text changes materially, so everyone accepts the
new text once.
"""

from __future__ import annotations

from typing import Any

from .host import PRIVACY_URL, SUPPORT_EMAIL, TERMS_URL, Host

TELEMETRY_LABEL = "Send anonymous usage counts"
TELEMETRY_HINT = (
    "Off unless you tick it. Only counts such as \"quotes shown: 3\" with the {kind} version: "
    "no files, part names, ids or personal data."
)

TITLE = "Before your first quote"
INTRO = "Make Parts Now prices your parts on makepartsnow.com. Here is what that involves."


def consent_view(host: Host) -> dict[str, Any]:
    """The consent screen's content, as plain text for the panel."""
    return {
        "title": TITLE,
        "intro": INTRO,
        "sections": [
            {
                "heading": "What is sent",
                "items": [
                    host.text(
                        "consent.sent_file",
                        "A STEP file of each part you add, named after the part (an STL for a mesh, or only if STEP export fails and you agree). "
                        "Your name and company are removed from the STEP header first",
                    ),
                    "The quantity, material and finish or colour you choose for each part",
                    "The ZIP code the parts ship to",
                ],
            },
            {
                "heading": "When",
                "items": [
                    host.text("consent.when_quote", f"Only when you click {host.quote_action}. Selecting parts in your design sends nothing."),
                    host.text("consent.when_open", "Opening the panel only fetches the current materials, limits and any promotion code."),
                    "Nothing else in your design is sent.",
                ],
            },
            {
                "heading": "Your files",
                "items": [
                    "Stored encrypted and deleted automatically under our privacy policy. "
                    "Files on an order are kept with the order.",
                    "Delete them sooner with Delete my uploaded files, or by emailing " + SUPPORT_EMAIL + ".",
                ],
            },
            {
                "heading": "Paying",
                "items": [
                    f"The {host.kind} is free. You order and pay on makepartsnow.com through Stripe; "
                    f"the {host.kind} never sees card details.",
                ],
            },
        ],
        "links": [
            {"label": "Privacy policy", "url": PRIVACY_URL},
            {"label": "Terms", "url": TERMS_URL},
            {"label": "Help", "url": host.help_url},
        ],
        "telemetry_label": TELEMETRY_LABEL,
        "telemetry_hint": TELEMETRY_HINT.format(kind=host.kind),
        "not_affiliated": host.not_affiliated,
    }
