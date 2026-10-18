"""User-facing text for every error an add-in can show.

The server's own message is shown after ours when it adds something, so a new
server error code still reads sensibly. The texts name the CAD program, the
kind of add-in and the quote button through ``{program}``, ``{kind}`` and
``{action}``, filled from the ``Host``.
"""

from __future__ import annotations

from typing import Optional

from .host import SUPPORT_EMAIL, Host

ERROR_TEXT: dict[str, str] = {
    "SERVICE_PAUSED": "Make Parts Now has paused quoting from {program} for a short while. Please try again later, or quote on makepartsnow.com.",
    "RATE_LIMITED": "Too many requests in a short time. Wait a minute, then try again.",
    "FILE_TOO_LARGE": "A part's file is larger than the upload limit. Simplify the part or split it, then try again.",
    "UNSUPPORTED_FILE_TYPE": "The server could not accept this file type. The {kind} sends STEP, or STL if STEP export failed.",
    "FILE_PARSE_FAILED": "The server could not read a part's geometry. Check that the part is a closed solid, then try again.",
    "QUOTE_NOT_FOUND": "That quote has expired or no longer exists. Get a new instant quote.",
    "QUOTE_NOT_READY": "The quote is not ready to order yet. Wait for every part to finish, then try again.",
    "QUOTE_ALREADY_ORDERED": "This quote has already been ordered. Get a new instant quote.",
    "QUOTE_REVISED": "This quote was revised. Use the newest quote.",
    "PART_NOT_FOUND": "An uploaded part has expired or was deleted. Click {action} to upload it again.",
    "PART_ON_ORDER": "This part is on an order, so its file is kept until the order ships.",
    "DELETE_TOKEN_INVALID": f"The server did not accept the delete request. Email {SUPPORT_EMAIL} and we will delete the files.",
    "UPLOAD_NOT_RECEIVED": "The server did not receive one of the files. Click {action} to try again.",
    "FILE_NOT_RECEIVED": "The server did not receive one of the files. Click {action} to try again.",
    "UPLOAD_FAILED": "A file upload failed. Click {action} to try again.",
    "INVALID_ZIP": "Enter a valid US ZIP code (5 digits).",
    "ZIP_REQUIRED": "Enter the ZIP code the parts ship to.",
    "INVALID_ARGUMENT": "The request was not accepted. Check the quantities and finishes, then try again.",
    "INVALID_OPTION": "One of the chosen options is not available. Check the materials and finishes, then try again.",
    "FINISH_NOT_SUPPORTED": "One of the chosen finishes or colours is not available for its material. Choose another one.",
    "UNITS_CONFIRMATION_REQUIRED": "The server needs to know the file's units. The {kind} sends millimeters; try again.",
    "SERVICE_UNAVAILABLE": "Make Parts Now is not answering right now. Please try again in a few minutes.",
    "INTERNAL_ERROR": f"Something went wrong on our side. Please try again, or email {SUPPORT_EMAIL}.",
    "NETWORK_ERROR": "Could not reach makepartsnow.com. Check your internet connection (and any proxy or firewall), then try again.",
    "TIMEOUT": "makepartsnow.com took too long to answer. Check your connection, then try again.",
    "TLS_ERROR": "A secure connection to makepartsnow.com could not be made. Check that your computer's date and time are right and that no network filter is intercepting HTTPS.",
    "PROTOCOL_ERROR": "The server's answer was not understood. Update the {kind}, or try again later.",
    "QUOTE_TIMEOUT": "The parts are taking longer than usual to analyze. Click {action} again in a minute to pick up the result.",
    "SERVER_ERROR": "The server reported an error.",
    "PROCESS_NOT_AVAILABLE": "This part can't be made in one piece in the chosen material (it may be larger than the build volume). Split it, choose another material, or remove it.",
    "restricted_product_category": "This part looks like a product we can't make. Remove it from the selection, or email us if you think this is wrong.",
    "NEEDS_INPUT": "Some parts need attention (marked in red). Fix them in {program} or remove them, then click {action} again; the other parts are not uploaded again.",
}

# Wording for a part's own error, shown on its row. Codes not listed fall back to ERROR_TEXT.
PART_ERROR_TEXT: dict[str, str] = {
    "UPLOAD_NOT_RECEIVED": "The file did not arrive. Click {action} to upload it again.",
    "PART_NOT_FOUND": "The uploaded file expired or was deleted. Click {action} to upload it again.",
    "FILE_PARSE_FAILED": "The file could not be read. Check that the part is a closed solid, then click {action}.",
    "UNITS_CONFIRMATION_REQUIRED": "The file's units are unclear. Choose millimeters or inches, then click {action}.",
    "PROCESS_NOT_AVAILABLE": "Can't be made in one piece in this material. Split it, choose another material, or remove it.",
    "restricted_product_category": "We can't make this kind of product. Remove it from the selection.",
}

# Part errors fixed by uploading the part again (the others need a change in the CAD program or removal).
REUPLOAD_CODES = frozenset({"UPLOAD_NOT_RECEIVED", "PART_NOT_FOUND", "UNITS_CONFIRMATION_REQUIRED", "FILE_PARSE_FAILED"})


def _fill(text: str, host: Optional[Host]) -> str:
    if host is None:
        return text.format(program="your CAD program", kind="add-in", action="Get instant quote")
    return text.format(program=host.program, kind=host.kind, action=host.quote_action)


def describe_part_error(code: str, server_message: str = "", host: Optional[Host] = None) -> str:
    """Short text for a part's error, for its line in the results."""
    text = PART_ERROR_TEXT.get(code)
    if text is not None and host is not None:
        text = host.text("part_error." + code, text)
    if text is None:
        return describe_error(code, server_message, host)
    text = _fill(text, host)
    if server_message and server_message.rstrip(".") not in text:
        return f"{text} ({server_message})"
    return text


def describe_error(code: str, server_message: str = "", host: Optional[Host] = None) -> str:
    """Our text for ``code``, followed by the server's message when it adds detail."""
    ours = ERROR_TEXT.get(code)
    if ours is not None and host is not None:
        ours = host.text("error." + code, ours)
    if ours is None:
        return f"{server_message or 'The request failed.'} ({code})"
    ours = _fill(ours, host)
    if server_message and server_message.rstrip(".") not in ours:
        return f"{ours} Details: {server_message}"
    return ours
