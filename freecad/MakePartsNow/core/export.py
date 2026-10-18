"""Export helpers shared by every add-in: the private temp folder, safe file names, the STEP header scrub.

Writing the file is the host's job, because only it can touch the model: the
host passes an ``Exporter`` callback, ``exporter(part, path, fmt) -> bool``,
which writes ``part`` to ``path`` as ``fmt`` ("step" or "stl", STL in
millimeters) and returns whether it did. ``export_part`` names the file, calls
it, checks a file was written, and scrubs the STEP header.

Export calls the CAD program's API, so it runs on the main thread. The folder is
made with ``tempfile.mkdtemp`` (readable only by the current user) and removed
by ``cleanup`` after the upload, on success or failure.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from typing import Any, Callable, Optional

TEMP_PREFIX = "MakePartsNow-"

Exporter = Callable[[Any, str, str], bool]  # (part, path, fmt) -> the file was written


@dataclass(frozen=True)
class ExportedFile:
    path: str
    file_name: str
    fmt: str  # "step" or "stl"

    @property
    def size(self) -> int:
        return os.path.getsize(self.path)

    @property
    def units(self) -> Optional[str]:
        """The units to tell the server: STL has none in the file, and every add-in writes mm."""
        return "mm" if self.fmt == "stl" else None


class ExportError(Exception):
    """An export produced no file."""


def make_temp_dir() -> str:
    return tempfile.mkdtemp(prefix=TEMP_PREFIX)


def cleanup(directory: Optional[str]) -> None:
    """Delete the export folder and everything in it. Never raises."""
    if directory and os.path.basename(directory).startswith(TEMP_PREFIX):
        shutil.rmtree(directory, ignore_errors=True)


_UNSAFE = re.compile(r"[^A-Za-z0-9._ -]+")


def safe_file_name(name: str, ext: str, used: set[str]) -> str:
    """A short, filesystem- and header-safe file name, unique within ``used``."""
    base = _UNSAFE.sub("_", name).strip(" ._") or "part"
    base = base[:60]
    candidate = f"{base}.{ext}"
    n = 2
    while candidate.lower() in used:
        candidate = f"{base}-{n}.{ext}"
        n += 1
    used.add(candidate.lower())
    return candidate


def written(path: str) -> bool:
    """True when ``path`` is a non-empty file."""
    return os.path.isfile(path) and os.path.getsize(path) > 0


HEADER_SCAN_BYTES = 256 * 1024  # the STEP header (HEADER; ... ENDSEC;) is at the start of the file


def _split_step_args(text: str) -> Optional[list[str]]:
    """Top-level comma-separated arguments of a STEP record's parameter list (strings use '' as an escaped quote)."""
    args, depth, start, i, in_str = [], 0, 0, 0, False
    while i < len(text):
        ch = text[i]
        if in_str:
            if ch == "'":
                if i + 1 < len(text) and text[i + 1] == "'":
                    i += 1
                else:
                    in_str = False
        elif ch == "'":
            in_str = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                return None
        elif ch == "," and depth == 0:
            args.append(text[start:i].strip())
            start = i + 1
        i += 1
    if in_str or depth != 0:
        return None
    args.append(text[start:].strip())
    return args


def _step_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def scrub_step_header(path: str, file_name: str) -> bool:
    """Blank the author, organization and authorization in the STEP header's FILE_NAME record, and keep only the
    file's own name in its name field (a CAD program can write the full temp path, which holds the user name).
    Geometry is untouched. Returns True when the header was rewritten; a file it can't parse is left as it is."""
    with open(path, "rb") as fh:
        head = fh.read(HEADER_SCAN_BYTES)
        rest = fh.read()
    text = head.decode("latin-1")
    start = text.find("FILE_NAME")
    end_header = text.find("ENDSEC;")
    if start < 0 or end_header < 0 or start > end_header:
        return False
    open_paren = text.find("(", start)
    close = text.find(";", open_paren)
    if open_paren < 0 or close < 0 or close > end_header:
        return False
    body = text[open_paren + 1 : close].rstrip()
    if not body.endswith(")"):
        return False
    args = _split_step_args(body[:-1])
    if args is None or len(args) != 7:
        return False
    args[0] = _step_string(file_name)
    args[2] = "('')"
    args[3] = "('')"
    args[6] = "''"
    record = "FILE_NAME(" + ",".join(args) + ");"
    new_text = text[:start] + record + text[close + 1 :]
    with open(path, "wb") as fh:
        fh.write(new_text.encode("latin-1"))
        fh.write(rest)
    return True


def export_part(exporter: Exporter, part: Any, name: str, directory: str, used: set[str], fmt: str, program: str) -> ExportedFile:
    """Export one part as ``fmt`` ("step" or "stl") through the host's ``exporter``, or raise ExportError.

    ``name`` is the part's display name (the file is named after it); ``program``
    is the CAD program's name for the error text.
    """
    ext = "step" if fmt == "step" else "stl"
    file_name = safe_file_name(name, ext, used)
    path = os.path.join(directory, file_name)
    try:
        ok = bool(exporter(part, path, fmt)) and written(path)
    except Exception:  # noqa: BLE001 - reported to the user as an export failure
        ok = False
    if ok and fmt == "step":
        try:
            scrub_step_header(path, file_name)
        except OSError:
            pass  # the file is still valid; the header just keeps what the CAD program wrote
    if not ok:
        used.discard(file_name.lower())
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass
        raise ExportError(f'{program} could not export "{name}" as {ext.upper()}.')
    return ExportedFile(path=path, file_name=file_name, fmt=fmt)
