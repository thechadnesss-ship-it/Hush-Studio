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


# --- Quick vs full suite -------------------------------------------------------------------
# The full suite takes ~12 min (measured 2026-11-11, 285 tests). The Narthen quick check runs
# `pytest -m "not slow"` (~1-2 min); run plain `pytest` before merging. Slow = anything over
# ~5 s, plus all of test_mux_speed.py (its session fixture encodes 30 min of audio first).
SLOW_MODULES = {"test_mux_speed.py"}
SLOW_TESTS = {
    "test_capture.py::test_pause_discards_audio_and_take_time",
    "test_capture.py::test_records_raw_flac_and_sees_the_plane",
    "test_capture.py::test_background_deepfilternet_segments_cover_the_take",
    "test_capture.py::test_listen_back_returns_last_seconds_cleaned",
    "test_capture.py::test_capture_report_counts_clipping_and_is_saved",
    "test_ui.py::test_window_reflects_live_guard",
    "test_ui.py::test_editor_hand_edits_undo_automatic_tools_and_raw_untouched",
    "test_ui.py::test_mic_clipping_warning",
    "test_ui.py::test_clean_take_shows_no_mic_warning",
    "test_pipeline.py::test_encode_free_tail_ends_in_silence_at_exact_length",
    "test_pipeline.py::test_session_end_to_end_with_a_plane",
    "test_pipeline.py::test_parallel_encode_is_one_valid_session",
    "test_pipeline.py::test_silence_split_points_land_in_silence",
    "test_denoise.py::test_deepfilternet_keeps_word_endings_before_gated_silence",
    "test_denoise.py::test_deepfilternet_removes_a_plane_from_real_voice",
    "test_denoise.py::test_deepfilternet_mid_word_split_matches_a_whole_file_run",
    "test_denoise.py::test_deepfilternet_input_above_full_scale_is_not_clipped",
    "test_polish.py::test_fast_on_a_real_33_min_take",
    "test_plane_ear.py::test_real_take_has_no_false_alarms",
    "test_plane_ear.py::test_sustained_vowels_never_trigger",
    "test_plane_ear.py::test_chunk_size_does_not_matter",
    "test_edl_splicer.py::test_end_to_end_plane_is_removed",
    "test_auto_edit.py::test_markers_found_without_a_template",
    "test_auto_edit.py::test_saved_template_reused",
    "test_auto_edit.py::test_real_take_reproduces_her_edit",
    "test_auto_edit.py::test_shaped_output_matches_sams_style",
    "test_loudness.py::test_sams_edited_take_needs_almost_no_change",
    "test_loudness.py::test_limiter_is_fast_on_a_long_take",
    "test_loudness.py::test_encoded_session_keeps_loudness",
    "test_editing.py::test_remove_plane_noise_on_part_and_all",
    "test_debug_bundle.py::test_bundle_reads_like_a_session",
    "test_sky_radar_guard.py::test_no_all_clear_while_another_plane_is_inbound",
    "test_sky_radar_guard.py::test_guard_red_then_clear_with_chimes",
}


def pytest_addoption(parser):
    from progress_plugin import pytest_addoption as add
    add(parser)


def pytest_configure(config):
    if config.getoption("--ui", default=False):
        from progress_plugin import ProgressUI
        config.pluginmanager.register(ProgressUI(), "progress-ui")


def pytest_collection_modifyitems(config, items):
    slow = pytest.mark.slow
    for item in items:
        path = Path(str(item.fspath)).name
        base = item.nodeid.split("/")[-1].split("[")[0]
        if path in SLOW_MODULES or base in SLOW_TESTS:
            item.add_marker(slow)
