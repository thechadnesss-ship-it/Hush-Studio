"""Headless check that an installed Hush Studio has everything it needs (T21).

`HushStudio.exe --self-test <report.json>` (or `python -m core.selftest <report.json>`) runs,
without a window: the bundled FFmpeg/ffprobe, deep-filter.exe (+ its VC runtime), libsndfile,
PortAudio, the font, the YouTube client's TLS setup, and the whole take pipeline on a synthetic
60 s take (import → noise removal → auto-edit → master → encode once → stitch a 1 h video).
Nothing touches Sam's takes: everything happens in a temporary folder. Exit code 0 = all passed.
"""
from __future__ import annotations

import json
import platform
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Callable

import numpy as np

SR = 48000


def _synthetic_take(seconds: float = 60.0, seed: int = 7) -> np.ndarray:
    """Phrases of a voiced buzz (harmonics + vibrato) separated by gated pauses."""
    rng = np.random.default_rng(seed)
    t = np.arange(int(seconds * SR)) / SR
    f0 = 180 + 12 * np.sin(2 * np.pi * 4.5 * t)
    phase = 2 * np.pi * np.cumsum(f0) / SR
    voice = sum(np.sin(k * phase) / k for k in range(1, 12)) * 0.08
    gate = (np.floor(t / 2.5) % 2 == 0).astype(np.float32)          # 2.5 s phrase, 2.5 s pause
    x = voice * gate + rng.normal(0, 10 ** (-60 / 20), t.size)
    return x.astype(np.float32)


def _check(name: str, fn: Callable[[], object], results: list[dict]) -> object:
    t0 = time.perf_counter()
    try:
        detail = fn()
        results.append({"check": name, "ok": True, "s": round(time.perf_counter() - t0, 2),
                        "detail": "" if detail is None else str(detail)})
        return detail
    except Exception as e:
        results.append({"check": name, "ok": False, "s": round(time.perf_counter() - t0, 2),
                        "detail": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()[-2000:]})
        return None


def run_self_test(work: Path | None = None) -> dict:
    from . import ffmpeg_tools as ff
    from .config import DATA_ROOT, FROZEN, PROJECT_ROOT, load_render_config

    results: list[dict] = []
    tmp_owner = None
    if work is None:
        tmp_owner = tempfile.TemporaryDirectory(prefix="hush-selftest-")
        work = Path(tmp_owner.name)
    work.mkdir(parents=True, exist_ok=True)

    _check("ffmpeg", lambda: f"{ff.check_ffmpeg_version()} at {ff.ffmpeg_bin()}", results)
    _check("ffprobe", lambda: ff.run([ff.ffprobe_bin(), "-hide_banner", "-version"]).stdout.splitlines()[0], results)
    _check("libx264 + aac", lambda: _encoders(ff), results)

    def soundfile_flac():
        import soundfile as sf
        p = work / "probe.flac"
        sf.write(p, np.zeros(SR, np.float32), SR, format="FLAC", subtype="PCM_24")
        return f"libsndfile {sf.__libsndfile_version__}"
    _check("soundfile (FLAC)", soundfile_flac, results)

    def portaudio():
        import sounddevice as sd
        ins = [d["name"] for d in sd.query_devices() if d["max_input_channels"] > 0]
        return f"{sd.get_portaudio_version()[1]}; {len(ins)} input device(s)"
    _check("sounddevice (PortAudio)", portaudio, results)

    def deepfilter():
        from .denoise import deepfilter_exe, deepfilter_file
        import soundfile as sf
        exe = deepfilter_exe()
        if exe is None:
            raise FileNotFoundError("deep-filter.exe is not bundled (the built-in noise gate will be used)")
        src, dst = work / "df_in.wav", work / "df_out.wav"
        sf.write(src, _synthetic_take(3.0) + np.float32(10 ** (-65 / 20)), SR, subtype="FLOAT")
        deepfilter_file(src, dst)
        return f"{exe} → {sf.info(dst).duration:.2f} s"
    _check("deep-filter.exe", deepfilter, results)

    def font():
        f = PROJECT_ROOT / "assets" / "fonts" / "Montserrat.ttf"
        if not f.is_file():
            raise FileNotFoundError(f)
        return f"{f.stat().st_size} bytes"
    _check("Montserrat font", font, results)

    def tls():
        import ssl
        import certifi
        import google.auth.transport.requests  # noqa: F401
        import google_auth_oauthlib.flow  # noqa: F401
        from . import youtube_publisher  # noqa: F401
        ctx = ssl.create_default_context(cafile=certifi.where())
        return f"{ssl.OPENSSL_VERSION}; CA bundle {Path(certifi.where()).name}, {len(ctx.get_ca_certs()) or 'n/a'} roots"
    _check("YouTube client (TLS, google-auth)", tls, results)

    def pipeline():
        import soundfile as sf
        from .asset_cache import AssetCache
        from .denoise import SpectralGate
        from .session import Session, denoise, finish, import_take, prepare
        src = work / "synthetic_take.wav"
        sf.write(src, _synthetic_take(), SR, subtype="FLOAT")
        cfg = load_render_config().with_(target_hours=1)
        session = Session(work / "Takes" / "selftest")
        raw = import_take(src, session)
        clean, backend = denoise(session, raw, denoiser=SpectralGate())
        edl = prepare(session, raw, clean, render_cfg=cfg)
        assets = AssetCache(cfg, work / "cache")
        plan, out, _, tm = finish(session, cfg, assets, intro_card=None, clean=clean, edl=edl)
        dur = ff.duration(out)
        if abs(dur - 3600) > 0.5:
            raise AssertionError(f"video is {dur:.2f} s, expected 3600 s")
        return f"{backend}; {plan.summary()}; {dur:.2f} s video; steps {tm.steps}"
    _check("record → render pipeline (1 h video)", pipeline, results)

    report = {
        "ok": all(r["ok"] for r in results if r["check"] != "deep-filter.exe" or FROZEN),
        "frozen": FROZEN,
        "python": sys.version.split()[0],
        "windows": platform.platform(),
        "program_files": str(PROJECT_ROOT),
        "data_folder": str(DATA_ROOT),
        "results": results,
    }
    if tmp_owner is not None:
        tmp_owner.cleanup()
    return report


def _encoders(ff) -> str:
    out = ff.run([ff.ffmpeg_bin(), "-hide_banner", "-encoders"]).stdout
    missing = [e for e in ("libx264", " aac ") if e not in out]
    if missing:
        raise RuntimeError(f"FFmpeg lacks encoder(s): {missing}")
    return "libx264, aac"


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    out = Path(argv[0]) if argv else None
    report = run_self_test()
    text = json.dumps(report, indent=2)
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
    if sys.stdout is not None:
        print(text)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
