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
; itself is fully translated into 12 languages at runtime -- the installer
; wizard stays English for simplicity.
Name: "english";  MessagesFile: "compiler:Default.isl"

[Tasks]
; Deliberately NO Check: on this entry. A task's Check runs while the Select Tasks
; page is being built, which is BEFORE [Files] copies anything, so the FileExists
; test that used to live here was False on every FIRST install: the checkbox never
; appeared, and the regsvr32 entry below -- gated on Tasks: cp -- therefore never
; ran. The Credential Provider got registered only when reinstalling over an
; install that already had the DLL on disk, which is the opposite of the intent.
; The file test moved to the [Run] entry, where it is evaluated at the right time.
Name: "cp"; Description: "Register the Credential Provider (enables log-in with your face)"; \
  GroupDescription: "Optional components"; Flags: unchecked

[Files]
; The whole PyInstaller output, which already includes postinstall\ (the task
; registrar and its declaration, staged there by installer/build.py step 4).
Source: "{#BuildRoot}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Dirs]
; Writable log/config dir in the per-user profile -- created on first run anyway,
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
Name: "{group}\{#MyAppName} - Uninstall"; Filename: "{uninstallexe}"

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
; Every system tool below is named through {sys}, never by bare filename, and that
; is load-bearing rather than tidy. Setup is a 32-bit process even in 64-bit
; install mode -- Inno's Setup.e32 and the setup stub this script compiles into are
; both IMAGE_FILE_MACHINE_I386 -- so a bare "regsvr32.exe" resolves through WOW64
; file-system redirection to SysWOW64\regsvr32.exe, which is 32-bit and cannot load
; our x64 DLL at all. The install would have reported success and left the
; Credential Provider unregistered. {sys} is the 64-bit System32 in 64-bit install
; mode and is not subject to that redirection.
;
; credential_provider\register.ps1:62 already refuses to run in a 32-bit host for
; exactly this reason ("Refuse rather than lie"). This section had no equivalent
; guard, and it had never been executed -- the installer had never been built.
;
; 1. Register the Credential Provider DLL (only if the user ticked the task).
;    The Check lives here rather than on the [Tasks] entry because a [Run] Check
;    is evaluated AFTER [Files] has copied the payload -- see the note in [Tasks].
;    It keeps the original intent intact: never hand regsvr32 a path that is not
;    in the layout, which is what a SKIP_CP=1 build produces.
Filename: "{sys}\regsvr32.exe"; Parameters: "/s ""{app}\credential_provider\FaceCredentialProvider.dll"""; \
  Tasks: cp; \
  Check: FileExists(ExpandConstant('{app}\credential_provider\FaceCredentialProvider.dll')); \
  StatusMsg: "Registering Credential Provider..."; Flags: runhidden

; 2. Create AND start the scheduled tasks. Task names, executables and settings
;    all come from postinstall\tasks.psd1 -- this script does not name them, so
;    adding or removing a task never needs an installer edit. Register also
;    starts what it registered, which is why there are no schtasks /Run lines.
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\postinstall\register_tasks.ps1"" -Mode Installed -InstallDir ""{app}"" -Action Register"; \
  StatusMsg: "Registering scheduled tasks..."; Flags: runhidden

; 3. Post-install: offer to bring the tray window up (it is already running).
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; \
  Flags: nowait postinstall skipifsilent

[UninstallRun]
; Stop and delete the scheduled tasks first so files aren't held open. This
; walks the SAME postinstall\tasks.psd1 the install used, so a task can never be
; created by one path and left behind by the other.
;
; {sys} for the same reason as [Run] -- see the note there. The uninstaller is the
; same 32-bit binary, so an unregister through a bare name would hit the 32-bit
; regsvr32 and leave the Credential Provider registered after removal.
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\postinstall\register_tasks.ps1"" -Mode Installed -InstallDir ""{app}"" -Action Unregister"; \
  Flags: runhidden
; Unregister the Credential Provider if it was installed.
Filename: "{sys}\regsvr32.exe"; Parameters: "/u /s ""{app}\credential_provider\FaceCredentialProvider.dll"""; \
  Flags: runhidden; Check: FileExists(ExpandConstant('{app}\credential_provider\FaceCredentialProvider.dll'))

