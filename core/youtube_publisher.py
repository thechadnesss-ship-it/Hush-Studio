"""YouTube publisher (CLAUDE.md Module E).

* OAuth 2 installed-app flow (browser sign-in once); token kept in data/youtube_token.json.
  Needs a "Desktop app" OAuth client from Google Cloud Console (YouTube Data API v3 enabled),
  saved as data/youtube_client_secret.json. The app walks Sam through it on first use.
* Resumable upload implemented directly on YouTube's documented protocol (POST to start a
  session → PUT 64 MB chunks → 308 "resume incomplete" with Range), so it can:
    - survive network drops (exponential backoff, then ask the server how much it has),
    - survive an app restart (session URI persisted next to the video; resumes mid-file),
    - restart cleanly if the session expired (404/410).
* Metadata from config/youtube_template.toml (the channel's title pattern, tags, Music category).
  Optional scheduled publishing (publishAt) at the channel's usual 04:00 local.

IMPORTANT (Google policy): videos uploaded through an *unverified* API project created after
28 July 2020 are locked to private, whatever privacy is requested. Sam can flip them to public
in YouTube Studio, or the project can pass Google's API audit. The app says this plainly.
"""
from __future__ import annotations

import json
import os
import random
import threading
import time
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from .config import DATA_ROOT, PROJECT_ROOT

DATA = DATA_ROOT / "data"
CLIENT_SECRET = DATA / "youtube_client_secret.json"
TOKEN = DATA / "youtube_token.json"
TEMPLATE = PROJECT_ROOT / "config" / "youtube_template.toml"
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
CHUNK = 64 * 1024 * 1024                    # must be a multiple of 256 KiB
UNVERIFIED_NOTE = ("Uploads from a new (unverified) Google API project are kept private by YouTube, "
                   "whatever privacy you choose. Flip it to Public in YouTube Studio, or apply for "
                   "Google's API audit to lift this.")


class UploadError(RuntimeError):
    pass


# =========================================================================== metadata

def load_template(path: Path = TEMPLATE) -> dict:
    return tomllib.loads(Path(path).read_text(encoding="utf-8"))


@dataclass
class VideoMeta:
    title: str
    description: str
    tags: list[str]
    category_id: str = "10"
    privacy: str = "private"
    publish_at: datetime | None = None       # aware datetime; requires privacy "private"
    made_for_kids: bool = False
    language: str = "en"

    def validate(self) -> None:
        if not self.title.strip():
            raise ValueError("The title is empty.")
        if len(self.title) > 100:
            raise ValueError(f"The title is {len(self.title)} characters; YouTube allows 100.")
        if "<" in self.title or ">" in self.title or "<" in self.description or ">" in self.description:
            raise ValueError("YouTube doesn't allow < or > in titles or descriptions.")
        if len(self.description.encode("utf-8")) > 5000:
            raise ValueError("The description is longer than YouTube's 5000 bytes.")
        if sum(len(t) + (2 if " " in t else 0) for t in self.tags) + max(0, len(self.tags) - 1) > 500:
            raise ValueError("The tags add up to more than YouTube's 500 characters.")
        if self.privacy not in ("private", "unlisted", "public"):
            raise ValueError(f"Unknown privacy {self.privacy!r}")
        if self.publish_at is not None:
            if self.publish_at.tzinfo is None:
                raise ValueError("publish_at must be timezone-aware")
            if self.publish_at <= datetime.now(timezone.utc):
                raise ValueError("The scheduled time is in the past.")

    def body(self) -> dict:
        self.validate()
        status = {"privacyStatus": "private" if self.publish_at else self.privacy,
                  "selfDeclaredMadeForKids": self.made_for_kids}
        if self.publish_at:
            status["publishAt"] = self.publish_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return {
            "snippet": {"title": self.title, "description": self.description, "tags": self.tags,
                        "categoryId": self.category_id, "defaultLanguage": self.language,
                        "defaultAudioLanguage": self.language},
            "status": status,
        }


def next_publish_time(hhmm: str, now: datetime | None = None) -> datetime:
    """Next occurrence of local `hh:mm` (aware, local zone), at least 15 min from now."""
    now = (now or datetime.now().astimezone())
    h, m = (int(x) for x in hhmm.split(":"))
    t = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if t <= now + timedelta(minutes=15):
        t += timedelta(days=1)
    return t


