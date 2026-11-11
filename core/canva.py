"""Small, local Canva integration used by the upload screen.

Canva Connect is deliberately not used here.  Sam opens her saved template in a
browser, exports a PNG to a local folder, and the app watches that folder for the
newest export.  The selected path is recorded beside the session so the YouTube
thumbnail flow can consume it later (T16).
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

CANVA_TEMPLATE_URL_KEY = "canva/template_url"
CANVA_EXPORT_FOLDER_KEY = "canva/export_folder"
SESSION_THUMBNAIL_FILE = "thumbnail.json"
PNG_SUFFIXES = {".png"}


def _setting(settings: Any, key: str, default: Any = "") -> Any:
    if settings is None:
        return default
    if isinstance(settings, dict):
        return settings.get(key, default)
    return settings.value(key, default)


def _set_setting(settings: Any, key: str, value: str) -> None:
    if settings is None:
        raise ValueError("Canva settings need a QSettings-compatible object")
    if isinstance(settings, dict):
        settings[key] = value
    else:
        settings.setValue(key, value)
        sync = getattr(settings, "sync", None)
        if sync is not None:
            sync()


def canva_template_url(settings: Any) -> str:
    """Return the saved Canva template URL, or an empty string if not configured."""
    return str(_setting(settings, CANVA_TEMPLATE_URL_KEY, "") or "").strip()


def set_canva_template_url(settings: Any, url: str) -> str:
    """Save and return a Canva URL after a small, user-facing sanity check."""
    value = str(url or "").strip()
    if value and not value.lower().startswith(("https://", "http://")):
        raise ValueError("The Canva template link must start with http:// or https://")
    _set_setting(settings, CANVA_TEMPLATE_URL_KEY, value)
    return value


def canva_export_folder(settings: Any) -> Path | None:
    """Return the saved export folder, preserving a missing folder for the UI to repair."""
    value = str(_setting(settings, CANVA_EXPORT_FOLDER_KEY, "") or "").strip()
    return Path(value) if value else None


def set_canva_export_folder(settings: Any, folder: str | Path) -> Path:
    value = Path(folder).expanduser()
    if not value.is_dir():
        raise ValueError(f"Canva export folder does not exist: {value}")
    value = value.resolve()
    _set_setting(settings, CANVA_EXPORT_FOLDER_KEY, str(value))
    return value


def png_exports(folder: str | Path | None) -> list[Path]:
    """List usable PNG exports in modification order (oldest first)."""
    if not folder:
        return []
    root = Path(folder).expanduser()
    if not root.is_dir():
        return []
    found: list[tuple[int, str, Path]] = []
    for path in root.iterdir():
        try:
            if path.is_file() and path.suffix.lower() in PNG_SUFFIXES and path.stat().st_size > 0:
                stat = path.stat()
                found.append((stat.st_mtime_ns, path.name.casefold(), path))
        except OSError:
            # Canva may still be moving or finishing a file.  The next poll will retry it.
            continue
    return [item[2] for item in sorted(found)]


def newest_png(folder: str | Path | None) -> Path | None:
    """Return the newest non-empty PNG export, without opening or copying it."""
    exports = png_exports(folder)
    return exports[-1] if exports else None


class CanvaExportWatcher:
    """A tiny polling watcher with no Qt dependency.

    ``newest`` is useful for an immediate UI refresh.  ``poll`` reports a path
    once when its newest-file signature changes, which makes it convenient for
    tests and for callers that want to avoid repeating a notification.
    """

    def __init__(self, folder: str | Path | None = None):
        self.folder = Path(folder).expanduser() if folder else None
        self._last_signature: tuple[str, int, int] | None = None

    def set_folder(self, folder: str | Path | None) -> None:
        self.folder = Path(folder).expanduser() if folder else None
        self._last_signature = None

    def newest(self) -> Path | None:
        return newest_png(self.folder)

    def poll(self) -> Path | None:
        path = self.newest()
        if path is None:
            self._last_signature = None
            return None
        try:
            stat = path.stat()
            signature = (str(path.resolve()), stat.st_mtime_ns, stat.st_size)
        except OSError:
            return None
        if signature == self._last_signature:
            return None
        self._last_signature = signature
        return path


def remember_thumbnail(session_dir: str | Path, image_path: str | Path) -> Path:
    """Record the chosen export path in a session and return its resolved path."""
    image = Path(image_path).expanduser()
    if image.suffix.lower() not in PNG_SUFFIXES:
        raise ValueError("Canva thumbnails must be PNG files")
    if not image.is_file():
        raise FileNotFoundError(image)
    image = image.resolve()
    session = Path(session_dir)
    session.mkdir(parents=True, exist_ok=True)
    metadata = {
        "path": str(image),
        "selected_at": time.time(),
        "source": "canva_export",
    }
    target = session / SESSION_THUMBNAIL_FILE
    temp = target.with_name(target.name + ".part")
    temp.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    temp.replace(target)
    return image


def session_thumbnail(session_dir: str | Path) -> Path | None:
    """Read a previously selected thumbnail path if the export still exists."""
    path = Path(session_dir) / SESSION_THUMBNAIL_FILE
    try:
        value = json.loads(path.read_text(encoding="utf-8")).get("path")
        image = Path(value) if value else None
    except (OSError, ValueError, TypeError, AttributeError):
        return None
    return image if image and image.is_file() else None


# Short aliases make the integration easy for the later upload/thumbnail task to consume.
save_thumbnail = remember_thumbnail
thumbnail_for_session = session_thumbnail