[UninstallDelete]
Type: filesandordirs; Name: "{app}"

[Code]

{ NOT a built-in, despite reading like one. ParamCount, ParamStr and CompareText
  ARE built in -- all three are in ISCmplr.dll's identifier table, and Inno's own
  Examples\CodePrepareToInstall.iss calls ParamStr -- but the helper that walks
  them is a convention each project rolls for itself. This script had called it
  since it was written while [Code] had never once been compiled, so the first
  ISCC run in the project's history stopped on it:

      Error on line 139 in installer.iss, Column 6:
      Unknown identifier 'CmdLineParamExists'
      Compile aborted.

  Declared at the very top of [Code] deliberately. Pascal Script resolves
  identifiers in declaration order, so a helper defined after its caller is the
  same error again. Its only caller is WantsDataRemoved, reached from
  CurUninstallStepChanged -- that is, from inside the UNINSTALLER, where
  ParamCount/ParamStr enumerate unins000.exe's own command line rather than
  Setup's. That is what makes "unins000.exe /REMOVEDATA" reach the branch below. }
function CmdLineParamExists(const Value: string): Boolean;
var
  I: Integer;
begin
  Result := False;
  for I := 1 to ParamCount do
  begin
    if CompareText(ParamStr(I), Value) = 0 then
    begin
      Result := True;
      Exit;
    end;
  end;
end;

{ Stop the running stack BEFORE [Files] overwrites it.

  NOTE FOR WHOEVER EDITS THIS COMMENT: a Pascal Script comment is delimited by
  braces and they do NOT nest, so the first closing brace ENDS it -- including one
  that is merely part of a constant being described in prose. Writing the app
  constant in the usual brace form here does not document the code, it terminates
  the comment mid-sentence and the remaining words are compiled as statements:

      Error on line 185, Column 65: 'BEGIN' expected.

  That is why the install directory is spelled out in words below rather than as
  the constant. ExpandConstant does the real work in the code itself.

  Reinstalling over a live install used to abort. CloseApplications=yes asks the
  Restart Manager to shut down whatever holds a file under the install directory,
  and the Restart Manager can only close what it knows how to close: a process
  with a message loop and a window to send WM_CLOSE to. face_unlock_watchdog.exe
  has neither -- it is a windowless supervisor loop -- so RM reported "Some
  applications could not be shut down", the wizard offered Abort, and an
  unattended run (presence_monitor/updater.py launches Setup with /SILENT) took
  that Abort automatically. /SUPPRESSMSGBOXES would only have made the abort
  quieter, not rarer.

  So Setup does what the uninstaller has always done: it walks the SAME
  postinstall\tasks.psd1 through the SAME registrar and unregisters every declared
  task, which stops the processes and releases the files. A reinstall is therefore
  unregister -> copy -> register, with [Run] step 2 creating the tasks again from
  the payload that was just laid down. This script still names no task, so the
  invariant in [Run]/[UninstallRun] holds here too.

  Gated on the registrar EXISTING, which is what makes this a no-op on a first
  install: there is no postinstall\ directory under the target yet, nothing is
  running, and nothing needs stopping.

  A failed unregister is deliberately NOT fatal. Returning a non-empty string
  aborts the install, and refusing to install because a cleanup step exited
  non-zero is worse than letting CloseApplications have its turn -- which is
  exactly the behaviour that was there before this function, i.e. no worse.
  CloseApplications stays yes for that reason: this is the first line, not the
  only one. }
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Registrar: string;
  AppDir: string;
  ResultCode: Integer;
begin
  Result := '';
  AppDir := ExpandConstant('{app}');
  Registrar := AppDir + '\postinstall\register_tasks.ps1';
  if not FileExists(Registrar) then
    exit;
  Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
       '-NoProfile -ExecutionPolicy Bypass -File "' + Registrar + '"'
       + ' -Mode Installed -InstallDir "' + AppDir + '" -Action Unregister',
       '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

function InitializeUninstall(): Boolean;
begin
  // Nothing fancy -- UninstallRun handles task teardown.
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