def build_meta(hook: str, hours: float, template: dict | None = None, schedule: bool = False,
               privacy: str | None = None, now: datetime | None = None) -> VideoMeta:
    tpl = template or load_template()
    fmt = {"hook": hook.strip() or tpl.get("default_hook", ""), "hours": f"{hours:g}",
           "date": (now or datetime.now()).strftime("%d %b %Y")}
    return VideoMeta(
        title=tpl["title"].format(**fmt),
        description=tpl["description"].format(**fmt),
        tags=list(tpl.get("tags", [])),
        category_id=str(tpl.get("category_id", "10")),
        privacy=privacy or tpl.get("privacy", "private"),
        publish_at=next_publish_time(tpl.get("publish_time", "04:00"), now) if schedule else None,
        made_for_kids=bool(tpl.get("made_for_kids", False)),
        language=tpl.get("language", "en"),
    )


# =========================================================================== auth

def is_configured() -> bool:
    return CLIENT_SECRET.exists()


def credentials(interactive: bool = True):
    """Saved token (refreshed if needed), else the browser sign-in (once)."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    creds = None
    if TOKEN.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES)
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            TOKEN.write_text(creds.to_json(), encoding="utf-8")
            return creds
        except Exception:
            creds = None
    if not interactive:
        raise UploadError("Not signed in to YouTube.")
    if not CLIENT_SECRET.exists():
        raise UploadError("YouTube isn't set up yet (data/youtube_client_secret.json is missing).")
    from google_auth_oauthlib.flow import InstalledAppFlow
    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET), SCOPES)
    creds = flow.run_local_server(port=0, open_browser=True, prompt="consent",
                                  success_message="Signed in. You can close this tab and go back to Hush Studio.")
    DATA.mkdir(parents=True, exist_ok=True)
    TOKEN.write_text(creds.to_json(), encoding="utf-8")
    return creds


def sign_out() -> None:
    TOKEN.unlink(missing_ok=True)


def authorized_session(interactive: bool = True):
    from google.auth.transport.requests import AuthorizedSession
    return AuthorizedSession(credentials(interactive))


# =========================================================================== resumable upload

@dataclass
class UploadResult:
    video_id: str
    url: str
    privacy: str
    requested_privacy: str
    resumed: bool

    @property
    def locked_private(self) -> bool:
        """YouTube kept it private although public/unlisted was asked for (unverified project)."""
        return self.privacy == "private" and self.requested_privacy != "private"


ProgressFn = Callable[[float], None]


@dataclass
class ResumableUpload:
    session: object                          # requests-like: .post(...), .put(...)
    path: Path
    meta: VideoMeta
    chunk: int = CHUNK
    max_retries: int = 10
    sleep: Callable[[float], None] = time.sleep
    state_path: Path | None = None
    _resumed: bool = field(default=False, init=False)

    def __post_init__(self):
        self.path = Path(self.path)
        self.size = self.path.stat().st_size
        self.state_path = Path(self.state_path or self.path.with_suffix(self.path.suffix + ".upload.json"))
        if self.chunk % (256 * 1024):
            raise ValueError("chunk must be a multiple of 256 KiB")

    # ------------------------------------------------------------------ state (survives restarts)

    def _load_state(self) -> str | None:
        try:
            st = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if st.get("size") == self.size and st.get("mtime") == int(self.path.stat().st_mtime):
            return st.get("uri")
        return None

    def _save_state(self, uri: str) -> None:
        self.state_path.write_text(json.dumps({"uri": uri, "size": self.size, "mtime": int(self.path.stat().st_mtime),
                                               "title": self.meta.title, "started": time.time()}), encoding="utf-8")

    def _clear_state(self) -> None:
        self.state_path.unlink(missing_ok=True)

    # ------------------------------------------------------------------ protocol

    def _start_session(self) -> str:
        r = self._call("post", UPLOAD_URL, params={"uploadType": "resumable", "part": "snippet,status"},
                       json=self.meta.body(),
                       headers={"X-Upload-Content-Length": str(self.size), "X-Upload-Content-Type": "video/mp4",
                                "Content-Type": "application/json; charset=UTF-8"})
        if r.status_code != 200 or "Location" not in r.headers:
            raise UploadError(_explain(r))
        uri = r.headers["Location"]
        self._save_state(uri)
        return uri

    def _query(self, uri: str) -> tuple[int | None, dict | None]:
        """Ask the server how much it has: (next offset, None) or (None, finished video)."""
        r = self._call("put", uri, headers={"Content-Range": f"bytes */{self.size}", "Content-Length": "0"})
        return self._interpret(r)

    def _interpret(self, r) -> tuple[int | None, dict | None]:
        if r.status_code in (200, 201):
            return None, r.json()
        if r.status_code == 308:
            rng = r.headers.get("Range")
            return (int(rng.split("-")[1]) + 1 if rng else 0), None
        if r.status_code in (404, 410):
            raise _SessionExpired()
        raise UploadError(_explain(r))

    def _call(self, method: str, url: str, **kw):
        """HTTP with exponential backoff on network errors, 5xx and 429."""
        import requests
        for attempt in range(self.max_retries + 1):
            try:
                r = getattr(self.session, method)(url, timeout=120, **kw)
            except (requests.ConnectionError, requests.Timeout, ConnectionError, TimeoutError) as e:
                err = e
            else:
                if r.status_code < 500 and r.status_code != 429:
                    return r
                err = UploadError(_explain(r))
            if attempt == self.max_retries:
                raise UploadError(f"Gave up after {self.max_retries} retries: {err}")
            self.sleep(min(64.0, 2 ** attempt) + random.random())
            if method == "put" and "data" in kw:
                raise _RetryFromServerOffset()       # a chunk failed: re-ask the offset before resending
        raise AssertionError("unreachable")

    def run(self, on_progress: ProgressFn | None = None, cancel: threading.Event | None = None) -> UploadResult:
        restarted = False
        while True:
            try:
                return self._run_once(on_progress, cancel)
            except _SessionExpired:
                if restarted:
                    raise UploadError("The upload session expired twice; please try again later.")
                self._clear_state()
                restarted = True

    def _run_once(self, on_progress, cancel) -> UploadResult:
        uri = self._load_state()
        offset, done = 0, None
        if uri:
            self._resumed = True
            offset, done = self._query(uri)
        else:
            uri = self._start_session()
        fails = 0
        with open(self.path, "rb") as f:
            while done is None:
                if cancel is not None and cancel.is_set():
                    raise InterruptedError("Upload paused. It will continue from here next time.")
                if on_progress:
                    on_progress(offset / self.size if self.size else 1.0)
                f.seek(offset)
                data = f.read(self.chunk)
                end = offset + len(data) - 1
                try:
                    r = self._call("put", uri, data=data,
                                   headers={"Content-Range": f"bytes {offset}-{end}/{self.size}",
                                            "Content-Length": str(len(data))})
                    offset, done = self._interpret(r)
                    fails = 0
                except _RetryFromServerOffset:
                    fails += 1
                    if fails > self.max_retries:
                        raise UploadError(f"Gave up after {self.max_retries} failed chunks in a row.")
                    offset, done = self._query(uri)
        self._clear_state()
        if on_progress:
            on_progress(1.0)
        vid = done.get("id", "")
        got = (done.get("status") or {}).get("privacyStatus", self.meta.privacy)
        # a scheduled video is private until its publish time, so it can't show the unverified lock
        requested = "private" if self.meta.publish_at else self.meta.privacy
        return UploadResult(vid, f"https://youtu.be/{vid}", got, requested, self._resumed)


class _SessionExpired(Exception):
    pass


class _RetryFromServerOffset(Exception):
    pass


def _explain(r) -> str:
    """Human-readable reason from a YouTube error response."""
    try:
        err = r.json().get("error", {})
        reasons = {e.get("reason") for e in err.get("errors", [])}
        msg = err.get("message", "")
    except Exception:
        reasons, msg = set(), getattr(r, "text", "")[:300]
    if "quotaExceeded" in reasons or "uploadLimitExceeded" in reasons:
        return "YouTube's daily upload limit for this project is used up. Try again tomorrow."
    if r.status_code == 401:
        return "YouTube sign-in expired. Sign in again."
    if r.status_code == 403:
        return f"YouTube refused the upload: {msg or 'permission denied'}"
    return f"YouTube error {r.status_code}: {msg}"


def upload(path: Path, meta: VideoMeta, on_progress: ProgressFn | None = None,
           cancel: threading.Event | None = None, session=None) -> UploadResult:
    from .log_setup import get_logger
    log = get_logger("upload")
    log.info("upload start: %s (%s bytes)", path.name, path.stat().st_size)
    try:
        res = ResumableUpload(session or authorized_session(), path, meta).run(on_progress, cancel)
    except Exception:
        log.exception("upload failed: %s", path.name)
        raise
    log.info("upload done: %s", path.name)
    return res
