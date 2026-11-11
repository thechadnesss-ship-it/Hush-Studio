"""T24: fixes from Sam's first laptop test (import in the installed app, intro bundled, no beep)."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from core import ffmpeg_tools as ff
from core.session import SR, load_mono_48k

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")             # the window tests below
if sys.platform == "win32":
    os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
sys.path.insert(0, str(ROOT / "build"))
import check_bundle  # noqa: E402


def _tone(seconds: float = 2.0, sr: int = 44100) -> np.ndarray:
    t = np.arange(int(seconds * sr)) / sr
    return (0.2 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


@pytest.mark.parametrize("ext,codec", [("wav", None), ("flac", None), ("mp3", "libmp3lame"), ("m4a", "aac")])
def test_import_formats(tmp_path, ext, codec):
    """wav / flac / mp3 / m4a at 44.1 kHz (Riverside) → mono 48 kHz."""
    src = tmp_path / "src.wav"
    sf.write(src, _tone(), 44100)
    path = src
    if ext != "wav":
        path = tmp_path / f"take.{ext}"
        args = [ff.ffmpeg_bin(), "-v", "error", "-y", "-i", str(src)]
        if codec:
            args += ["-c:a", codec]
        r = subprocess.run(args + [str(path)], capture_output=True, stdin=subprocess.DEVNULL)
        if r.returncode != 0:
            pytest.skip(f"this FFmpeg can't write {ext}")
    y = load_mono_48k(path)
    assert y.dtype == np.float32
    assert abs(len(y) / SR - 2.0) < 0.1
    rms = float(np.sqrt(np.mean(y[SR // 4:-SR // 4] ** 2)))
    assert abs(rms - 0.2 / np.sqrt(2)) < 0.02          # level survives (AAC peaks overshoot a bit)


def test_import_bad_file_gives_readable_error(tmp_path):
    bad = tmp_path / "not_audio.mp3"
    bad.write_bytes(b"this is not audio" * 100)
    with pytest.raises(RuntimeError, match="FFmpeg could not read this audio file"):
        load_mono_48k(bad)


def test_import_works_without_a_console_stdin(tmp_path):
    """The installed app has no console: FFmpeg children must not inherit stdin."""
    src = tmp_path / "take.wav"
    sf.write(src, _tone(1.0), 44100)
    code = ("import sys; sys.path.insert(0, sys.argv[1]); from pathlib import Path\n"
            "if sys.platform == 'win32':\n"   # like the console-less installed app: a dead stdin handle
            "    import ctypes; ctypes.windll.kernel32.SetStdHandle(-10, ctypes.c_void_p(0x1234))\n"
            "from core.session import load_mono_48k; print(len(load_mono_48k(Path(sys.argv[2]))))")
    flags = {"creationflags": subprocess.DETACHED_PROCESS} if sys.platform == "win32" else {}
    r = subprocess.run([sys.executable, "-c", code, str(ROOT), str(src)], stdin=subprocess.DEVNULL,
                       capture_output=True, text=True, **flags)
    assert r.returncode == 0, r.stderr
    assert abs(int(r.stdout.strip()) - SR) < 0.05 * SR


def test_ffmpeg_children_never_inherit_stdin():
    for rel in ("core/session.py", "core/renderer.py", "core/denoise.py", "core/diagnostics.py",
                "core/ffmpeg_tools.py"):
        src = (ROOT / rel).read_text(encoding="utf-8")
        calls = src.count("subprocess.run(") + src.count("subprocess.Popen(")
        redirected = src.count("stdin=subprocess.DEVNULL") + src.count("stdin=subprocess.PIPE")
        assert redirected >= calls, rel


def test_intro_card_is_bundled():
    card = ROOT / "assets" / "intro_card.mp4"
    assert card.exists()
    assert "_internal/assets/intro_card.mp4" in check_bundle.REQUIRED
    spec = (ROOT / "build" / "hush_studio.spec").read_text(encoding="utf-8")
    assert '"intro_card.mp4"' in spec
    from core.intros import validate_intro_file  # passes the intro manager's own checks
    validate_intro_file(card)


def test_clean_install_seeds_the_bundled_intro(tmp_path):
    """First run with an empty library: the bundled card becomes the active intro (not "Intro: none")."""
    from core.intros import IntroLibrary
    from ui.main_window import default_intro_card
    card = default_intro_card()
    assert card is not None and card.name == "intro_card.mp4"
    lib = IntroLibrary(tmp_path / "Intros")
    lib.add(card, "Original intro", make_active=True)
    assert lib.active_path() is not None and lib.active_path().exists()


@pytest.fixture
def window(tmp_path, monkeypatch):
    pytest.importorskip("PyQt6")
    from PyQt6.QtCore import QSettings
    from PyQt6.QtWidgets import QApplication
    import ui.main_window as mw
    app = QApplication.instance() or QApplication([])   # keep a reference or Qt tears it down
    w = mw.MainWindow()
    w.settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    yield w, mw
    w.close()
    app.processEvents()


def test_chime_is_off_by_default(window, monkeypatch):
    """A beep through laptop speakers ends up in the take: only chime when she turned it on."""
    from types import SimpleNamespace
    w, mw = window
    played = []
    monkeypatch.setattr(mw, "chime", lambda freqs=(): played.append(freqs))
    s = SimpleNamespace(chime=True, light="clear")
    w._maybe_chime(s)
    assert played == [] and s.chime is False
    w.settings.setValue("chime_on", "true")
    w._maybe_chime(SimpleNamespace(chime=True, light="amber"))
    assert played == [(520.0,)]


def test_worker_failure_is_logged_with_traceback(window, monkeypatch, caplog):
    w, mw = window
    monkeypatch.setattr(mw.QMessageBox, "exec", lambda self: 0)
    tb = ('Traceback (most recent call last):\n  File "core/session.py", line 70, in load_mono_48k\n'
          "OSError: [WinError 6] The handle is invalid\n")
    with caplog.at_level("ERROR"):
        w._error(tb)
    assert any("worker failed" in r.getMessage() and "WinError 6" in r.getMessage() for r in caplog.records)
