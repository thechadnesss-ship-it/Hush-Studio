import json
import time

import pytest

from core.canva import (CanvaExportWatcher, canva_export_folder, canva_template_url,
                         newest_png, png_exports, remember_thumbnail, session_thumbnail,
                         set_canva_export_folder, set_canva_template_url)


class Settings(dict):
    def value(self, key, default=""):
        return self.get(key, default)

    def setValue(self, key, value):
        self[key] = value


def test_canva_settings_round_trip(tmp_path):
    settings = Settings()
    assert set_canva_template_url(settings, "https://www.canva.com/design/abc")
    folder = set_canva_export_folder(settings, tmp_path)
    assert canva_template_url(settings).endswith("/abc")
    assert canva_export_folder(settings) == folder


def test_template_url_rejects_non_web_links():
    with pytest.raises(ValueError):
        set_canva_template_url(Settings(), "C:\\template")


def test_newest_png_ignores_other_files_and_empty_exports(tmp_path):
    old = tmp_path / "old.png"
    old.write_bytes(b"png")
    (tmp_path / "notes.txt").write_text("not an image")
    (tmp_path / "unfinished.PNG").touch()
    time.sleep(0.01)
    newest = tmp_path / "new.PNG"
    newest.write_bytes(b"png")
    assert png_exports(tmp_path) == [old, newest]
    assert newest_png(tmp_path) == newest


def test_watcher_reports_each_newest_signature_once(tmp_path):
    watcher = CanvaExportWatcher(tmp_path)
    assert watcher.poll() is None
    image = tmp_path / "export.png"
    image.write_bytes(b"png")
    assert watcher.poll() == image
    assert watcher.poll() is None
    time.sleep(0.01)
    image.write_bytes(b"new png")
    assert watcher.poll() == image


def test_remember_thumbnail_writes_session_metadata(tmp_path):
    image = tmp_path / "export.png"
    image.write_bytes(b"png")
    session = tmp_path / "take"
    selected = remember_thumbnail(session, image)
    assert selected == image.resolve()
    assert session_thumbnail(session) == image.resolve()
    metadata = json.loads((session / "thumbnail.json").read_text(encoding="utf-8"))
    assert metadata["path"] == str(image.resolve())
    assert metadata["source"] == "canva_export"


def test_remember_thumbnail_requires_existing_png(tmp_path):
    with pytest.raises(FileNotFoundError):
        remember_thumbnail(tmp_path / "take", tmp_path / "missing.png")
    jpg = tmp_path / "image.jpg"
    jpg.write_bytes(b"image")
    with pytest.raises(ValueError):
        remember_thumbnail(tmp_path / "take", jpg)
