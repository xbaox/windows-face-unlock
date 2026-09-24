; Inno Setup script for Windows Face Unlock.
; Produces installer_output\WindowsFaceUnlock-Setup-<ver>.exe
;
; Build with:   ISCC.exe installer\installer.iss
; Or via:       python installer\build.py

#define MyAppName "Windows Face Unlock"
#define MyAppShortName "WindowsFaceUnlock"
#define MyAppVersion "0.1.1"
; Stage 8b (F-41 / D-33): this fork publishes the installer, so Programs and Features names it and
; links to it -- not the upstream project whose name and URLs were inherited here.
#define MyAppPublisher "xbaox"
#define MyAppURL "https://github.com/xbaox/windows-face-unlock"
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
; Stage 8b (F-09). Defect: the directory page let the installing admin pick ANY folder. Consequence:
; a folder ordinary users can write to would hold the DLL LogonUI loads as SYSTEM and the scripts
; Setup runs elevated. Fix: no directory page -- always Program Files (or, on an upgrade, the
; directory the previous version recorded).
DisableDirPage=yes
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
;
; CHECKED by default (Stage 7l). Signing in with your face from the lock screen IS
; the definition of done for this product, and an installer that leaves it off
; unless the user finds and ticks a box ships the tray without the feature. It
; used to carry Flags: unchecked, which is how a silent install ended with no
; Credential Provider registered at all.
;
; The provider is ADDITIVE. regsvr32 adds one more tile to LogonUI; it filters
; nothing and replaces nothing, so the PIN / password tiles stay exactly where
; they were and remain the way in whenever the face tile cannot unlock.
;
; UsePreviousTasks=yes (see [Setup]) carries the previous choice into an upgrade:
; a machine whose last install recorded cp as DESELECTED keeps it deselected, and
; this default then applies only to a first install. That is Inno's intended
; behaviour and it is kept -- a user who opted out stays opted out. For a scripted
; install that must end with the provider registered regardless of history, pass
;     /MERGETASKS="cp"
; which adds the task to whatever the previous selection was.
Name: "cp"; Description: "Register the Credential Provider (enables log-in with your face)"; \
  GroupDescription: "Optional components"

[Files]
; The whole PyInstaller output, which already includes postinstall\ (the task
; registrar and its declaration, staged there by installer/build.py step 4).
Source: "{#BuildRoot}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
; Stage 8b (F-35): the NEW registrar and its declaration, again, as dontcopy entries. PrepareToInstall
; runs before [Files], so on an upgrade the registrar under the installed directory is the OLD one,
; which has no -Action Stop; these two are extracted to a temporary folder for that step instead.
Source: "{#BuildRoot}\postinstall\register_tasks.ps1"; Flags: dontcopy
Source: "{#BuildRoot}\postinstall\tasks.psd1"; Flags: dontcopy

; There is deliberately NO [Dirs] section (Stage 8b, F-01).
;
; Defect: up to 0.1.0 this section pre-created the data directory
; (USERPROFILE\.face-unlock) with "Permissions: users-modify" -- an explicit,
; inheritable BUILTIN\Users:Modify ACE that every file below it inherited,
; including the encrypted password blob, the per-install entropy and the face
; images. Consequence: other local accounts could read and alter data the service
; trusts. Fix: the installer no longer creates or touches that directory at all.
; face_service creates it on first start and, on EVERY start, re-secures it to
; SELF / SYSTEM / Administrators (face_service/datadir.py) -- which also heals a
; machine that was installed with the old ACE, since a reinstall never removes an
; ACE Inno added. Do not reinstate an entry here: an installer-side ACL is exactly
; what broke custody.

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

; 2. Creating AND starting the scheduled tasks moved to [Code] (RegisterTasks, from
;    CurStepChanged ssPostInstall) in Stage 8b. Defects (F-33, F-07): a [Run]
;    entry cannot look at the exit code and Inno does not capture its output, so
;    a failed registrar left no trace; and the registrar ran for whoever
;    elevated rather than for the user who started Setup. Task names,
;    executables and settings still come only from postinstall\tasks.psd1.

