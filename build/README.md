# Building Hush Studio's installer (T21)

Output: `dist\Hush-Studio-Setup-<version>.exe`, one file Sam double-clicks. It needs nothing
pre-installed (Python, Qt, FFmpeg, deep-filter and the VC++ runtime are inside) and works offline,
so it can be copied to a flash drive.

Releases are built by GitHub Actions when a version tag is pushed (see `RELEASING.md`); this page
is for building one by hand on a Windows PC.

## One-time setup on the build PC (not Sam's)

* python.org **Python 3.12** with the `py` launcher (it ships `vcruntime140.dll`, which the build copies).
* **Inno Setup 6**: `winget install JRSoftware.InnoSetup`
* **FFmpeg ≥ 6 with libx264**: the gyan.dev build on PATH works; for a smaller installer put the
  gyan.dev *essentials* `ffmpeg.exe` + `ffprobe.exe` + `LICENSE` in `tools\ffmpeg\` (or set
  `HUSH_FFMPEG_DIR`). The *full* build is ~228 MB per exe.
* `tools\deepfilter\deep-filter.exe` v0.5.6 (already there on the dev PC).

## Build

```powershell
powershell -ExecutionPolicy Bypass -File build\build.ps1
```

1. makes `.work\build-venv` (its own venv; the dev `.venv` is untouched) and installs
   `requirements.txt` + `build\requirements-build.txt` (PyInstaller);
2. draws the icon (`build\make_icon.py` → `.work\hush.ico`);
3. PyInstaller one-folder build (`build\hush_studio.spec`) → `dist\HushStudio\`;
4. `build\check_bundle.py dist\HushStudio --run`: required files present, every helper exe only
   needs Windows' own DLLs or DLLs shipped beside it, then `HushStudio.exe --self-test` runs the
   whole pipeline frozen (FFmpeg, deep-filter, libsndfile, PortAudio, TLS, a synthetic take →
   1 h video) in a temp folder;
5. Inno Setup (`build\hush_studio.iss`) → `dist\Hush-Studio-Setup-<version>.exe`.

`-SkipInstaller` stops after step 4; `-SkipSelfTest` skips the frozen run.
The version comes from `core\app_info.py` (`APP_VERSION`); bump it for each release so Apps & features shows it.

## Where things go on Sam's PC

| What | Where | Upgrade / uninstall |
|---|---|---|
| Program | `%LOCALAPPDATA%\Programs\Hush Studio\` (per user, no admin) | replaced / removed |
| Takes, Intros, cache, YouTube token, sky history | `%LOCALAPPDATA%\HushStudio\` | **kept** |
| Logs | `%LOCALAPPDATA%\HushStudio\logs\` | **kept** |
| Settings | registry `HKCU\Software\HushStudio\Hush Studio` | **kept** |

`HUSH_DATA_DIR` still overrides the data folder (the USB launcher uses it).

## Verify (clean Windows 10/11 VM)

Copy `Hush-Studio-Setup-<version>.exe` and `build\verify_install.ps1` to the VM, then as the normal user:

```powershell
powershell -ExecutionPolicy Bypass -File verify_install.ps1 -Setup .\Hush-Studio-Setup-0.2.exe
```

It installs silently, runs the frozen self-test, adds a marker take + setting, installs again
(upgrade), uninstalls, and checks the marker take and setting survived both. It then prints the
manual steps: record, render, upload, and the first-run microphone message.

## Notes

* Unsigned: SmartScreen says "Windows protected your PC" → **More info → Run anyway**. Code
  signing costs money; decide later.
* First run checks the microphone (`core\mic_check.py`): Windows privacy switches (device,
  apps, desktop apps) and half a second of listening. If blocked, she gets plain steps and an
  "Open Windows settings" button; the check repeats at start-up until the mic has worked once.
* Licences: `build\THIRD-PARTY-NOTICES.md` (shipped in the app folder).
