"""In-app updater (T23, D05): find a newer release, download and check it, hand over to Setup.

Releases live in the public `Hush-Studio-Releases` repo (RELEASING.md), so asking GitHub needs
no login: `GET /repos/<repo>/releases/latest` (drafts and `-rc` pre-releases are never "latest").
Each release carries `Hush-Studio-Setup-<version>.exe` and `<that>.sha256` (sha256sum format).

Installing: the running app holds `HushStudioAppMutex` and its own files are what Setup replaces,
so it can't run Setup itself. `launch_installer()` writes a tiny PowerShell script next to the
download that waits for this process to exit, runs Setup silently (same AppId = upgrade in place;
Sam's data in %LOCALAPPDATA%\\HushStudio and HKCU settings are never touched), records Setup's
exit code and reopens the app. The caller quits right after launching it.

Everything here is plain stdlib (urllib, like the sky radar) and UI-free; ui/updates.py drives it.
Overrides for testing against a fake endpoint: HUSH_UPDATE_API (API base URL) and
HUSH_RELEASES_REPO (owner/name).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .app_info import APP_VERSION, USER_AGENT

DEFAULT_API = "https://api.github.com"
DEFAULT_REPO = "thechadnesss-ship-it/Hush-Studio-Releases"
SETUP_RE = re.compile(r"^Hush-Studio-Setup-[0-9][0-9A-Za-z.\-]*\.exe$", re.IGNORECASE)
CHECK_TIMEOUT_S = 8.0
DOWNLOAD_TIMEOUT_S = 30.0
CHUNK = 1 << 20
# Inno Setup command line: no wizard, no message boxes (defaults), no reboot, can't be cancelled
# half-way. Without /MERGETASKS the previous choice (desktop icon or not) is remembered.
SETUP_ARGS = ("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/NOCANCEL", "/SP-")
APP_MUTEX = "HushStudioAppMutex"      # held by main.py; build/hush_studio.iss AppMutex


class UpdateError(Exception):
    """A plain-language reason the check or download didn't work."""


@dataclass(frozen=True)
class Release:
    version: str                 # "0.3.1" (tag without the leading v)
    tag: str
    notes: str                   # markdown, the CHANGELOG section (build footer removed)
    page_url: str                # the release page, for "open in browser"
    setup_name: str
    setup_url: str
    setup_size: int
    sha256_url: str | None


def api_base() -> str:
    return os.environ.get("HUSH_UPDATE_API", DEFAULT_API).rstrip("/")


def releases_repo() -> str:
    return os.environ.get("HUSH_RELEASES_REPO", DEFAULT_REPO)


def parse_version(v: str) -> tuple[int, ...] | None:
    """'v0.3.1' / '0.3.1' / '0.3' -> (0, 3, 1) / (0, 3, 0). Pre-release or odd tags -> None."""
    m = re.fullmatch(r"v?(\d+)(?:\.(\d+))?(?:\.(\d+))?", (v or "").strip())
    if not m:
        return None
    return tuple(int(x or 0) for x in m.groups())


def is_newer(candidate: str, current: str = APP_VERSION) -> bool:
    a, b = parse_version(candidate), parse_version(current)
    return a is not None and b is not None and a > b


def _get(url: str, timeout: float, accept: str = "application/vnd.github+json"):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept,
                                               "X-GitHub-Api-Version": "2022-11-28"})
    return urllib.request.urlopen(req, timeout=timeout)


def _clean_notes(body: str) -> str:
    # The release workflow appends "\n---\nBuilt from <repo>@<sha> ... Get-FileHash ..." for
    # people downloading by hand; Sam doesn't need it in What's new.
    body = (body or "").replace("\r\n", "\n")
    cut = body.rfind("\n---\nBuilt from")
    return (body[:cut] if cut >= 0 else body).strip()