; 3. There is deliberately NO "Launch ..." checkbox on the Finish page, and this
;    comment is what is left of the one that used to be here
;    (Flags: nowait postinstall skipifsilent).
;
;    Step 2 does not merely register the tasks, it STARTS them: register_tasks.ps1
;    ends in a Start-ScheduledTask pass over every planned task, and one of them --
;    FaceUnlock-Presence -- IS {#MyAppExeName}. The tray is therefore already
;    running by the time the Finish page is drawn, and ticking the box started a
;    SECOND one on top of it. That is not theory: it happened on the 7g acceptance
;    install.
;
;    Since Stage 8b the tray holds Local\FaceUnlockTray and a duplicate exits at
;    once (KNOWN_ISSUES #4), but the rule stands: the tray is started by its
;    scheduled task and by nothing else. Do not reinstate this entry.
;
; 4. Onboarding on the Finish page (Stage 7l): save the Windows password, then
;    enroll the face. Without both, the face tile registered in step 1 has
;    nothing to match and nothing to hand LogonUI.
;
;    These do NOT break the rule in step 3. They run {#MyAppExeName} with a
;    FLAG, and presence_monitor\__main__.py is a flag router: --set-password and
;    --enroll import the dialog or the wizard and return from main() when it
;    closes. Neither reaches the no-flag branch that starts the tray and the
;    presence monitor, so no second tray is ever started from here. The wizard
;    borrows the camera from the service through the pause_camera lease, the same
;    way the tray menu's "Enroll" item launches it.
;
;    Flags, each load-bearing:
;      postinstall        - a checkbox on the Finish page, run after Finish.
;      runasoriginaluser  - Setup is elevated, and the password is DPAPI-sealed
;                           under the account that runs the dialog; the data
;                           lives in that user's profile, not the admin's.
;      skipifsilent       - the updater reinstalls with /SILENT; an update must
;                           never pop a password dialog or a camera window.
;    Deliberately NO nowait: Setup waits for each to exit before the next, so the
;    password dialog and the wizard are never on screen at the same time.
;
;    The password entry is split in two with mutually exclusive Checks, because
;    a [Run] entry has one Description and one default state. Without a saved
;    credential it is offered checked; with one it is offered UNCHECKED as an
;    update, so a reinstall does not push the user into re-typing a password
;    that already works. CredentialsSaved only tests that the file exists; it
;    never opens it.
Filename: "{app}\{#MyAppExeName}"; Parameters: "--set-password"; \
  Description: "Save your Windows password for face sign-in"; \
  Check: not CredentialsSaved; Flags: postinstall runasoriginaluser skipifsilent
Filename: "{app}\{#MyAppExeName}"; Parameters: "--set-password"; \
  Description: "Update your saved Windows password for face sign-in"; \
  Check: CredentialsSaved; Flags: postinstall runasoriginaluser skipifsilent unchecked
Filename: "{app}\{#MyAppExeName}"; Parameters: "--enroll"; \
  Description: "Set up face recognition now"; \
  Flags: postinstall runasoriginaluser skipifsilent

[UninstallRun]
; Stop and delete the scheduled tasks first so files aren't held open. This
; walks the SAME postinstall\tasks.psd1 the install used, so a task can never be
; created by one path and left behind by the other.
;
; {sys} for the same reason as [Run] -- see the note there. The uninstaller is the
; same 32-bit binary, so an unregister through a bare name would hit the 32-bit
; regsvr32 and leave the Credential Provider registered after removal.
;
; RunOnceId on both (Stage 8b, F-34). Defect: without it Inno appends one more
; copy of each entry per upgrade to the uninstall log. Consequence: an uninstall
; after upgrades ran the registrar and regsvr32 /u once per version ever
; installed (twice in 7g). Fix: a stable RunOnceId, so each runs exactly once.
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\postinstall\register_tasks.ps1"" -Mode Installed -InstallDir ""{app}"" -Action Unregister"; \
  Flags: runhidden; RunOnceId: "FaceUnlockUnregisterTasks"
; Unregister the Credential Provider if it was installed.
Filename: "{sys}\regsvr32.exe"; Parameters: "/u /s ""{app}\credential_provider\FaceCredentialProvider.dll"""; \
  Flags: runhidden; RunOnceId: "FaceUnlockUnregisterCP"; \
  Check: FileExists(ExpandConstant('{app}\credential_provider\FaceCredentialProvider.dll'))

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

{ ---- Stage 8b (F-07): the ORIGINAL user -------------------------------------

  Defect: every per-user step used this process's own identity -- the registrar
  took the current user name, and the data directory was the USERPROFILE of
  Setup. Setup runs elevated, and when a different administrator typed their
  credentials into UAC, all of that is the administrator's.
  Consequence: on a standard-user machine the user who installed got no running
  service, the data directory landed in the wrong profile, and the uninstaller's
  "remove my data" cleaned the wrong profile.
  Fix: ExecAsOriginalUser (a process of the user who started Setup) reports its
  SID into a file; a SECOND original-user process confirms it through its exit
  code -- the file sits where other accounts can write, so its content is only a
  claim until then. The SID goes to the registrar (-UserSid) and is recorded
  under the app key so the uninstaller can find that user's profile. When it
  cannot be established, Setup falls back to its own identity (the pre-8b
  behaviour) and says so in the log. On a same-account elevation -- this
  machine -- both are the same account. }
var
  OrigUserSid: string;
  OrigUserSidResolved: Boolean;

function IsUserSid(const S: string): Boolean;
var
  I: Integer;
begin
  Result := (Length(S) > 12) and (Copy(S, 1, 9) = 'S-1-5-21-');
  if not Result then
    exit;
  for I := 1 to Length(S) do
    if not (((S[I] >= '0') and (S[I] <= '9')) or (S[I] = '-') or ((I = 1) and (S[I] = 'S'))) then
    begin
      Result := False;
      exit;
    end;
end;

function PowerShellExe(): string;
begin
  Result := ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe');
end;

function ResolveOriginalUserSid(): string;
var
  SidFile, Sid: string;
  Raw: AnsiString;
  ResultCode: Integer;
begin
  Result := '';
  SidFile := ExpandConstant('{commonappdata}') + '\WindowsFaceUnlock-setup-'
             + IntToStr(Random(2147483647)) + '.sid';
  DeleteFile(SidFile);
  if not ExecAsOriginalUser(PowerShellExe(),
      '-NoProfile -NonInteractive -Command "[Security.Principal.WindowsIdentity]::GetCurrent().User.Value'
      + ' | Set-Content -Encoding ascii -LiteralPath ''' + SidFile + '''"',
      '', SW_HIDE, ewWaitUntilTerminated, ResultCode) or (ResultCode <> 0) then
  begin
    Log('Original user: the SID query did not run (code ' + IntToStr(ResultCode) + ').');
    DeleteFile(SidFile);
    exit;
  end;
  if not LoadStringFromFile(SidFile, Raw) then
  begin
    Log('Original user: no SID file came back.');
    exit;
  end;
  DeleteFile(SidFile);
  Sid := Trim(String(Raw));
  if not IsUserSid(Sid) then
  begin
    Log('Original user: the reply is not a user SID: ' + Sid);
    exit;
  end;
  if not ExecAsOriginalUser(PowerShellExe(),
      '-NoProfile -NonInteractive -Command "if ([Security.Principal.WindowsIdentity]::GetCurrent().User.Value'
      + ' -ceq ''' + Sid + ''') { exit 0 } else { exit 3 }"',
      '', SW_HIDE, ewWaitUntilTerminated, ResultCode) or (ResultCode <> 0) then
  begin
    Log('Original user: SID ' + Sid + ' was NOT confirmed (code ' + IntToStr(ResultCode) + ').');
    exit;
  end;
  Log('Original user: SID ' + Sid + ' confirmed.');
  Result := Sid;
end;

function GetOriginalUserSid(): string;
begin
  if not OrigUserSidResolved then
  begin
    OrigUserSid := ResolveOriginalUserSid();
    OrigUserSidResolved := True;
    if OrigUserSid = '' then
      Log('Original user could not be established; per-user steps use Setup''s own account.');
  end;
  Result := OrigUserSid;
end;

// The profile directory of a SID, from ProfileList; '' when unknown.
function ProfileDirOf(const Sid: string): string;
var
  P: string;
begin
  Result := '';
  if Sid = '' then
    exit;
  if RegQueryStringValue(HKLM, 'SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList\' + Sid,
                         'ProfileImagePath', P) then
  begin
    StringChangeEx(P, '%SystemDrive%', ExpandConstant('{%SYSTEMDRIVE}'), True);
    Result := P;
  end;
end;

// The data directory face_service uses for that user: <profile>\.face-unlock. Falls back to
// Setup's own USERPROFILE when the original user is unknown (the pre-8b behaviour).
function DataDirFor(const Sid: string): string;
var
  Profile: string;
begin
  Profile := ProfileDirOf(Sid);
  if Profile = '' then
    Profile := ExpandConstant('{%USERPROFILE}');
  Result := Profile + '\.face-unlock';
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

  CloseApplications=force was the other half-fix considered, and it is recorded
  here as REJECTED rather than left for someone to rediscover. The documented
  behaviour is "Setup will force close when closing applications... Use with care
  since this may cause the user to lose unsaved work", applied to whatever holds
  files listed in Files or InstallDelete. Three reasons it is the wrong tool:

    1. It is a hard kill by another name, and it would land on the live owner of
       a camera capture -- the exact condition KNOWN_ISSUES #2 exists about, and
       the reason the registrar tries a graceful pipe shutdown FIRST. Trading an
       aborted install for a wedged Frame Server until reboot is not a trade.
    2. It is indiscriminate. It closes anything holding a file under the install
       directory, including a user application that merely has something open
       there, and the documentation's warning about unsaved work is aimed at
       precisely that.
    3. It treats the symptom. Force-closing the processes leaves their scheduled
       tasks registered, so the machine is briefly in a state the uninstaller
       never produces: no processes, live registrations. Unregistering is what
       makes install and uninstall symmetric, which is the property worth having.

  So Setup does what the uninstaller has always done: it walks the SAME
  postinstall\tasks.psd1 through the SAME registrar and unregisters every declared
  task, which stops the processes and releases the files. A reinstall is therefore
  unregister -> copy -> register, with [Run] step 2 creating the tasks again from
  the payload that was just laid down. This script still names no task, so the
  invariant in [Run]/[UninstallRun] holds here too.

  Gated on the registrar EXISTING, which is what makes this a no-op on a first
  install: there is no postinstall\ directory under the target yet, nothing is
  running, and nothing needs stopping.

  A failed unregister IS fatal, and that is a deliberate reversal of how this
  function was first written. The registrar used to exit 0 no matter what, so
  there was nothing to react to; as of the same block it re-checks after killing
  and exits non-zero, printing the surviving PIDs, when the stack is still up.
  Given a trustworthy signal, stopping is better than continuing: the documented
  effect of a non-empty Result is that Setup halts on the Preparing to Install
  page and shows that text, which names the problem, whereas continuing hands the
  question to the Restart Manager -- and RM on a windowless watchdog is the exact
  failure this function exists to remove. Under /SILENT nobody would see either
  message, but one path stops with a logged reason and the other overwrites files
  belonging to a process that is still running.

  Note the ordering this depends on, which is documented rather than assumed:
  PrepareToInstall is called BEFORE Setup checks for files being in use when
  CloseApplications is set to yes. So the stack is stopped first and the in-use
  check then finds nothing to complain about; CloseApplications stays yes as the
  second line of defence for anything outside the registrar's remit. }
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Registrar: string;
  AppDir: string;
  ResultCode: Integer;
begin
  Result := '';
  // Resolved here, once, before anything is stopped or copied (Stage 8b, F-07).
  GetOriginalUserSid();
  AppDir := ExpandConstant('{app}');
  // Stage 8b (F-35): an existing installation is recognised by its registrar, but the
  // registrar that RUNS is the new one, extracted to the temporary folder -- the old one
  // has no -Action Stop. Stop only: the registrations stay until the Register after the
  // copy overwrites them, so an aborted install no longer leaves the machine without tasks.
  if not FileExists(AppDir + '\postinstall\register_tasks.ps1') then
    exit;
  try
    ExtractTemporaryFile('register_tasks.ps1');
    ExtractTemporaryFile('tasks.psd1');
  except
    Result := 'Setup could not unpack its task registrar: ' + GetExceptionMessage;
    exit;
  end;
  Registrar := ExpandConstant('{tmp}\register_tasks.ps1');
  if not Exec(PowerShellExe(),
              '-NoProfile -ExecutionPolicy Bypass -File "' + Registrar + '"'
              + ' -Mode Installed -InstallDir "' + AppDir + '" -Action Stop',
              '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
  begin
    Result := 'Setup could not run the Face Unlock task registrar:' + #13#10
            + Registrar + #13#10#13#10
            + 'The existing installation has to be stopped before it can be replaced. '
            + 'Stop it manually, then run Setup again.';
    exit;
  end;
  if ResultCode <> 0 then
  begin
    Result := 'The installed Face Unlock is still running, so Setup will not overwrite it.'
            + #13#10#13#10
            + 'The task registrar exited with code ' + IntToStr(ResultCode)
            + ' after trying twice to stop the stack; it lists the surviving process IDs in its '
            + 'own output.' + #13#10#13#10
            + 'Stop it and run Setup again:' + #13#10
            + '  powershell -NoProfile -ExecutionPolicy Bypass -File "' + AppDir
            + '\postinstall\register_tasks.ps1" -Mode Installed -Action Unregister';
    exit;
  end;
end;

{ Stage 8b (F-33, F-07): register and start the tasks for the original user, and
  LOOK at the result. The registrar transcribes itself to the logs folder under
  the install directory; this adds one line to the Setup log either way, and an
  interactive install also gets a message box on failure. }
procedure RegisterTasks();
var
  Params: string;
  ResultCode: Integer;
begin
  WizardForm.StatusLabel.Caption := 'Registering scheduled tasks...';
  Params := '-NoProfile -ExecutionPolicy Bypass -File "'
            + ExpandConstant('{app}\postinstall\register_tasks.ps1') + '"'
            + ' -Mode Installed -InstallDir "' + ExpandConstant('{app}') + '" -Action Register';
  if GetOriginalUserSid() <> '' then
    Params := Params + ' -UserSid ' + GetOriginalUserSid();
  if not Exec(PowerShellExe(), Params, '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
    ResultCode := -1;
  if ResultCode = 0 then
    Log('Face Unlock task registrar: OK')
  else
  begin
    Log('Face Unlock task registrar FAILED with exit code ' + IntToStr(ResultCode) + '; see '
        + ExpandConstant('{app}\logs\register_tasks.log'));
    if not WizardSilent() then
      MsgBox('Face Unlock could not register its background tasks (exit code '
             + IntToStr(ResultCode) + ').' + #13#10#13#10
             + 'Face sign-in will not work until they are registered. Details: '
             + ExpandConstant('{app}\logs\register_tasks.log'), mbError, MB_OK);
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    // Recorded for the uninstaller, which has no original-user process to ask (F-07).
    if GetOriginalUserSid() <> '' then
      RegWriteStringValue(HKLM, 'Software\{#MyAppShortName}', 'OriginalUserSid', GetOriginalUserSid());
    RegisterTasks();
  end;
end;

{ Check for the two password entries in [Run] step 4: the ORIGINAL user's data
  directory (Stage 8b, F-07) -- the profile the runasoriginaluser dialog writes
  to -- which is what face_service/config.py resolves as Path.home() for that
  user. Existence only; the DPAPI blob itself is never opened here. }
function CredentialsSaved(): Boolean;
begin
  Result := FileExists(DataDirFor(GetOriginalUserSid()) + '\credentials.bin');
end;

var
  UninstallUserSid: string;

function InitializeUninstall(): Boolean;
begin
  // Task teardown is [UninstallRun]. The user whose data this is was recorded at install
  // time (Stage 8b, F-07); read it now, before the app key is deleted with the rest.
  if not RegQueryStringValue(HKLM, 'Software\{#MyAppShortName}', 'OriginalUserSid', UninstallUserSid)
     or not IsUserSid(UninstallUserSid) then
    UninstallUserSid := '';
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

{ Stage 8b (F-02). Defect: the data directory is removed ELEVATED with
  rmdir /S /Q, and up to 0.1.0 other accounts could write inside it.
  Consequence: whether the delete stays inside the directory depended only on how
  rmdir treats a link. Fix, defence in depth: the directory itself must not be a
  reparse point (then nothing is deleted and the uninstall log says why), and below
  it rmdir /S removes a junction as a link without entering it -- measured on this
  Windows build in 8b (ps51-junction-test.txt: target survived). $400 is
  FILE_ATTRIBUTE_REPARSE_POINT; FindFirst on a path without a wildcard describes
  that entry itself, not its target. }
function IsReparsePoint(const Path: string): Boolean;
var
  FindRec: TFindRec;
begin
  Result := False;
  if FindFirst(Path, FindRec) then
  begin
    try
      Result := (FindRec.Attributes and $400) <> 0;
    finally
      FindClose(FindRec);
    end;
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: string;
  ResultCode: Integer;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    // The recorded original user's profile (F-07); Setup's own profile for an install
    // made before 8b, which recorded nothing.
    DataDir := DataDirFor(UninstallUserSid);
    if DirExists(DataDir) then
    begin
      if WantsDataRemoved(DataDir) then
      begin
        if IsReparsePoint(DataDir) then
          Log('Data directory is a reparse point, not removed: ' + DataDir)
        else
          Exec(ExpandConstant('{cmd}'), '/C rmdir /S /Q "' + DataDir + '"',
               '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
      end;
    end;
  end;
end;
