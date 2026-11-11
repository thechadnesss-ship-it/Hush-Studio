"""Intro manager (T15): validation, library, and cache rebuild when the active intro changes."""
import subprocess

import pytest

from core import ffmpeg_tools as ff
from core.asset_cache import AssetCache
from core.config import load_render_config
from core.intros import IntroError, IntroLibrary


def make_video(path, color="red", size="1920x1080", secs=2, fps=30):
    ff.run([ff.ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
            f"color=c={color}:s={size}:r={fps}", "-t", str(secs), "-pix_fmt", "yuv420p", str(path)])
    return path


@pytest.fixture
def lib(tmp_path):
    return IntroLibrary(tmp_path / "Intros")


def test_add_set_active_and_keep_old(lib, tmp_path):
    a = lib.add(make_video(tmp_path / "a.mp4"), "First")
    b = lib.add(make_video(tmp_path / "b.mp4", "blue"), "Second")
    assert lib.active_id == a.id                       # first one becomes active
    lib.set_active(b.id)
    again = IntroLibrary(lib.root)                     # persists
    assert again.active_id == b.id and [i.name for i in again.list()] == ["First", "Second"]
    assert again.path(a).exists()                      # old one is kept
    with pytest.raises(IntroError):
        again.remove(b.id)                             # can't delete the active one
    again.remove(a.id)
    assert not (lib.root / a.id).exists()


def test_image_becomes_card(lib, tmp_path):
    png = tmp_path / "card.png"
    ff.run([ff.ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
            "color=c=purple:s=1920x1080", "-frames:v", "1", str(png)])
    i = lib.add(png, "Picture card")
    assert i.kind == "image" and 9 < ff.duration(lib.path(i)) < 11


@pytest.mark.parametrize("kw,needle", [
    (dict(size="640x360"), "at least"),
    (dict(size="1920x1440"), "widescreen"),
    (dict(secs=0.4), "long"),
])
def test_bad_video_rejected_with_message(lib, tmp_path, kw, needle):
    f = make_video(tmp_path / "bad.mp4", **kw)
    with pytest.raises(IntroError, match=needle):
        lib.add(f)
    assert lib.list() == []


def test_not_a_video_rejected(lib, tmp_path):
    f = tmp_path / "x.mp4"
    f.write_bytes(b"not a video")
    with pytest.raises(IntroError, match="couldn't be opened"):
        lib.add(f)
    t = tmp_path / "x.txt"
    t.write_text("hi")
    with pytest.raises(IntroError, match="isn't a video"):
        lib.add(t)


def test_cache_rebuilds_on_intro_change_and_old_stays(lib, tmp_path):
    cache = AssetCache(load_render_config().with_(), tmp_path / "cache")
    a = lib.add(make_video(tmp_path / "a.mp4"), "A")
    b = lib.add(make_video(tmp_path / "b.mp4", "blue"), "B")
    lib.set_active(a.id)
    ca = cache.intro_video(lib.active_path())
    lib.set_active(b.id)
    cb = cache.intro_video(lib.active_path())
    assert ca != cb and ca.exists() and cb.exists()    # new intro built, old cache entry still there
    lib.set_active(a.id)
    assert cache.intro_video(lib.active_path()) == ca  # switching back reuses the cache
    assert cache.black_loop().exists()
