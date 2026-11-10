"""Loop math + concat-list builder + stream-copy mux (Module D).

FINISH never re-encodes the long output. Audio = intro + session × N (+ tail),
video = black loop × M, joined with the concat demuxer and `-c copy`.
"""
from __future__ import annotations

import math
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from . import ffmpeg_tools as ff
from . import mp4_stitch
from .config import FIT_POLICIES, RenderConfig

MIN_TAKE_EXTRA_S = 10.0       # take must be > 2 × seam fade + this
SHORT_TAKE_WARN_S = 120.0     # below this, repeats get noticeable
MIN_TAIL_S = 1.0              # "exact" tails shorter than this are dropped


class TakeTooShortError(ValueError):
    pass


class CodecMismatchError(ff.FFmpegError):
    pass


# --------------------------------------------------------------------------- planning

@dataclass(frozen=True)
class RenderPlan:
    target_s: float
    policy: str
    intro_s: float
    session_s: float
    loops: int                 # N: session repeats
    tail_s: float              # 0 unless policy == "exact"
    black_loop_s: float
    black_copies: int          # M
    total_s: float
    warnings: tuple[str, ...] = field(default=())
    video_head_s: float = 0.0  # silent intro card at the start of the video track (audio plays under it)

    @property
    def error_s(self) -> float:
        """Signed difference from the target (positive = longer than target)."""
        return self.total_s - self.target_s

    def summary(self) -> str:
        """e.g. 'Take 34:12 · intro 1:05 · ×14 → 7h 59m 53s' or 'Take 33:13 · card 0:30 · ×14 + …'"""
        s = f"Take {fmt_mmss(self.session_s)}"
        if self.intro_s > 0.5:
            s += f" · intro {fmt_mmss(self.intro_s)}"
        if self.video_head_s > 0.5:
            s += f" · card {fmt_mmss(self.video_head_s)}"
        s += f" · ×{self.loops}"
        if self.tail_s:
            s += f" + {fmt_mmss(self.tail_s)} tail"
        return f"{s} → {fmt_hms(self.total_s)}"


def fmt_mmss(sec: float) -> str:
    sec = int(round(sec))
    m, s = divmod(sec, 60)
    return f"{m}:{s:02d}"


