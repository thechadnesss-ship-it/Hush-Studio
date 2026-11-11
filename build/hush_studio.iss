; Inno Setup 6 script for Hush Studio (T21) -> dist\Hush-Studio-Setup-<version>.exe
; Build with build\build.ps1 (it passes /DAppVersion from core\app_info.py).
;
; * Per-user install, no admin: %LOCALAPPDATA%\Programs\Hush Studio.
; * Start menu + desktop shortcut, uninstaller in Apps & features.
; * Sam's data lives OUTSIDE the install folder and is never listed here, so upgrading
;   (running a newer Setup.exe over the old one) and uninstalling both leave it alone:
;     %LOCALAPPDATA%\HushStudio\  Takes, Intros, cache, data (YouTube token), logs
;     HKCU\Software\HushStudio\    settings (QSettings)
; * Unsigned: SmartScreen shows "Windows protected your PC" -> More info -> Run anyway.
; * Fully offline: everything (Python, Qt, FFmpeg, deep-filter) is inside, so the same file
;   works from a flash drive.

#ifndef AppVersion
  #define AppVersion "0.0"
#endif
#define AppName "Hush Studio"
#define AppExe "HushStudio.exe"

[Setup]
; Never change AppId: it is how a newer Setup.exe finds and upgrades the installed copy.
AppId={{7A23A1D2-BFE1-4F5E-9F79-48555348A1D0}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Hush Studio
VersionInfoVersion={#AppVersion}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=auto
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir=..\dist
OutputBaseFilename=Hush-Studio-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\{#AppExe}
; Ask Sam to close the app before upgrading/uninstalling (main.py holds this mutex).
AppMutex=HushStudioAppMutex
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Put a Hush Studio icon on the desktop"; GroupDescription: "Shortcuts:"

[InstallDelete]
; Upgrades replace the program files cleanly (stale DLLs from an older build can break Qt).
; Only the install folder: Sam's data is in %LOCALAPPDATA%\HushStudio, not here.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "..\dist\HushStudio\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "Open Hush Studio now"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Intentionally nothing outside {app}. Takes, intros and settings stay after uninstalling;
; installing again picks them straight back up.
Type: filesandordirs; Name: "{app}\_internal"

[Messages]
FinishedLabel=Hush Studio is installed. Your takes and settings are kept in your own user folder, so updating or uninstalling never deletes them.
