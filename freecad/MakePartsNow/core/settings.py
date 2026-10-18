"""Per-install settings, kept in a small JSON file in the user's app data folder.

Stored: whether and when the user accepted the consent text (and which version),
the usage-count opt-in (off by default), the last ZIP code, and an optional
developer base URL. A base URL other than makepartsnow.com puts a TEST badge on
the panel. Nothing here is sent anywhere.

Each add-in has its own file (``Host.settings_file``) and its own base URL
override variable (``Host.base_url_env``). The folder is the host's choice:
``settings_dir()`` gives ``%APPDATA%\\MakePartsNow`` on Windows and
``~/.config/makepartsnow`` elsewhere, and a host may pass its own data
directory instead (FreeCAD's user data folder, for example).
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import urllib.parse
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from typing import Optional

from .host import DEFAULT_BASE_URL, Host

ZIP_RE = re.compile(r"^\d{5}(-\d{4})?$")


def settings_dir(app_data_dir: Optional[str] = None) -> str:
    """The folder for the settings file: ``app_data_dir/MakePartsNow`` when the host gives one."""
    if app_data_dir:
        return os.path.join(app_data_dir, "MakePartsNow")
    appdata = os.environ.get("APPDATA")
    if os.name == "nt" and appdata:
        return os.path.join(appdata, "MakePartsNow")
    return os.path.join(os.path.expanduser("~"), ".config", "makepartsnow")


@dataclass
class Settings:
    consent_version: int = 0
    consent_at: str = ""
    telemetry_opt_in: bool = False
    zip: str = ""
    base_url: str = ""

    def has_consent(self, host: Host) -> bool:
        return self.consent_version >= host.consent_version

    def accept_consent(self, host: Host, telemetry_opt_in: bool, now: Optional[datetime] = None) -> None:
        self.consent_version = host.consent_version
        self.consent_at = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
        self.telemetry_opt_in = bool(telemetry_opt_in)

    def effective_base_url(self, host: Host) -> str:
        override = os.environ.get(host.base_url_env) or self.base_url
        if override and is_allowed_base_url(override):
            return override.rstrip("/")
        return DEFAULT_BASE_URL

    def is_test(self, host: Host) -> bool:
        return self.effective_base_url(host) != DEFAULT_BASE_URL


def is_allowed_base_url(url: str) -> bool:
    """HTTPS anywhere, or plain HTTP only to this computer (local development)."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme == "https" and parsed.netloc:
        return True
    return parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1")


def normalize_zip(value: str) -> str:
    return (value or "").strip()


def is_valid_zip(value: str) -> bool:
    return bool(ZIP_RE.match(normalize_zip(value)))


def load(host: Host, directory: Optional[str] = None) -> Settings:
    """Read the settings file; a missing or unreadable file gives the defaults."""
    path = os.path.join(directory or settings_dir(), host.settings_file)
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return Settings()
    if not isinstance(raw, dict):
        return Settings()
    values = {f.name: raw[f.name] for f in fields(Settings) if f.name in raw}
    try:
        settings = Settings(**values)
    except TypeError:
        return Settings()
    settings.consent_version = int(settings.consent_version or 0) if str(settings.consent_version).isdigit() else 0
    settings.telemetry_opt_in = settings.telemetry_opt_in is True
    settings.zip = str(settings.zip or "")
    settings.base_url = str(settings.base_url or "")
    settings.consent_at = str(settings.consent_at or "")
    return settings


def save(settings: Settings, host: Host, directory: Optional[str] = None) -> str:
    """Write the settings atomically and return the file path."""
    directory = directory or settings_dir()
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, host.settings_file)
    fd, tmp = tempfile.mkstemp(prefix=".settings-", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(asdict(settings), fh, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return path
