"""Always-on logging (T19): one rotating log in the user's app-data folder, a startup system
report, secret redaction and a global crash handler. Never log tokens, secrets or file contents."""
from __future__ import annotations

import logging
import logging.handlers
import os
import platform
import re
import sys
import threading
import traceback
from pathlib import Path
from typing import Callable

from .app_info import APP_NAME, APP_VERSION, SETTINGS_ORG

# Sub-loggers: hush.app, hush.capture, hush.denoise, hush.render, hush.upload
ROOT = "hush"
LOG_NAME = "hush-studio.log"
_FMT = "%(asctime)s %(levelname)-7s %(name)s [%(threadName)s] %(message)s"

_PATTERNS = [
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)((?:access_token|refresh_token|id_token|client_secret|api[_-]?key|authorization|"
                r"password|token|secret|code)\"?\s*[:=]\s*\"?)[^\s\"',&}]+"), r"\1[REDACTED]"),
    (re.compile(r"\bya29\.[A-Za-z0-9._-]+"), "[REDACTED]"),
    (re.compile(r"\b1//[A-Za-z0-9._-]{20,}"), "[REDACTED]"),
    (re.compile(r"(?i)([?&](?:access_token|key|upload_id|code|client_secret)=)[^&\s]+"), r"\1[REDACTED]"),
]


def redact(text: str) -> str:
    for pat, rep in _PATTERNS:
        text = pat.sub(rep, text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            msg = str(record.msg)
        record.msg, record.args = redact(msg), None
        if record.exc_info:                      # fold the traceback in so it's redacted too
            record.exc_text = redact("".join(traceback.format_exception(*record.exc_info)).rstrip())
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = redact(record.exc_text)
        return True


def log_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / SETTINGS_ORG / "logs"


def get_logger(part: str) -> logging.Logger:
    return logging.getLogger(f"{ROOT}.{part}")


def setup_logging(directory: Path | None = None, level: int = logging.INFO,
                  max_bytes: int = 2_000_000, backups: int = 5) -> Path:
    """Idempotent: replaces handlers it installed earlier. Returns the log file path."""
    d = Path(directory) if directory else log_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = d / LOG_NAME
    root = logging.getLogger(ROOT)
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()
    h = logging.handlers.RotatingFileHandler(path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8")
    h.setFormatter(logging.Formatter(_FMT))
    h.addFilter(RedactingFilter())
    root.addHandler(h)
    root.setLevel(level)
    root.propagate = False
    return path


def system_info(ffmpeg_version: str | None = None) -> dict[str, str]:
    info = {"app": f"{APP_NAME} {APP_VERSION}", "windows": platform.platform(),
            "python": platform.python_version(), "machine": platform.machine(),
            "cpus": str(os.cpu_count())}
    try:
        from PyQt6.QtCore import PYQT_VERSION_STR, qVersion
        info["qt"], info["pyqt"] = qVersion(), PYQT_VERSION_STR
    except Exception:
        info["qt"] = "unavailable"
    info["ffmpeg"] = ffmpeg_version or "unknown"
    return info


def audio_devices() -> list[str]:
    """Input devices with their host API, plus the default input; never raises."""
    try:
        import sounddevice as sd
        apis = sd.query_hostapis()
        lines = []
        for i, d in enumerate(sd.query_devices()):
            if d["max_input_channels"] > 0:
                lines.append(f"#{i} {d['name']} [{apis[d['hostapi']]['name']}] "
                             f"{d['max_input_channels']}ch {d['default_samplerate']:.0f}Hz")
        try:
            dd = sd.query_devices(kind="input")
            lines.append(f"default input: {dd['name']} [{apis[dd['hostapi']]['name']}]")
        except Exception:
            lines.append("default input: none")
        return lines
    except Exception as e:
        return [f"audio devices unavailable: {e}"]


def log_startup(ffmpeg_version: str | None = None) -> None:
    log = get_logger("app")
    log.info("=== %s starting ===", APP_NAME)
    for k, v in system_info(ffmpeg_version).items():
        log.info("%s: %s", k, v)
    for line in audio_devices():
        log.info("audio: %s", line)


def install_crash_handler(show: Callable[[str], None] | None = None) -> None:
    """Log every uncaught exception (main and worker threads) with its traceback, then show a
    friendly message via `show(text)`."""
    log = get_logger("app")
    friendly = (f"Sorry, {APP_NAME} hit a problem. Your recording is safe. "
                f"Details were saved in the log:\n{log_dir() / LOG_NAME}")

    def report(exc_type, exc, tb, where: str) -> None:
        log.critical("Uncaught exception in %s", where, exc_info=(exc_type, exc, tb))
        for h in log.parent.handlers:
            h.flush()
        if show:
            try:
                show(friendly)
            except Exception:
                pass

    def hook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        report(exc_type, exc, tb, "main thread")

    def thook(args):
        if args.exc_type is SystemExit:
            return
        report(args.exc_type, args.exc_value, args.exc_traceback,
               f"thread {args.thread.name if args.thread else '?'}")

    sys.excepthook = hook
    threading.excepthook = thook
