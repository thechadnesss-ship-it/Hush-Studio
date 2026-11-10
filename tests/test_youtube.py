"""YouTube publisher against a fake resumable-upload server (no Google account needed)."""
from datetime import datetime, timedelta, timezone

import pytest
import requests

from core.youtube_publisher import (CHUNK, ResumableUpload, UploadError, VideoMeta, build_meta, load_template,
                                    next_publish_time)

KB256 = 256 * 1024


class Resp:
    def __init__(self, code, headers=None, body=None):
        self.status_code, self.headers, self._body = code, headers or {}, body or {}
        self.text = str(body)

    def json(self):
        return self._body


class FakeYouTube:
    """Implements the resumable protocol: POST → Location; PUT chunk → 308 Range / 200 video."""

    def __init__(self, privacy="private", fail_puts=(), drop_after=None, expire_first_session=False):
        self.data = bytearray()
        self.privacy = privacy
        self.fail_puts = set(fail_puts)       # indices of PUTs that 503
        self.drop_after = drop_after          # raise ConnectionError on this PUT index (after storing)
        self.expire_first = expire_first_session
        self.puts = 0
        self.sessions = 0
        self.meta = None
        self.size = None

    def post(self, url, params=None, json=None, headers=None, timeout=None):
        self.sessions += 1
        self.meta, self.size = json, int(headers["X-Upload-Content-Length"])
        self.data = bytearray()
        return Resp(200, {"Location": f"https://upload/session{self.sessions}"})

    def put(self, url, data=None, headers=None, timeout=None):
        i = self.puts
        self.puts += 1
        if self.expire_first and url.endswith("session1"):
            return Resp(404, body={"error": {"message": "session expired"}})
        rng = headers["Content-Range"]
        if rng.startswith("bytes */"):
            return self._status()
        if i in self.fail_puts:
            return Resp(503, body={"error": {"message": "backend error"}})
        start = int(rng.split(" ")[1].split("-")[0])
        assert start == len(self.data), "client must resume exactly where the server is"
        self.data += data
        if self.drop_after == i:
            raise requests.ConnectionError("wifi dropped")
        return self._status()

    def _status(self):
        if len(self.data) >= self.size:
            return Resp(201, body={"id": "abc123XYZ", "status": {"privacyStatus": self.privacy}})
        return Resp(308, {"Range": f"bytes=0-{len(self.data) - 1}"} if self.data else {})


@pytest.fixture
def video(tmp_path):
    p = tmp_path / "output_8h.mp4"
    p.write_bytes(bytes(range(256)) * (KB256 * 10 // 256 + 123))      # ~2.5 MB, not a chunk multiple
    return p


def meta(**kw):
    return VideoMeta(title="Test | Deep Sleep Hypnosis (Black Screen)", description="d", tags=["sleep"], **kw)


def run(fake, video, chunk=KB256 * 4, **kw):
    return ResumableUpload(fake, video, meta(**kw), chunk=chunk, sleep=lambda s: None).run()


def test_uploads_in_chunks_byte_exact(video):
    yt = FakeYouTube()
    res = run(yt, video)
    assert bytes(yt.data) == video.read_bytes()
    assert res.video_id == "abc123XYZ" and res.url == "https://youtu.be/abc123XYZ"
    assert yt.meta["snippet"]["categoryId"] == "10"
    assert not video.with_suffix(".mp4.upload.json").exists()          # state cleaned up


def test_5xx_and_dropped_connection_are_retried_from_server_offset(video):
    yt = FakeYouTube(fail_puts={1, 2}, drop_after=4)
    run(yt, video)
    assert bytes(yt.data) == video.read_bytes()


def test_resumes_after_app_restart(video):
    import threading
    yt = FakeYouTube()
    stop = threading.Event()
    up = ResumableUpload(yt, video, meta(), chunk=KB256 * 4, sleep=lambda s: None)
    calls = []
    with pytest.raises(InterruptedError):
        up.run(on_progress=lambda f: (calls.append(f), stop.set() if len(calls) == 2 else None), cancel=stop)
    assert video.with_suffix(".mp4.upload.json").exists() and 0 < len(yt.data) < video.stat().st_size
    # new process, same file: picks up the saved session and continues from the server's offset
    res = ResumableUpload(yt, video, meta(), chunk=KB256 * 4, sleep=lambda s: None).run()
    assert res.resumed and yt.sessions == 1
    assert bytes(yt.data) == video.read_bytes()


def test_expired_session_restarts_cleanly(video):
    yt = FakeYouTube(expire_first_session=True)
    run(yt, video)
    assert yt.sessions == 2 and bytes(yt.data) == video.read_bytes()


def test_quota_error_is_explained(video):
    class Quota(FakeYouTube):
        def post(self, *a, **k):
            return Resp(403, body={"error": {"message": "quota", "errors": [{"reason": "quotaExceeded"}]}})
    with pytest.raises(UploadError, match="daily upload limit"):
        run(Quota(), video)


def test_unverified_project_lock_detected(video):
    res = run(FakeYouTube(privacy="private"), video, privacy="public")
    assert res.locked_private
    assert not run(FakeYouTube(privacy="public"), video, privacy="public").locked_private


def test_chunk_size_rule():
    assert CHUNK % KB256 == 0
    with pytest.raises(ValueError):
        ResumableUpload(FakeYouTube(), __file__, meta(), chunk=1000)


# ----------------------------------------------------------------- metadata

def test_template_matches_channel():
    m = build_meta("You'll Forget You Were Ever Awake… 😴", 8, schedule=False)
    assert m.title == "You'll Forget You Were Ever Awake… 😴 | Deep Sleep Hypnosis (Black Screen)"
    assert "samkatz.store" in m.description and "sleep hypnosis" in m.tags and m.category_id == "10"
    m.validate()


def test_empty_hook_uses_default_and_limits_checked():
    m = build_meta("", 3)
    assert m.title.startswith(load_template()["default_hook"])
    with pytest.raises(ValueError, match="100"):
        build_meta("x" * 90, 8).validate()


def test_scheduled_publish_is_private_with_publish_at():
    now = datetime(2026, 11, 9, 20, 0, tzinfo=timezone(timedelta(hours=2)))
    t = next_publish_time("04:00", now)
    assert (t.day, t.hour) == (10, 4)
    m = VideoMeta(title="t", description="d", tags=[], privacy="public",
                  publish_at=datetime.now(timezone.utc) + timedelta(hours=5))
    b = m.body()
    assert b["status"]["privacyStatus"] == "private" and b["status"]["publishAt"].endswith("Z")
