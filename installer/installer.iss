; Inno Setup script for Windows Face Unlock.
; Produces installer_output\WindowsFaceUnlock-Setup-<ver>.exe
;
; Build with:   ISCC.exe installer\installer.iss
; Or via:       python installer\build.py

#define MyAppName "Windows Face Unlock"
#define MyAppShortName "WindowsFaceUnlock"
#define MyAppVersion "0.1.0"
#define MyAppPublisher "Cao Chi Tam"
#define MyAppURL "https://github.com/caochitam/windows-face-unlock"
#define MyAppExeName "face_unlock_tray.exe"
; NOTE: the service/watchdog executables are deliberately NOT defined here.
; Scheduled tasks are declared once, in postinstall\tasks.psd1, and this script
; never names them -- see [Run] / [UninstallRun].
#define BuildRoot "..\dist\WindowsFaceUnlock"

[Setup]
AppId={{2F7A9B14-3C31-4B1E-9AB9-5E0D1B02B6A7}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}/issues
AppUpdatesURL={#MyAppURL}/releases
DefaultDirName={autopf}\{#MyAppShortName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=..\installer_output
OutputBaseFilename=WindowsFaceUnlock-Setup-{#MyAppVersion}
SetupIconFile=
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64
ArchitecturesAllowed=x64
PrivilegesRequired=admin
CloseApplications=yes
RestartApplications=yes
UsePreviousAppDir=yes
UsePreviousTasks=yes

[Languages]
; Inno Setup 6 ships only Default.isl (English) out of the box. The app
; itself is fully translated into 12 languages at runtime — the installer
; wizard stays English for simplicity.
Name: "english";  MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "cp"; Description: "Register the Credential Provider (enables log-in with your face)"; \
  Check: FileExists(ExpandConstant('{app}\credential_provider\FaceCredentialProvider.dll')); GroupDescription: "Optional components"; Flags: unchecked

[Files]
; The whole PyInstaller output, which already includes postinstall\ (the task
; registrar and its declaration, staged there by installer/build.py step 4).
Source: "{#BuildRoot}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Dirs]
; Writable log/config dir in the per-user profile — created on first run anyway,
; but pre-created so permissions are right the first time.
;
; This used to say {userappdata}\..\.face-unlock, which expands to
; %APPDATA%\..\.face-unlock = C:\Users\<u>\AppData\.face-unlock -- NOT the
; directory the application uses, and not the one CurUninstallStepChanged below
; offers to delete. The installer therefore created a stray empty directory that
; nothing read and no uninstall path removed, while the real data directory got
; no pre-created permissions at all. {%USERPROFILE} matches face_service/config.py
; (Path.home()/".face-unlock") and matches the uninstall code below.
Name: "{%USERPROFILE}\.face-unlock"; Permissions: users-modify

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{#MyAppName} — Uninstall"; Filename: "{uninstallexe}"

[Registry]
; InstallLocation is read by tools\register_tasks.ps1 when -Mode Installed is used without an
; explicit -InstallDir, so -Action Unregister and clean_restart.ps1 work without re-typing the
; path. Version is informational: it is what Programs and Features shows, and it is the only
; on-disk record of which build produced this install.
;
; This comment used to claim the values were "used by the auto-updater fallback". They were not --
; no Python module in the tree imports winreg at all, and the updater compares against the
; __version__ baked into the executable. Corrected rather than deleted, because the keys ARE
; written and something does read one of them now.
Root: HKLM; Subkey: "Software\{#MyAppShortName}"; ValueType: string; ValueName: "InstallLocation"; ValueData: "{app}"; Flags: uninsdeletekey
Root: HKLM; Subkey: "Software\{#MyAppShortName}"; ValueType: string; ValueName: "Version";         ValueData: "{#MyAppVersion}"

[Run]
; 1. Register the Credential Provider DLL (only if the user ticked the task)
Filename: "regsvr32.exe"; Parameters: "/s ""{app}\credential_provider\FaceCredentialProvider.dll"""; \
  Tasks: cp; StatusMsg: "Registering Credential Provider…"; Flags: runhidden

; 2. Create AND start the scheduled tasks. Task names, executables and settings
;    all come from postinstall\tasks.psd1 -- this script does not name them, so
;    adding or removing a task never needs an installer edit. Register also
;    starts what it registered, which is why there are no schtasks /Run lines.
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\postinstall\register_tasks.ps1"" -Mode Installed -InstallDir ""{app}"" -Action Register"; \
  StatusMsg: "Registering scheduled tasks…"; Flags: runhidden

; 3. Post-install: offer to bring the tray window up (it is already running).
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; \
  Flags: nowait postinstall skipifsilent

[UninstallRun]
; Stop and delete the scheduled tasks first so files aren't held open. This
; walks the SAME postinstall\tasks.psd1 the install used, so a task can never be
; created by one path and left behind by the other.
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\postinstall\register_tasks.ps1"" -Mode Installed -InstallDir ""{app}"" -Action Unregister"; \
  Flags: runhidden
; Unregister the Credential Provider if it was installed.
Filename: "regsvr32.exe"; Parameters: "/u /s ""{app}\credential_provider\FaceCredentialProvider.dll"""; \
  Flags: runhidden; Check: FileExists(ExpandConstant('{app}\credential_provider\FaceCredentialProvider.dll'))

[UninstallDelete]
Type: filesandordirs; Name: "{app}"

[Code]
function InitializeUninstall(): Boolean;
begin
  // Nothing fancy — UninstallRun handles task teardown.
  Result := True;
end;

{ The user's enrollment data is KEPT unless they say otherwise.

  This runs during an unattended upgrade too: presence_monitor/updater.py launches
  the installer with /SILENT, and a MsgBox in that context either blocks forever
  or is answered by nobody. Silently deleting biometric images and the DPAPI
  credential blob because a dialog could not be shown is the worst of the
  available outcomes, so silent mode now always keeps the data, and only an
  interactive uninstall asks. /REMOVEDATA forces removal for scripted teardown.

  tools\uninstall.ps1 is the fuller story (model cache, %TEMP% downloads,
  leftover verification); this stays deliberately minimal. }
function WantsDataRemoved(DataDir: string): Boolean;
begin
  Result := False;
  if CmdLineParamExists('/REMOVEDATA') then
  begin
    Result := True;
    exit;
  end;
  { UninstallSilent() covers both /SILENT and /VERYSILENT. }
  if UninstallSilent() then
    exit;
  Result := MsgBox('Also remove your saved enrollment data at ' + DataDir + '?'#13#10#13#10
                   + 'This includes your face embeddings and the encrypted Windows password. '
                   + 'Choose No to keep them for a future reinstall.',
                   mbConfirmation, MB_YESNO) = IDYES;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: string;
  ResultCode: Integer;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    DataDir := ExpandConstant('{%USERPROFILE}\.face-unlock');
    if DirExists(DataDir) then
    begin
      if WantsDataRemoved(DataDir) then
      begin
        Exec(ExpandConstant('{cmd}'), '/C rmdir /S /Q "' + DataDir + '"',
             '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
      end;
    end;
  end;
end;
