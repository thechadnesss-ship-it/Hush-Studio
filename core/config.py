"""Typed loader for config/render.toml."""
from __future__ import annotations

import os
import tomllib
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# Where recordings, caches and app data go. The USB launcher points this at the user's PC
# (Videos\Hush Studio) so nothing heavy is written to a slow flash drive.
DATA_ROOT = Path(os.environ["HUSH_DATA_DIR"]) if os.environ.get("HUSH_DATA_DIR") else PROJECT_ROOT
DEFAULT_RENDER_TOML = PROJECT_ROOT / "config" / "render.toml"

FIT_POLICIES = ("nearest", "at_least", "exact")
TARGET_HOURS_OPTIONS = (1, 3, 4, 6, 8)


@dataclass(frozen=True)
class RenderConfig:
    target_hours: float = 8
    fit_policy: str = "exact"
    resolution: str = "1920x1080"
    loop_fps: int = 30
    black_frame_s: float = 1.0
    black_loop_seconds: int = 60
    audio_codec: str = "aac"
    audio_bitrate: str = "128k"
    audio_sample_rate: int = 48000
    audio_channels: int = 1
    aac_coder: str = "fast"
    seam_fade_ms: int = 1500
    polish: bool = False             # Studio polish (core/polish.py), chosen per session in review
    target_lufs: float = -21.0

    def __post_init__(self) -> None:
        if self.fit_policy not in FIT_POLICIES:
            raise ValueError(f"fit_policy must be one of {FIT_POLICIES}, got {self.fit_policy!r}")
        if self.target_hours <= 0:
            raise ValueError("target_hours must be > 0")
        w, _, h = self.resolution.partition("x")
        if not (w.isdigit() and h.isdigit()):
            raise ValueError(f"resolution must look like 1920x1080, got {self.resolution!r}")

    @property
    def height(self) -> int:
        return int(self.resolution.split("x")[1])

    @property
    def seam_fade_s(self) -> float:
        return self.seam_fade_ms / 1000.0

    def audio_settings(self) -> dict:
        """The settings every concat audio segment must share."""
        return {
            "codec": self.audio_codec,
            "bitrate": self.audio_bitrate,
            "sample_rate": self.audio_sample_rate,
            "channels": self.audio_channels,
            "aac_coder": self.aac_coder,
        }

    def with_(self, **changes) -> "RenderConfig":
        return replace(self, **changes)

    def as_dict(self) -> dict:
        return asdict(self)


def load_render_config(path: str | Path = DEFAULT_RENDER_TOML) -> RenderConfig:
    path = Path(path)
    if not path.exists():
        return RenderConfig()
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    known = {f.name for f in fields(RenderConfig)}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"Unknown keys in {path.name}: {sorted(unknown)}")
    return RenderConfig(**data)
