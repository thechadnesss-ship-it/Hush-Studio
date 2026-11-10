from pathlib import Path

import pytest

from core import ffmpeg_tools as ff
from core.asset_cache import AssetCache
from core.config import RenderConfig


@pytest.fixture(scope="session")
def cfg() -> RenderConfig:
    # Explicit defaults so tests don't depend on a user's edited render.toml.
    return RenderConfig()


@pytest.fixture(scope="session")
def media(tmp_path_factory, cfg):
    """Synthetic 30 min session + 65 s intro + black loop, encoded once per test run."""
    d: Path = tmp_path_factory.mktemp("media")
    ff.check_ffmpeg_version()

    session = d / "session.m4a"
    ff.run([ff.ffmpeg_bin(), "-y", "-v", "error",
            # Noise, not a pure sine: FFmpeg's "fast" AAC coder is pathologically slow on pure
            # tones (~5x), and room tone + voice behaves like noise.
            "-f", "lavfi", "-i", "anoisesrc=color=brown:r=48000:d=1800:a=0.05",
            *ff.aac_encode_args(cfg.audio_settings()), str(session)])

    intro_wav = d / "intro.wav"
    ff.run([ff.ffmpeg_bin(), "-y", "-v", "error",
            "-f", "lavfi", "-i", "sine=f=440:r=48000:d=65", "-ac", "1", str(intro_wav)])

    cache = AssetCache(cfg, d / "cache")
    return {
        "dir": d,
        "cache": cache,
        "session": session,
        "intro_wav": intro_wav,
        "intro": cache.intro(intro_wav),
        "black": cache.black_loop(),
    }
