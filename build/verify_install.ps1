# Verify T21 on a clean Windows 10/11 VM (or any PC): install -> self-test -> upgrade -> uninstall,
# checking that takes and settings survive.   Run as the normal user (no admin):
#
#   powershell -ExecutionPolicy Bypass -File verify_install.ps1 -Setup .\Hush-Studio-Setup-0.2.exe
#
# It adds one marker take (Takes\_install-check) and one marker setting, and removes only those
# two at the end. Recording, uploading and the first-run microphone message still need a person:
# see the manual steps printed at the end.
param(
    [Parameter(Mandatory = $true)][string]$Setup,
    [switch]$KeepInstalled      # don't uninstall at the end
)
$ErrorActionPreference = 'Stop'
$Setup = (Resolve-Path $Setup).Path
$InstallDir = Join-Path $env:LOCALAPPDATA 'Programs\Hush Studio'
$Exe = Join-Path $InstallDir 'HushStudio.exe'
$DataDir = Join-Path $env:LOCALAPPDATA 'HushStudio'
$MarkerTake = Join-Path $DataDir 'Takes\_install-check'
$RegKey = 'HKCU:\Software\HushStudio\Hush Studio'
$failures = @()

function Check($ok, $what) {
    if ($ok) { Write-Host "  ok    $what" -ForegroundColor Green }
    else { Write-Host "  FAIL  $what" -ForegroundColor Red; $script:failures += $what }
}
function Install-Silently($label) {
    Write-Host "==> $label"
    $p = Start-Process -FilePath $Setup -ArgumentList '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/TASKS=desktopicon' -Wait -PassThru
    Check ($p.ExitCode -eq 0) "$label exit code 0 (got $($p.ExitCode))"
    Check (Test-Path $Exe) "$label put HushStudio.exe in $InstallDir (no admin)"
}
function Self-Test($label) {
    $report = Join-Path $env:TEMP "hush-selftest-$label.json"
    Remove-Item $report -ErrorAction SilentlyContinue
    $p = Start-Process -FilePath $Exe -ArgumentList '--self-test', "`"$report`"" -Wait -PassThru
    if (Test-Path $report) {
        $r = Get-Content $report -Raw | ConvertFrom-Json
        foreach ($c in $r.results) { Check $c.ok "self-test ($label): $($c.check) - $($c.detail)" }
        Check ($r.data_folder -eq $DataDir) "data folder is $DataDir (got $($r.data_folder))"
    } else { Check $false "self-test ($label) wrote a report (exit $($p.ExitCode))" }
}

Install-Silently 'First install'
Check (Test-Path (Join-Path ([Environment]::GetFolderPath('Programs')) 'Hush Studio.lnk')) 'Start menu shortcut'
Check (Test-Path (Join-Path ([Environment]::GetFolderPath('Desktop')) 'Hush Studio.lnk')) 'Desktop shortcut'
Self-Test 'install'

Write-Host '==> Add a marker take and setting'
New-Item -ItemType Directory -Force $MarkerTake | Out-Null
Set-Content -Path (Join-Path $MarkerTake 'take.json') -Value '{"name": "install check"}' -Encoding UTF8
New-Item -Force -Path $RegKey -ErrorAction SilentlyContinue | Out-Null
New-ItemProperty -Path $RegKey -Name 'install_check' -Value 'keep-me' -PropertyType String -Force | Out-Null

Install-Silently 'Upgrade (same Setup.exe again)'
Check (Test-Path (Join-Path $MarkerTake 'take.json')) 'take survives the upgrade'
Check ((Get-ItemProperty -Path $RegKey -Name 'install_check' -ErrorAction SilentlyContinue).install_check -eq 'keep-me') 'setting survives the upgrade'
Self-Test 'upgrade'

if (-not $KeepInstalled) {
    Write-Host '==> Uninstall'
    $un = Get-ChildItem $InstallDir -Filter 'unins*.exe' | Select-Object -First 1
    $p = Start-Process -FilePath $un.FullName -ArgumentList '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART' -Wait -PassThru
    # The uninstaller re-launches itself from %TEMP%, so wait for the files to go (up to 60 s).
    for ($i = 0; $i -lt 60 -and (Test-Path $Exe); $i++) { Start-Sleep -Seconds 1 }
    Check (-not (Test-Path $Exe)) 'uninstall removed the program'
    Check (Test-Path (Join-Path $MarkerTake 'take.json')) 'take survives the uninstall'
    Check ((Get-ItemProperty -Path $RegKey -Name 'install_check' -ErrorAction SilentlyContinue).install_check -eq 'keep-me') 'setting survives the uninstall'
}

Write-Host '==> Remove the markers (only what this script added)'
Remove-Item -Recurse -Force $MarkerTake
Remove-ItemProperty -Path $RegKey -Name 'install_check' -ErrorAction SilentlyContinue

Write-Host ''
Write-Host 'Manual steps (need a person, a mic and a Google account):'
Write-Host '  1. Install with a double-click (SmartScreen: More info -> Run anyway). Open Hush Studio.'
Write-Host '  2. First run: the microphone check stays quiet if the mic works. To test the message, turn off'
Write-Host '     Settings -> Privacy & security -> Microphone -> "Let desktop apps access your microphone" and restart the app.'
Write-Host '  3. Record ~1 min, Finish & render (1 h), open the video.  4. Upload it to YouTube (private).'
Write-Host '  5. Run the Setup.exe again, then uninstall: the take is still in Takes and opens after reinstalling.'
if ($failures.Count) { Write-Host "$($failures.Count) check(s) FAILED" -ForegroundColor Red; exit 1 }
Write-Host 'All automatic checks passed.' -ForegroundColor Green
