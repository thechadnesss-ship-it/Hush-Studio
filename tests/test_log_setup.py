import logging
import subprocess
import sys
import textwrap

from core import log_setup


def _read(p):
    for h in logging.getLogger("hush").handlers:
        h.flush()
    return p.read_text("utf-8")


def test_log_lines_timestamped_and_rotating(tmp_path):
    p = log_setup.setup_logging(tmp_path, max_bytes=500, backups=2)
    for i in range(60):
        log_setup.get_logger("capture").info("line %d", i)
    assert (tmp_path / "hush-studio.log.1").exists()
    assert len(list(tmp_path.glob("hush-studio.log*"))) <= 3
    import re
    assert re.match(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d+ INFO\s+hush\.capture", p.read_text("utf-8").splitlines()[0])


def test_secrets_redacted(tmp_path):
    p = log_setup.setup_logging(tmp_path)
    log = log_setup.get_logger("upload")
    log.info("Authorization: Bearer ya29.A0AbCdEf123 and url ?upload_id=SECRETID&x=1")
    log.info('{"refresh_token": "1//0gABCDEFGHIJKLMNOPQRSTUV", "client_secret": "GOCSPX-xyz"}')
    try:
        raise ValueError("access_token=ya29.leak")
    except ValueError:
        log.exception("failed")
    txt = _read(p)
    for leak in ("ya29", "SECRETID", "1//0g", "GOCSPX", "leak"):
        assert leak not in txt
    assert "[REDACTED]" in txt and "Traceback" in txt


def test_startup_has_versions(tmp_path):
    p = log_setup.setup_logging(tmp_path)
    log_setup.log_startup("6.1")
    txt = _read(p)
    for k in ("app:", "windows:", "python:", "qt:", "ffmpeg: 6.1", "audio:"):
        assert k in txt


def test_crash_writes_traceback_and_shows_message(tmp_path):
    script = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, r"{log_setup.Path(__file__).resolve().parents[1]}")
        from core import log_setup
        log_setup.setup_logging(r"{tmp_path}")
        shown = []
        log_setup.install_crash_handler(shown.append)
        sys.excepthook(RuntimeError, RuntimeError("induced crash"), None)
        assert shown and "safe" in shown[0]
    """)
    r = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    txt = (tmp_path / "hush-studio.log").read_text("utf-8")
    assert "CRITICAL" in txt and "induced crash" in txt
