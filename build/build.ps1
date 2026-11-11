# Build Hush Studio's installer: dist\Hush-Studio-Setup-<version>.exe   (T21; see build\README.md)
#
#   powershell -ExecutionPolicy Bypass -File build\build.ps1
#
# Build-PC tools (never needed on Sam's PC):
#   * python.org Python 3.12 (the `py` launcher);  the script makes its own venv in .work\build-venv
#   * Inno Setup 6:  winget install JRSoftware.InnoSetup
#   * FFmpeg >= 6 with libx264 (tools\ffmpeg\, HUSH_FFMPEG_DIR, or the one on PATH)
#   * tools\deepfilter\deep-filter.exe (v0.5.6)
param(
    [switch]$SkipInstaller,     # stop after the PyInstaller folder + checks
    [switch]$SkipSelfTest       # skip running HushStudio.exe --self-test (~15 s)
)
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Invoke-Step($what, [scriptblock]$cmd) {
    Write-Host "==> $what" -ForegroundColor Magenta
    & $cmd
    if ($LASTEXITCODE -ne 0) { throw "$what failed (exit $LASTEXITCODE)" }
}

$Venv = Join-Path $Root '.work\build-venv'
$Py = Join-Path $Venv 'Scripts\python.exe'
if (-not (Test-Path $Py)) {
    Invoke-Step 'Create build venv (.work\build-venv)' { py -3.12 -m venv $Venv }
}
Invoke-Step 'Install app + build requirements' {
    & $Py -m pip install --disable-pip-version-check -q -r requirements.txt -r build\requirements-build.txt
}
$Version = (& $Py -c "from core.app_info import APP_VERSION; print(APP_VERSION)").Trim()
Write-Host "Hush Studio $Version"

Invoke-Step 'Draw icon' { & $Py build\make_icon.py .work\hush.ico }
Invoke-Step 'PyInstaller (one folder)' {
    & $Py -m PyInstaller --noconfirm --clean --distpath dist --workpath .work\pyinstaller build\hush_studio.spec
}
$checkArgs = @('build\check_bundle.py', 'dist\HushStudio')
if (-not $SkipSelfTest) { $checkArgs += '--run' }
Invoke-Step 'Check bundle' { & $Py @checkArgs }
if ($SkipInstaller) { Write-Host 'Skipped Inno Setup.'; exit 0 }

$Iscc = @(
    (Get-Command iscc.exe -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source),
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if (-not $Iscc) { throw 'Inno Setup 6 not found. Install it with: winget install JRSoftware.InnoSetup' }
Invoke-Step 'Inno Setup' { & $Iscc "/DAppVersion=$Version" build\hush_studio.iss }

$Setup = Join-Path $Root "dist\Hush-Studio-Setup-$Version.exe"
$MB = [math]::Round((Get-Item $Setup).Length / 1MB, 1)
Write-Host "Done: $Setup ($MB MB)" -ForegroundColor Green