def fmt_hms(sec: float) -> str:
    sec = int(round(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m:02d}m {s:02d}s"


def plan_render(
    session_s: float,
    intro_s: float,
    target_hours: float,
    policy: str = "nearest",
    black_loop_s: float = 60.0,
    seam_fade_s: float = 1.5,
    video_head_s: float = 0.0,
) -> RenderPlan:
    if policy not in FIT_POLICIES:
        raise ValueError(f"Unknown fit policy {policy!r}")
    min_take = 2 * seam_fade_s + MIN_TAKE_EXTRA_S
    if session_s < min_take:
        raise TakeTooShortError(
            f"This take is only {session_s:.1f} s. It needs at least {min_take:.0f} s to loop smoothly."
        )
    if black_loop_s <= 0:
        raise ValueError("black_loop_s must be > 0")

    warnings: list[str] = []
    if session_s < SHORT_TAKE_WARN_S:
        warnings.append(
            f"Short take ({fmt_mmss(session_s)}): it will repeat many times, and listeners may notice."
        )

    target_s = float(target_hours) * 3600.0
    remaining = target_s - intro_s
    tail_s = 0.0

    if remaining <= session_s:
        loops = 1
    else:
        ratio = remaining / session_s
        if policy == "nearest":
            loops = math.floor(ratio + 0.5)          # round half up (not banker's)
        elif policy == "at_least":
            loops = math.ceil(ratio - 1e-9)          # tolerate float noise on exact fits
        else:  # exact
            loops = math.floor(ratio + 1e-9)
            tail_s = remaining - loops * session_s
            if tail_s < MIN_TAIL_S:
                tail_s = 0.0
        loops = max(1, loops)

    total_s = intro_s + loops * session_s + tail_s
    black_copies = max(0, math.ceil((total_s - video_head_s) / black_loop_s - 1e-9))
    return RenderPlan(
        target_s=target_s, policy=policy, intro_s=intro_s, session_s=session_s,
        loops=loops, tail_s=tail_s, black_loop_s=black_loop_s,
        black_copies=black_copies, total_s=total_s, warnings=tuple(warnings),
        video_head_s=video_head_s,
    )


def plan_from_files(cfg: RenderConfig, session_m4a: Path, intro_m4a: Path | None, black_loop: Path,
                    intro_video: Path | None = None) -> RenderPlan:
    """Plan from the encoded files, using the durations the stitched output will actually
    have: every segment occupies its full packet length (priming + padding included), except
    the first audio segment, whose priming is skipped by the edit list. Sample-accurate.

    `intro_m4a` = optional intro *audio* (None for Sam's channel: her intro is a silent card).
    `intro_video` = optional silent intro card at the start of the video track."""
    try:
        session_media, session_priming = mp4_stitch.stitch_durations(Path(session_m4a))
        black_s = mp4_stitch.video_loop_duration(Path(black_loop))
        if intro_m4a is not None:
            intro_media, intro_priming = mp4_stitch.stitch_durations(Path(intro_m4a))
            intro_s = intro_media - intro_priming
        else:
            intro_s = -session_priming          # the first session copy loses its priming instead
        head_s = mp4_stitch.video_loop_duration(Path(intro_video)) if intro_video else 0.0
        session_s = session_media
    except mp4_stitch.UnsupportedInput:
        intro_s = ff.duration(intro_m4a) if intro_m4a is not None else 0.0
        session_s, black_s = ff.duration(session_m4a), ff.duration(black_loop)
        head_s = ff.duration(intro_video) if intro_video else 0.0
    return plan_render(
        session_s=session_s,
        intro_s=intro_s,
        target_hours=cfg.target_hours,
        policy=cfg.fit_policy,
        black_loop_s=black_s,
        seam_fade_s=cfg.seam_fade_s,
        video_head_s=head_s,
    )


# --------------------------------------------------------------------------- tail ("exact")

def build_tail(source: Path, tail_s: float, dst: Path, cfg: RenderConfig) -> Path:
    """Encode the first `tail_s` seconds of the take with a fade-out. Only ever ≤ 1 take
    of audio, so this costs seconds. Prefer the mastered WAV as `source` (no generation loss)."""
    fade = min(cfg.seam_fade_s, tail_s / 2)
    return ff.encode_audio(
        source, dst, cfg.audio_settings(),
        extra_input_args=["-t", f"{tail_s:.6f}"],
        filters=f"afade=t=out:st={tail_s - fade:.6f}:d={fade:.6f}",
    )


def plan_tail_copy(session: Path, tail_s: float, silence: Path,
                   min_silence_s: float = 1.0, end_quiet_s: float = 1.0) -> list[tuple[Path, int]] | None:
    """Encode-free `exact` tail: the session's first AAC frames up to the last pause before
    `tail_s`, then frames of pre-encoded silence to make up the exact length. The video thus
    ends inside a pause (no fade needed, no word cut off) at exactly the target length.
    Returns stitcher pieces [(path, n_frames), ...] or None (no pause found → encode a tail)."""
    import subprocess
    import numpy as np
    n_sess, fdur, priming, ts = mp4_stitch.audio_frame_info(session)
    sr = ts
    r = subprocess.run([ff.ffmpeg_bin(), "-v", "error", "-t", f"{tail_s:.3f}", "-i", str(session),
                        "-ac", "1", "-f", "f32le", "-"], capture_output=True)
    if r.returncode != 0:
        return None
    y = np.frombuffer(r.stdout, np.float32)
    quiet = np.concatenate(([0], (np.abs(y) < 1e-4).astype(np.int8), [0]))
    d = np.diff(quiet)
    starts, ends = np.flatnonzero(d == 1), np.flatnonzero(d == -1)
    # prefer a real sentence break that leaves the last `end_quiet_s` of the video silent;
    # fall back to any short pause
    early = starts < (tail_s - end_quiet_s) * sr
    ok = (ends - starts >= int(min_silence_s * sr)) & early
    if not ok.any():
        ok = (ends - starts >= int(0.3 * sr)) & (starts < (tail_s - 0.1) * sr)
    if not ok.any():
        return None
    s0, e0 = int(starts[ok][-1]), int(ends[ok][-1])
    cut = min(s0 + int(0.15 * sr), (s0 + e0) // 2, int((tail_s - 0.1) * sr))   # a little into the pause
    frames = min(n_sess, -(-(cut + priming) // fdur))
    pad = tail_s * ts - frames * fdur
    pieces: list[tuple[Path, int]] = [(session, frames)]
    n_sil, sfd, _, _ = mp4_stitch.audio_frame_info(silence)
    need = max(0, -(-int(np.ceil(pad)) // sfd)) + 1                      # +1: presentation clips it
    while need > 0:
        k = min(need, n_sil)
        pieces.append((silence, k))
        need -= k
    return pieces


# --------------------------------------------------------------------------- concat + mux

def _concat_line(path: Path) -> str:
    # Absolute POSIX-style path; escape single quotes per the concat demuxer's rules.
    p = Path(path).resolve().as_posix().replace("'", "'\\''")
    return f"file '{p}'\n"


def write_concat_list(dst: Path, files: Sequence[Path]) -> Path:
    with open(dst, "w", encoding="utf-8", newline="\n") as f:
        f.write("ffconcat version 1.0\n")
        for p in files:
            f.write(_concat_line(p))
    return dst


def check_audio_compatible(cfg: RenderConfig, *segments: Path) -> None:
    want = ff.expected_audio_params(cfg.audio_settings())
    for seg in segments:
        got = ff.audio_params(seg)
        if got != want:
            raise CodecMismatchError(
                f"{Path(seg).name} has {got}, expected {want}. Concat-copy needs identical params."
            )


def mux(
    plan: RenderPlan,
    cfg: RenderConfig,
    *,
    intro: Path | None = None,
    session: Path,
    black_loop: Path,
    out_path: Path,
    intro_video: Path | None = None,
    tail: Path | None = None,
    tail_pieces: list[tuple[Path, int]] | None = None,
    work_dir: Path | None = None,
    on_progress: ff.ProgressFn | None = None,
    cancel: threading.Event | None = None,
) -> str:
    """Zero encoding. Primary path: native byte-copy stitcher (disk-speed). Falls back to
    the FFmpeg concat demuxer if an input isn't in the simple layout the stitcher supports.
    The `exact` tail is either `tail_pieces` (frame-prefix copies, see plan_tail_copy) or an
    encoded `tail` file. Writes `out_path` atomically. Returns "stitch" or "ffmpeg"."""
    if plan.tail_s and tail is None and not tail_pieces:
        raise ValueError("Plan has a tail but no tail was given (see plan_tail_copy / build_tail).")
    out_path = Path(out_path)
    work_dir = Path(work_dir or out_path.parent)
    work_dir.mkdir(parents=True, exist_ok=True)

    head = [intro] if intro is not None else []
    if plan.tail_s:
        end = list(tail_pieces) if tail_pieces else [tail]
    else:
        end = []
    end_files = [p if not isinstance(p, tuple) else p[0] for p in end]
    check_audio_compatible(cfg, *head, session, *dict.fromkeys(end_files))
    audio_files = head + [session] * plan.loops + end
    video_head = [intro_video] if intro_video is not None else []

    try:
        mp4_stitch.stitch(out_path, audio_files, black_loop, plan.total_s, video_head=video_head,
                          loop_frame_s=cfg.black_frame_s or None,
                          on_progress=on_progress, cancel=cancel)
        return "stitch"
    except mp4_stitch.UnsupportedInput:
        pass
    if any(isinstance(p, tuple) for p in audio_files):     # FFmpeg concat can't do frame prefixes
        tail = build_tail(session, plan.tail_s, out_path.parent / "tail.m4a", cfg)
        audio_files = head + [session] * plan.loops + [tail]
    _mux_ffmpeg(plan, audio_files, black_loop, out_path, work_dir, on_progress, cancel, video_head)
    return "ffmpeg"


def _mux_ffmpeg(plan: RenderPlan, audio_files: list[Path], black_loop: Path, out_path: Path,
                work_dir: Path, on_progress, cancel, video_head: list[Path] | None = None) -> None:
    """Fallback: concat demuxer + -c copy. ~10 s for 8 h (per-packet overhead), still no encoding.
    Fragmented MP4 avoids the mov muxer's slow index finalisation (~23 s otherwise)."""
    video_files = list(video_head or []) + [black_loop] * plan.black_copies
    audio_list = write_concat_list(work_dir / "concat_audio.txt", audio_files)
    video_list = write_concat_list(work_dir / "concat_video.txt", video_files)

    tmp = out_path.with_name(out_path.name + ".part")
    try:
        ff.run_with_progress(
            [
                "-y",
                "-f", "concat", "-safe", "0", "-i", str(video_list),
                "-f", "concat", "-safe", "0", "-i", str(audio_list),
                "-map", "0:v:0", "-map", "1:a:0",
                "-c", "copy",
                "-t", f"{plan.total_s:.3f}",
                "-map_metadata", "-1",
                # no +faststart: it would rewrite the whole file
                "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
                "-f", "mp4", str(tmp),
            ],
            total_seconds=plan.total_s,
            on_progress=on_progress,
            cancel=cancel,
        )
        os.replace(tmp, out_path)
    finally:
        if tmp.exists():
            tmp.unlink()


def render(
    cfg: RenderConfig,
    *,
    intro: Path | None = None,
    session: Path,
    black_loop: Path,
    out_path: Path,
    intro_video: Path | None = None,
    silence: Path | None = None,
    tail_source: Path | None = None,
    on_progress: ff.ProgressFn | None = None,
    cancel: threading.Event | None = None,
) -> tuple[RenderPlan, Path]:
    """Plan → (`exact` tail: frame copy ending in a pause if `silence` is given, else an
    encoded fade-out tail) → mux. `tail_source` defaults to `session`."""
    plan = plan_from_files(cfg, session, intro, black_loop, intro_video)
    out_path = Path(out_path)
    tail, pieces = None, None
    if plan.tail_s:
        if silence is not None:
            pieces = plan_tail_copy(session, plan.tail_s, silence)
        if pieces is None:
            tail = build_tail(Path(tail_source or session), plan.tail_s, out_path.parent / "tail.m4a", cfg)
    mux(plan, cfg, intro=intro, session=session, black_loop=black_loop, out_path=out_path,
        intro_video=intro_video, tail=tail, tail_pieces=pieces, on_progress=on_progress, cancel=cancel)
    return plan, out_path


__all__ = [
    "RenderPlan", "TakeTooShortError", "CodecMismatchError", "plan_render", "plan_from_files",
    "build_tail", "write_concat_list", "check_audio_compatible", "mux", "render", "fmt_hms", "fmt_mmss",
]