def parse_release(data: dict) -> Release | None:
    """The bits of a GitHub release object we use; None if it has no usable installer."""
    if data.get("draft") or data.get("prerelease"):
        return None
    tag = str(data.get("tag_name") or "")
    if parse_version(tag) is None:
        return None
    assets = {a.get("name", ""): a for a in data.get("assets") or []}
    setups = [n for n in assets if SETUP_RE.match(n)]
    if not setups:
        return None
    name = setups[0]
    sha = assets.get(name + ".sha256")
    return Release(
        version=tag.lstrip("v"), tag=tag, notes=_clean_notes(data.get("body") or ""),
        page_url=str(data.get("html_url") or ""), setup_name=name,
        setup_url=str(assets[name].get("browser_download_url") or ""),
        setup_size=int(assets[name].get("size") or 0),
        sha256_url=(sha or {}).get("browser_download_url"))


def latest_release(timeout: float = CHECK_TIMEOUT_S) -> Release | None:
    """The newest published release, or None if there isn't one with an installer yet.
    Raises UpdateError when GitHub can't be reached (offline, rate-limited, broken reply)."""
    url = f"{api_base()}/repos/{releases_repo()}/releases/latest"
    try:
        with _get(url, timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:                     # repo has no releases yet
            return None
        if e.code in (403, 429):
            raise UpdateError("GitHub is busy right now (too many checks). Try again in an hour.") from e
        raise UpdateError(f"GitHub answered with an error ({e.code}).") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise UpdateError("Couldn't reach GitHub. Check the internet connection.") from e
    except ValueError as e:
        raise UpdateError("GitHub sent a reply the app couldn't read.") from e
    return parse_release(data) if isinstance(data, dict) else None


def check(current: str = APP_VERSION, timeout: float = CHECK_TIMEOUT_S) -> Release | None:
    """The latest release if it's newer than `current`, else None. Raises UpdateError."""
    rel = latest_release(timeout)
    return rel if rel is not None and is_newer(rel.version, current) else None


def expected_sha256(rel: Release, timeout: float = CHECK_TIMEOUT_S) -> str:
    if not rel.sha256_url:
        raise UpdateError("This release has no checksum file, so it can't be checked. Not installing it.")
    try:
        with _get(rel.sha256_url, timeout, accept="application/octet-stream") as r:
            text = r.read(4096).decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise UpdateError("Couldn't download the checksum. Check the internet connection.") from e
    m = re.search(r"\b([0-9a-fA-F]{64})\b", text)
    if not m:
        raise UpdateError("The checksum file is unreadable. Not installing this update.")
    return m.group(1).lower()


def download(rel: Release, folder: Path, progress: Callable[[float], None] | None = None,
             cancel: threading.Event | None = None) -> Path:
    """Download the installer into `folder` and check its SHA-256. Returns the verified file.
    A wrong checksum deletes the download and raises UpdateError."""
    want = expected_sha256(rel)
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / rel.setup_name
    part = dest.with_name(dest.name + ".part")
    h = hashlib.sha256()
    got = 0
    try:
        with _get(rel.setup_url, DOWNLOAD_TIMEOUT_S, accept="application/octet-stream") as r, open(part, "wb") as f:
            total = int(r.headers.get("Content-Length") or rel.setup_size or 0)
            while True:
                if cancel is not None and cancel.is_set():
                    raise UpdateError("Download stopped.")
                block = r.read(CHUNK)
                if not block:
                    break
                f.write(block)
                h.update(block)
                got += len(block)
                if progress and total:
                    progress(min(1.0, got / total))
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        part.unlink(missing_ok=True)
        raise UpdateError("The download broke off. Check the internet connection and try again.") from e
    except UpdateError:
        part.unlink(missing_ok=True)
        raise
    if h.hexdigest() != want:
        part.unlink(missing_ok=True)
        raise UpdateError("The downloaded installer didn't match its checksum, so it was thrown away. "
                          "Try again later.")
    dest.unlink(missing_ok=True)
    part.replace(dest)
    return dest


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def _ps_quote(s: str) -> str:
    return "'" + str(s).replace("'", "''") + "'"


def installer_script(setup: Path, pid: int, relaunch: Path | None, exit_file: Path, log_file: Path,
                     process_name: str = "", mutex: str = APP_MUTEX) -> str:
    """PowerShell that waits for the app to close, runs Setup silently, notes how it went and
    reopens the app (also after a failed Setup, so Sam is never left with nothing open).

    "Closed" = our process is gone (same PID *and* name, so a reused PID can't stall it) and no
    window holds the app mutex any more: with /SUPPRESSMSGBOXES Setup would otherwise quietly
    cancel on Inno's "Hush Studio is running" prompt. Gives up waiting after 2 minutes."""
    args = ", ".join(_ps_quote(a) for a in (*SETUP_ARGS, f'/LOG="{log_file}"'))
    lines = [
        "$ErrorActionPreference = 'SilentlyContinue'",
        "$deadline = (Get-Date).AddSeconds(120)",
        "while ((Get-Date) -lt $deadline) {",
        f"  $p = Get-Process -Id {int(pid)} -ErrorAction SilentlyContinue",
        f"  $alive = $p -and ({_ps_quote(process_name)} -eq '' -or $p.ProcessName -eq {_ps_quote(process_name)})",
        "  $held = $false",
        f"  try {{ [System.Threading.Mutex]::OpenExisting({_ps_quote(mutex)}).Dispose(); $held = $true }} catch {{ }}",
        "  if (-not $alive -and -not $held) { break }",
        "  Start-Sleep -Milliseconds 250",
        "}",
        "Start-Sleep -Milliseconds 500",
        "$code = -1",
        "try {",
        f"  $p = Start-Process -FilePath {_ps_quote(setup)} -ArgumentList @({args}) -Wait -PassThru",
        "  $code = $p.ExitCode",
        "} catch { $code = -2 }",
        f"Set-Content -Path {_ps_quote(exit_file)} -Value $code -Encoding ascii",
    ]
    if relaunch is not None:
        lines.append(f"Start-Process -FilePath {_ps_quote(relaunch)}")
    return "\r\n".join(lines) + "\r\n"


def relaunch_target() -> Path | None:
    """The installed HushStudio.exe (upgrade in place keeps the path); None when run from source."""
    return Path(sys.executable) if getattr(sys, "frozen", False) else None


def launch_installer(setup: Path, pid: int | None = None, relaunch: Path | None = None,
                     popen=subprocess.Popen) -> Path:
    """Start the detached hand-off script. The caller must quit the app straight after.
    Returns the script path. `popen` is injectable for tests."""
    folder = setup.parent
    script = folder / "apply_update.ps1"
    exit_file = folder / "install.exitcode"
    exit_file.unlink(missing_ok=True)
    if pid is None:
        pid, name = os.getpid(), Path(sys.executable).stem
    else:
        name = ""
    script.write_text(installer_script(setup, pid, relaunch, exit_file, folder / "install.log", name),
                      encoding="utf-8-sig")
    cmd = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
           "-WindowStyle", "Hidden", "-File", str(script)]
    kw = dict(close_fds=True, cwd=str(folder), stdin=subprocess.DEVNULL,
              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if sys.platform != "win32":
        popen(cmd, **kw)
        return script
    # A hidden console, not DETACHED_PROCESS: measured, powershell.exe with no console at all
    # exits at once without running the script. Break away from any job object so closing the
    # app can't take the helper with it (not every job allows that: then start it normally).
    flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        popen(cmd, creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB, **kw)
    except OSError:
        popen(cmd, creationflags=flags, **kw)
    return script


def last_install_exit_code(folder: Path) -> int | None:
    try:
        return int((folder / "install.exitcode").read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None


def clean_downloads(folder: Path) -> None:
    """Remove finished/abandoned installer downloads (they're 100+ MB). Only our own files."""
    if not folder.is_dir():
        return
    for p in folder.iterdir():
        if p.is_file() and (SETUP_RE.match(p.name) or p.name.endswith(".exe.part")):
            try:
                p.unlink()
            except OSError:
                pass        # Setup may still be closing; next start tries again
