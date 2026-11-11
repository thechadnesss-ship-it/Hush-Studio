"""T23 in-app updater, against a fake GitHub releases endpoint on localhost (no internet)."""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication, QMessageBox, QPushButton, QWidget

from core import updater
from core.app_info import APP_VERSION

REPO = "someone/Hush-Studio-Releases"
SETUP_BYTES = b"MZ fake installer " * 50_000          # ~900 KB, several download chunks


class FakeGitHub:
    """Serves /repos/<repo>/releases/latest plus the Setup.exe and its .sha256."""

    def __init__(self):
        self.version = "99.0.0"
        self.prerelease = False
        self.status = 200
        self.setup = SETUP_BYTES
        self.sha = hashlib.sha256(SETUP_BYTES).hexdigest()
        self.requests: list[str] = []
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                fake.requests.append(self.path)
                base = f"http://127.0.0.1:{self.server.server_port}"
                name = f"Hush-Studio-Setup-{fake.version}.exe"
                if self.path == f"/repos/{REPO}/releases/latest":
                    if fake.status != 200:
                        return self._send(fake.status, b'{"message": "Not Found"}')
                    body = {
                        "tag_name": f"v{fake.version}", "draft": False, "prerelease": fake.prerelease,
                        "html_url": f"{base}/release-page",
                        "body": "- **Faster renders.**\r\n- Fixed the meter.\r\n\r\n---\r\nBuilt from x/y@abc by GitHub Actions.",
                        "assets": [
                            {"name": name, "size": len(fake.setup), "browser_download_url": f"{base}/dl/{name}"},
                            {"name": name + ".sha256", "size": 99,
                             "browser_download_url": f"{base}/dl/{name}.sha256"},
                        ]}
                    return self._send(200, json.dumps(body).encode())
                if self.path == f"/dl/{name}":
                    return self._send(200, fake.setup)
                if self.path == f"/dl/{name}.sha256":
                    return self._send(200, f"{fake.sha}  {name}\n".encode())
                self._send(404, b"")

            def _send(self, code, data):
                self.send_response(code)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture
def gh(monkeypatch):
    fake = FakeGitHub()
    monkeypatch.setenv("HUSH_UPDATE_API", fake.url)
    monkeypatch.setenv("HUSH_RELEASES_REPO", REPO)
    yield fake
    fake.server.shutdown()


