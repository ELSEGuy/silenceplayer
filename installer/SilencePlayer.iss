; Inno Setup script for the Silence Player installer.
;
; Built by build.ps1 (or CI) after PyInstaller has made dist\SilencePlayer:
;   iscc /DAppVersion=1.4.0 installer\SilencePlayer.iss
; Output: dist\SilencePlayer-Setup-<version>.exe
;
; Installs for the current Windows user only, so it never asks for admin
; rights: into %LOCALAPPDATA%\Programs\SilencePlayer, where Silence Player can
; keep config.json next to silenceplayer.exe. Installing a newer version over
; it keeps the settings. Uninstalling also removes the "Start with Windows"
; entry, but leaves config.json so a reinstall keeps your settings.

#define AppName "Silence Player"
#define AppExe "silenceplayer.exe"
#ifndef AppVersion
  #define AppVersion "0.0.0-dev"
#endif
#define SourceDir "..\dist\SilencePlayer"

[Setup]
; Never change AppId: Windows uses it to recognise upgrades of this app.
AppId={{15F8772A-9CDC-4F59-98B4-34FFD4AB5260}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=ELSEGuy
AppPublisherURL=https://github.com/ELSEGuy/silenceplayer
AppSupportURL=https://github.com/ELSEGuy/silenceplayer/issues
AppUpdatesURL=https://github.com/ELSEGuy/silenceplayer/releases
PrivilegesRequired=lowest
DefaultDirName={autopf}\SilencePlayer
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableReadyPage=yes
OutputDir=..\dist
OutputBaseFilename=SilencePlayer-Setup-{#AppVersion}
SetupIconFile=..\icon.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
; Silence Player's single-instance mutex: Setup asks you to quit it first
; (tray icon > Quit) instead of replacing files while it runs.
AppMutex=SilencePlayer.SingleInstance
CloseApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[InstallDelete]
; Upgrades: start from a clean bundle so files a newer version no longer
; ships (old VLC plugins, ...) don't linger. config.json is not in here.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Registry]
; Written by Silence Player itself (Start with Windows); only removed here.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueName: "SilencePlayer"; ValueType: none; Flags: uninsdeletevalue dontcreatekey
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"; ValueName: "SilencePlayer"; ValueType: none; Flags: uninsdeletevalue dontcreatekey

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent
