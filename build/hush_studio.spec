# PyInstaller spec for Hush Studio (T21): one-folder build → dist/HushStudio/HushStudio.exe.
# Run through build/build.ps1, which also checks the result and wraps it with Inno Setup.
#
# Layout inside dist/HushStudio/_internal (= sys._MEIPASS = core.config.bundle_root()):
#   config/*.toml, assets/ (font + licence), tools/ffmpeg/{ffmpeg,ffprobe}.exe + LICENSE,
#   tools/deepfilter/deep-filter.exe + vcruntime140.dll (deep-filter needs the VC++ runtime,
#   which a clean Windows 10 doesn't have), THIRD-PARTY-NOTICES.md.
# Takes, intros, caches, the YouTube token and logs go to %LOCALAPPDATA%\HushStudio instead
# (core.config.resolve_data_root), so upgrades/uninstall never touch them.
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent
sys.path.insert(0, str(ROOT))
from core.app_info import APP_NAME, APP_VERSION  # noqa: E402


def _need(path: Path, hint: str) -> Path:
    if not path.is_file():
        raise SystemExit(f"[hush build] missing {path}\n  {hint}")
    return path


def _ffmpeg_dir() -> Path:
    """HUSH_FFMPEG_DIR, else repo tools/ffmpeg/, else the FFmpeg the dev machine uses."""
    if os.environ.get("HUSH_FFMPEG_DIR"):
        return Path(os.environ["HUSH_FFMPEG_DIR"])
    local = ROOT / "tools" / "ffmpeg"
    if (local / "ffmpeg.exe").is_file():
        return local
    from core import ffmpeg_tools as ff
    return Path(ff.ffmpeg_bin()).parent


ffdir = _ffmpeg_dir()
ff_hint = "Put a static FFmpeg >= 6 build with libx264 (e.g. gyan.dev 'essentials') in tools/ffmpeg/ or set HUSH_FFMPEG_DIR."
ff_licence = next((p for p in (ffdir / "LICENSE", ffdir.parent / "LICENSE", ffdir / "LICENSE.txt") if p.is_file()), None)
if ff_licence is None:
    raise SystemExit(f"[hush build] no FFmpeg LICENSE next to {ffdir}; the GPL requires shipping it.")

df_exe = _need(ROOT / "tools" / "deepfilter" / "deep-filter.exe",
               "Download deep-filter v0.5.6 (x86_64-pc-windows-msvc) from github.com/Rikorose/DeepFilterNet/releases.")
vcruntime = _need(Path(sys.base_prefix) / "vcruntime140.dll",
                  "The build Python must be a python.org CPython (it ships vcruntime140.dll).")

datas = [
    (str(ROOT / "config" / "render.toml"), "config"),
    (str(ROOT / "config" / "youtube_template.toml"), "config"),
    (str(ROOT / "assets" / "fonts" / "Montserrat.ttf"), "assets/fonts"),
    (str(ROOT / "assets" / "fonts" / "OFL.txt"), "assets/fonts"),
    (str(_need(ffdir / "ffmpeg.exe", ff_hint)), "tools/ffmpeg"),
    (str(_need(ffdir / "ffprobe.exe", ff_hint)), "tools/ffmpeg"),
    (str(ff_licence), "tools/ffmpeg"),
    (str(df_exe), "tools/deepfilter"),
    (str(vcruntime), "tools/deepfilter"),
    (str(ROOT / "build" / "THIRD-PARTY-NOTICES.md"), "."),
]
# Sam's own companion GIFs (personal use only, see CLAUDE.md §6 Artwork) ship if present.
for gif in sorted((ROOT / "assets").glob("companion_*.gif")):
    datas.append((str(gif), "assets"))

icon = ROOT / ".work" / "hush.ico"

a = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=["core.selftest", "core.mic_check"],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "_tkinter", "pytest", "_pytest", "IPython", "matplotlib", "pandas",
              "PyQt5", "PySide2", "PySide6", "ops", "tests"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="HushStudio",
    console=False,                      # a window app: no black console box
    icon=str(icon) if icon.is_file() else None,
    upx=False,                          # UPX trips antivirus heuristics; installer is LZMA-compressed anyway
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="HushStudio")