@pytest.fixture
def offline(monkeypatch):
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()                                           # nothing listens there: connection refused
    monkeypatch.setenv("HUSH_UPDATE_API", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("HUSH_RELEASES_REPO", REPO)


# ------------------------------------------------------------------ core

def test_versions():
    assert updater.parse_version("v0.3.1") == (0, 3, 1)
    assert updater.parse_version("0.2") == (0, 2, 0)
    assert updater.parse_version("v0.3.1-rc.2") is None
    assert updater.is_newer("0.3.1", "0.3.0") and updater.is_newer("v0.10.0", "0.9.9")
    assert not updater.is_newer("0.3.0", "0.3.0") and not updater.is_newer("0.2.9", "0.3.0")
    assert not updater.is_newer("garbage", "0.3.0")


def test_check_finds_newer_release_without_login(gh):
    rel = updater.check(current="0.3.0")
    assert rel is not None and rel.version == "99.0.0"
    assert rel.notes == "- **Faster renders.**\n- Fixed the meter."       # build footer dropped
    assert rel.setup_name == "Hush-Studio-Setup-99.0.0.exe" and rel.sha256_url
    assert updater.check(current="99.0.0") is None                        # up to date
    gh.prerelease = True
    assert updater.check(current="0.3.0") is None                         # rc builds are never offered
    gh.prerelease, gh.status = False, 404
    assert updater.check(current="0.3.0") is None                         # no releases yet


def test_offline_raises_a_plain_error(offline):
    with pytest.raises(updater.UpdateError, match="internet"):
        updater.check(timeout=2)


def test_rate_limited(gh):
    gh.status = 403
    with pytest.raises(updater.UpdateError, match="busy"):
        updater.check()


def test_download_checks_sha256(gh, tmp_path):
    rel = updater.check(current="0.3.0")
    seen = []
    path = updater.download(rel, tmp_path / "updates", seen.append)
    assert path.read_bytes() == SETUP_BYTES and seen and seen[-1] == 1.0
    assert updater.file_sha256(path) == gh.sha
    gh.sha = "0" * 64                                                     # tampered / corrupted
    path.unlink()
    with pytest.raises(updater.UpdateError, match="checksum"):
        updater.download(rel, tmp_path / "updates")
    assert list((tmp_path / "updates").iterdir()) == []                   # nothing left to run


def test_download_can_be_cancelled(gh, tmp_path):
    rel = updater.check(current="0.3.0")
    stop = threading.Event()
    stop.set()
    with pytest.raises(updater.UpdateError, match="stopped"):
        updater.download(rel, tmp_path, cancel=stop)
    assert not any(tmp_path.iterdir())


def test_installer_handoff_script(tmp_path):
    setup = tmp_path / "it's here" / "Hush-Studio-Setup-9.0.0.exe"
    setup.parent.mkdir()
    setup.write_bytes(b"x")
    calls = []
    script = updater.launch_installer(setup, pid=4242, relaunch=Path(r"C:\Apps\Hush Studio\HushStudio.exe"),
                                      popen=lambda args, **kw: calls.append((args, kw)))
    text = script.read_text(encoding="utf-8-sig")
    assert "Get-Process -Id 4242" in text and "OpenExisting('HushStudioAppMutex')" in text   # waits for the app
    assert "'/VERYSILENT'" in text and "'/SUPPRESSMSGBOXES'" in text and "'/NORESTART'" in text
    assert "it''s here" in text                                          # PowerShell quoting
    assert text.index("Start-Process -FilePath 'C:\\Apps\\Hush Studio\\HushStudio.exe'") > text.index("-Wait")
    (args, kw), = calls
    assert args[0] == "powershell.exe" and args[-1] == str(script) and "Bypass" in args
    assert updater.last_install_exit_code(setup.parent) is None
    (setup.parent / "install.exitcode").write_text("0\r\n", encoding="ascii")
    assert updater.last_install_exit_code(setup.parent) == 0


def test_clean_downloads_only_removes_installers(tmp_path):
    for n in ("Hush-Studio-Setup-1.0.0.exe", "Hush-Studio-Setup-1.0.1.exe.part", "notes.txt"):
        (tmp_path / n).write_bytes(b"x")
    updater.clean_downloads(tmp_path)
    assert [p.name for p in tmp_path.iterdir()] == ["notes.txt"]


# ------------------------------------------------------------------ UI

@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def pump(app, seconds=0.0, until=None, timeout=10.0):
    end = time.perf_counter() + (seconds or timeout)
    while time.perf_counter() < end:
        app.processEvents()
        if until is not None and until():
            return True
        time.sleep(0.01)
    return until is None


class FakeWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.blocked: str | None = None
        self.closed = False

    def update_blocker(self):
        return self.blocked

    def close(self):
        self.closed = True
        return super().close()


@pytest.fixture
def ctl(app, tmp_path, monkeypatch):
    from ui import updates
    boxes = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: boxes.append(a[2]))
    win = FakeWindow()
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    btn = QPushButton()
    c = updates.UpdateController(win, settings, tmp_path / "updates", btn)
    c.boxes = boxes
    c.launched = []
    c.quits = []
    c.launch = lambda setup, relaunch=None: c.launched.append((setup, relaunch))
    c.quit_app = lambda: c.quits.append(1)
    yield c
    if c.dialog is not None:
        c.dialog.done(0)
    win.deleteLater()


def _check(app, c, manual=False):
    c.check(manual=manual)
    assert pump(app, until=lambda: c._worker is not None and c._worker.isFinished())
    pump(app, 0.05)


def test_offline_start_shows_nothing(app, ctl, offline):
    _check(app, ctl)
    assert ctl.boxes == [] and ctl.dialog is None and not ctl.header_button.isVisibleTo(ctl.window)
    _check(app, ctl, manual=True)                     # asking by hand does say why
    assert ctl.boxes and "internet" in ctl.boxes[0]


def test_up_to_date(app, ctl, gh, monkeypatch):
    gh.version = APP_VERSION
    _check(app, ctl)
    assert ctl.dialog is None and ctl.boxes == []
    _check(app, ctl, manual=True)
    assert "newest version" in ctl.boxes[0]


def test_offer_skip_and_manual_recheck(app, ctl, gh):
    _check(app, ctl)
    d = ctl.dialog
    assert d is not None and d.isVisible()
    assert "Faster renders" in d.notes.toPlainText()
    assert ctl.header_button.text() == "Update to 99.0.0"
    d.btn_skip.click()
    assert ctl.skipped() == "99.0.0" and not d.isVisible()
    ctl.dialog = None
    _check(app, ctl)                                  # skipped: start-up check stays quiet
    assert ctl.dialog is None
    _check(app, ctl, manual=True)                     # but Check for updates still shows it
    assert ctl.dialog is not None and ctl.dialog.isVisible()


def test_never_offered_or_installed_while_busy(app, ctl, gh, monkeypatch):
    ctl.window.blocked = "You're recording. Finish the take first; the update will wait."
    _check(app, ctl)
    assert ctl.dialog is None                         # no pop-up over a recording...
    assert ctl.header_button.isVisibleTo(ctl.window)  # ...just the header link
    monkeypatch.setattr(updater, "relaunch_target", lambda: Path("C:/x/HushStudio.exe"))
    ctl.show_dialog()
    ctl.dialog.btn_update.click()
    assert "recording" in ctl.dialog.status.text() and ctl.dialog.worker is None
    assert ctl.launched == [] and gh.requests.count(f"/dl/Hush-Studio-Setup-99.0.0.exe") == 0


def test_update_now_downloads_verifies_and_hands_over(app, ctl, gh, monkeypatch):
    exe = Path("C:/Users/x/AppData/Local/Programs/Hush Studio/HushStudio.exe")
    monkeypatch.setattr(updater, "relaunch_target", lambda: exe)
    _check(app, ctl)
    ctl.dialog.btn_update.click()
    assert pump(app, until=lambda: bool(ctl.launched), timeout=20)
    setup, relaunch = ctl.launched[0]
    assert setup == ctl.download_dir / "Hush-Studio-Setup-99.0.0.exe" and setup.read_bytes() == SETUP_BYTES
    assert relaunch == exe and ctl.window.closed and ctl.quits == [1]
    assert ctl.settings.value("update/pending_version") == "99.0.0"


def test_bad_checksum_is_not_installed(app, ctl, gh, monkeypatch):
    monkeypatch.setattr(updater, "relaunch_target", lambda: Path("C:/x/HushStudio.exe"))
    gh.sha = "f" * 64
    _check(app, ctl)
    ctl.dialog.btn_update.click()
    assert pump(app, until=lambda: "checksum" in ctl.dialog.status.text(), timeout=20)
    assert ctl.launched == [] and ctl.dialog.btn_update.isEnabled()


def test_after_restart_note(app, ctl, monkeypatch):
    from ui import updates
    ctl.download_dir.mkdir()
    (ctl.download_dir / "Hush-Studio-Setup-9.9.9.exe").write_bytes(b"x")
    assert ctl.after_start() is None                                   # normal start: just tidy up
    assert not (ctl.download_dir / "Hush-Studio-Setup-9.9.9.exe").exists()
    ctl.settings.setValue(updates.PENDING_KEY, APP_VERSION)            # the update landed
    assert "Updated to" in ctl.after_start()
    assert ctl.settings.value(updates.PENDING_KEY) is None
    ctl.settings.setValue(updates.PENDING_KEY, "99.0.0")               # Setup failed
    (ctl.download_dir / "install.exitcode").write_text("5", encoding="ascii")
    assert "didn't finish" in ctl.after_start()


def test_main_window_blocks_updates_while_recording_or_rendering(app):
    from ui.main_window import MainWindow
    w = MainWindow()
    try:
        assert any("Check for updates" in a.text() for a in w.help_menu.actions())
        assert not w.btn_update.isVisibleTo(w)
        assert w.update_blocker() is None
        for mode in ("recording", "paused", "busy", "review"):
            w._set_mode(mode)
            assert w.update_blocker()
        w._set_mode("done")
        assert w.update_blocker() is None
    finally:
        w.close()
        w.deleteLater()


@pytest.mark.skipif(os.name != "nt", reason="Windows hand-off (PowerShell)")
def test_real_handoff_waits_for_the_app_then_runs_setup_and_reopens(tmp_path):
    """The real PowerShell script with real processes: 'app' = a sleeping Python, 'Setup' and the
    reopened 'app' = small .cmd files that leave a trace."""
    import subprocess
    import sys
    folder = tmp_path / "up dates"                    # a space, like "Hush Studio"
    folder.mkdir()
    trace = folder / "trace.txt"
    setup = folder / "Hush-Studio-Setup-9.9.9.cmd"
    setup.write_text(f'@echo setup %* >> "{trace}"\r\n@exit /b 3\r\n', encoding="ascii")
    reopen = folder / "HushStudio.cmd"
    reopen.write_text(f'@echo reopened >> "{trace}"\r\n', encoding="ascii")
    app_proc = subprocess.Popen([sys.executable, "-c", f"import time; time.sleep(1.5); open(r'{trace}', 'a').write('app closed' + chr(10))"])
    updater.launch_installer(setup, pid=app_proc.pid, relaunch=reopen)
    app_proc.wait()
    deadline = time.time() + 30
    while time.time() < deadline and "reopened" not in (trace.read_text() if trace.exists() else ""):
        time.sleep(0.2)
    lines = [ln.strip() for ln in trace.read_text().splitlines()]
    assert lines[0] == "app closed"                                    # Setup only after the app exited
    assert lines[1].startswith("setup /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /NOCANCEL /SP- /LOG=")
    assert lines[2] == "reopened"
    assert updater.last_install_exit_code(folder) == 3
