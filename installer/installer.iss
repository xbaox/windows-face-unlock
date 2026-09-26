; Inno Setup script for Windows Face Unlock.
; Produces installer_output\WindowsFaceUnlock-Setup-<version>-<variant>.exe
;
; Build with:   python installer\build.py --variant cpu   (or gpu)
; build.py passes the version, the variant and the model pins (face_service/model_pins.py, the one
; source) as /D defines; this script has no pin of its own.

#define MyAppName "Windows Face Unlock"
#define MyAppShortName "WindowsFaceUnlock"
#ifndef MyAppVersion
  #define MyAppVersion "0.2.0"
#endif
#ifndef Variant
  #define Variant "cpu"
#endif
#define MyAppPublisher "xbaox"
#define MyAppURL "https://github.com/xbaox/windows-face-unlock"
#define MyAppExeName "face_unlock_tray.exe"
; The Credential Provider class (credential_provider/guid.h).
#define CPClsid "{{8414D7B6-D536-461B-B31B-ADF77B3A8974}"
#ifndef BuildRoot
  #define BuildRoot "..\dist\WindowsFaceUnlock"
#endif
#ifndef BuffaloURL
  #error The model pins come from installer/build.py (/DBuffaloURL, /DBuffaloSHA256, /DBuffaloBytes, /DModel1..5, /DModelSHA1..5).
#endif

[Setup]
AppId={{2F7A9B14-3C31-4B1E-9AB9-5E0D1B02B6A7}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}/issues
AppUpdatesURL={#MyAppURL}/releases
AppCopyright=Copyright (c) 2026 Cao Chí Tâm; modifications (c) 2026 xbaox. MIT License.
VersionInfoVersion={#MyAppVersion}.0
VersionInfoCompany={#MyAppPublisher}
VersionInfoProductName={#MyAppName}
VersionInfoDescription={#MyAppName} Setup ({#Variant})
; Stage 9 (act 9b R17, F-87): ALWAYS Program Files\WindowsFaceUnlock -- no directory page, no reuse
; of a previously recorded directory, and a /DIR= elsewhere is refused in InitializeSetup. The
; directory holds the DLL LogonUI loads as SYSTEM, so it must be writable by administrators only;
; that is checked before the DLL is registered.
DefaultDirName={autopf}\{#MyAppShortName}
DisableDirPage=yes
UsePreviousAppDir=no
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=..\installer_output
OutputBaseFilename=WindowsFaceUnlock-Setup-{#MyAppVersion}-{#Variant}
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2/ultra64
SolidCompression=yes
; buffalo_l.zip is a zip archive: ExtractArchive needs the full 7-Zip engine for it.
ArchiveExtraction=full
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64os
ArchitecturesAllowed=x64os
; R17: Windows 10 22H2 (19045) is the hard floor; below 11 24H2 (26100) Setup warns.
MinVersion=10.0.19045
PrivilegesRequired=admin
CloseApplications=yes
; The tray is started by its scheduled task, never by the Restart Manager (D-107).
RestartApplications=no
UsePreviousTasks=yes
#ifdef SignToolName
; R18: only when build.py has signing credentials; otherwise the build says loudly it is unsigned.
SignTool={#SignToolName}
SignedUninstaller=yes
#endif

[Languages]
; English and Russian, like the app (R16). Inno's own texts come from its message files, ours from
; lang\*.isl ([CustomMessages]); Setup picks the one that matches the Windows display language.
Name: "english"; MessagesFile: "compiler:Default.isl,lang\en.isl"
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl,lang\ru.isl"

[Tasks]
; Checked by default: signing in with your face IS the product. The provider is additive -- PIN and
; password tiles stay. UsePreviousTasks keeps an opt-out across upgrades; unticking it on an upgrade
; unregisters the provider (F-212). /MERGETASKS="cp" forces it for a scripted install.
Name: "cp"; Description: "{cm:TaskCP}"; GroupDescription: "{cm:TaskGroup}"

[InstallDelete]
; Leftovers of 0.1.x that 0.2.0 no longer ships: the PowerShell registrar, the CP dev script and the
; bundled model pack (now downloaded into {app}\models).
Type: filesandordirs; Name: "{app}\postinstall"
Type: files; Name: "{app}\credential_provider\register.ps1"
Type: filesandordirs; Name: "{app}\_internal\insightface_home"

[Files]
; The whole PyInstaller output except the CP DLL, which gets its own entry.
Source: "{#BuildRoot}\*"; Excludes: "credential_provider\FaceCredentialProvider.dll"; DestDir: "{app}"; \
  Flags: recursesubdirs createallsubdirs ignoreversion uninsrestartdelete
; The CP DLL may be loaded by LogonUI right now: replaced at the next restart if it is in use, and
; removed at the next restart by the uninstaller if it is still loaded (F-217).
Source: "{#BuildRoot}\credential_provider\FaceCredentialProvider.dll"; DestDir: "{app}\credential_provider"; \
  Flags: ignoreversion restartreplace uninsrestartdelete skipifsourcedoesntexist

; There is deliberately NO [Dirs] section (Stage 8b, F-01): the installer never creates or touches
; the user's data directory; face_service creates and secures it.

[Icons]
; R14: the Start-menu shortcut carries the tray's AppUserModelID -- Windows shows the tray's toasts
; only for an app ID it knows from such a shortcut (presence_monitor\toast.py, APP_ID).
Name: "{group}\{cm:IconTray}"; Filename: "{app}\{#MyAppExeName}"; AppUserModelID: "WindowsFaceUnlock.Tray"
Name: "{group}\{cm:IconUninstall}"; Filename: "{uninstallexe}"

[Registry]
Root: HKLM; Subkey: "Software\{#MyAppShortName}"; ValueType: string; ValueName: "InstallLocation"; ValueData: "{app}"; Flags: uninsdeletekey
Root: HKLM; Subkey: "Software\{#MyAppShortName}"; ValueType: string; ValueName: "Version"; ValueData: "{#MyAppVersion}"
Root: HKLM; Subkey: "Software\{#MyAppShortName}"; ValueType: string; ValueName: "Variant"; ValueData: "{#Variant}"
; R17 / F-229: the provider's keys are removed by the uninstaller even when regsvr32 /u could not
; run (the DLL already gone). dontcreatekey: Setup never writes them itself -- the DLL does.
Root: HKLM64; Subkey: "SOFTWARE\Microsoft\Windows\CurrentVersion\Authentication\Credential Providers\{#CPClsid}"; Flags: uninsdeletekey dontcreatekey
Root: HKLM64; Subkey: "SOFTWARE\Classes\CLSID\{#CPClsid}"; Flags: uninsdeletekey dontcreatekey

[Run]
; Onboarding on the Finish page: save the Windows password, then set up the face. They run the tray
; exe with a FLAG (a flag router, never a second tray), as the original user (the password is sealed
; for that account and the data lives in that profile), never during a silent (update) install, and
; one after the other. Each is offered checked only when it is still missing (F-192).
Filename: "{app}\{#MyAppExeName}"; Parameters: "--set-password"; Description: "{cm:RunSavePassword}"; \
  Check: not CredentialsSaved; Flags: postinstall runasoriginaluser skipifsilent
Filename: "{app}\{#MyAppExeName}"; Parameters: "--set-password"; Description: "{cm:RunUpdatePassword}"; \
  Check: CredentialsSaved; Flags: postinstall runasoriginaluser skipifsilent unchecked
Filename: "{app}\{#MyAppExeName}"; Parameters: "--enroll"; Description: "{cm:RunEnroll}"; \
  Check: not EnrollmentExists; Flags: postinstall runasoriginaluser skipifsilent
Filename: "{app}\{#MyAppExeName}"; Parameters: "--enroll"; Description: "{cm:RunReEnroll}"; \
  Check: EnrollmentExists; Flags: postinstall runasoriginaluser skipifsilent unchecked

[UninstallRun]
; 1. Stop the stack and remove the tasks through the product exe (Task Scheduler over COM, no
;    PowerShell; R17). 2. Unregister the provider; its keys are removed by [Registry] as a fallback.
; {sys}: Setup is 32-bit; a bare regsvr32 would be the 32-bit one. Each runs once (RunOnceId).
Filename: "{app}\{#MyAppExeName}"; Parameters: "--unregister"; Flags: runhidden waituntilterminated; \
  RunOnceId: "FaceUnlockUnregisterTasks"
Filename: "{sys}\regsvr32.exe"; Parameters: "/u /s ""{app}\credential_provider\FaceCredentialProvider.dll"""; \
  Flags: runhidden waituntilterminated; RunOnceId: "FaceUnlockUnregisterCP"; \
  Check: FileExists(ExpandConstant('{app}\credential_provider\FaceCredentialProvider.dll'))

[UninstallDelete]
; Only what Setup and the product put there -- never a recursive delete of the install directory
; itself, which could be shared with something else (F-209).
Type: filesandordirs; Name: "{app}\models\buffalo_l"
Type: dirifempty; Name: "{app}\models"
Type: filesandordirs; Name: "{app}\logs"
Type: dirifempty; Name: "{app}\credential_provider"
Type: dirifempty; Name: "{app}"

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

{ ---- Stage 9 (R1): the OWNER --------------------------------------------------

  The product is single-user. Its owner is the user of the ACTIVE CONSOLE SESSION
  when Setup runs -- not the account Setup was elevated with, and never SYSTEM or
  a service account. Setup records the owner's SID in
  HKLM\Software\WindowsFaceUnlock\OriginalUserSid (writable by administrators
  only); the service refuses to work for anyone else and the lock-screen tile is
  shown to the owner only. The Ready page names the owner.

  Before Stage 9 (F-07 / F-193 / F-213) the "original user" was whoever started
  Setup, resolved through two PowerShell processes; when that failed Setup fell
  back to its OWN account and left a stale SID from an earlier install in place.
  Now an owner that cannot be established stops Setup, and the value is always
  rewritten.

  Silent installs: /OWNER=<DOMAIN\user or SID> names the owner explicitly. When a
  DIFFERENT owner is already recorded, an interactive Setup asks before replacing
  it; a silent one refuses (exit code 1) unless /FORCEOWNER is given.

  Accepted SIDs: S-1-5-21-* (local / domain) and S-1-12-1-* (Entra ID). }
const
  WTS_USER_NAME = 5;
  WTS_DOMAIN_NAME = 7;
  NO_CONSOLE_SESSION = $FFFFFFFF;

function WTSGetActiveConsoleSessionId(): Cardinal;
  external 'WTSGetActiveConsoleSessionId@kernel32.dll stdcall';
function WTSQuerySessionInformationW(hServer: Cardinal; SessionId: Cardinal; InfoClass: Integer;
  var Buffer: Cardinal; var BytesReturned: Cardinal): Boolean;
  external 'WTSQuerySessionInformationW@wtsapi32.dll stdcall';
procedure WTSFreeMemory(Memory: Cardinal);
  external 'WTSFreeMemory@wtsapi32.dll stdcall';
function lstrlenW(Src: Cardinal): Integer;
  external 'lstrlenW@kernel32.dll stdcall';
function lstrcpynW(Dest: string; Src: Cardinal; MaxLen: Integer): Cardinal;
  external 'lstrcpynW@kernel32.dll stdcall';
function LookupAccountNameW(SystemName: Cardinal; AccountName: string; Sid: AnsiString;
  var SidSize: Cardinal; Domain: string; var DomainSize: Cardinal; var Use: Integer): Boolean;
  external 'LookupAccountNameW@advapi32.dll stdcall';
function LookupAccountSidW(SystemName: Cardinal; Sid: Cardinal; Name: string;
  var NameSize: Cardinal; Domain: string; var DomainSize: Cardinal; var Use: Integer): Boolean;
  external 'LookupAccountSidW@advapi32.dll stdcall';
function ConvertSidToStringSidW(Sid: AnsiString; var StringSid: Cardinal): Boolean;
  external 'ConvertSidToStringSidW@advapi32.dll stdcall';
function ConvertStringSidToSidW(StringSid: string; var Sid: Cardinal): Boolean;
  external 'ConvertStringSidToSidW@advapi32.dll stdcall';
function LocalFree(Mem: Cardinal): Cardinal;
  external 'LocalFree@kernel32.dll stdcall';

var
  OwnerSid: string;
  OwnerName: string;
  OwnerResolved: Boolean;

// A person's SID: S-1-5-21-a-b-c-rid (local / domain) or S-1-12-1-a-b-c-d (Entra ID).
function IsUserSid(const S: string): Boolean;
var
  I, Dashes: Integer;
begin
  Result := ((Copy(S, 1, 9) = 'S-1-5-21-') or (Copy(S, 1, 9) = 'S-1-12-1-')) and (Length(S) > 10);
  if not Result then
    exit;
  Dashes := 0;
  for I := 3 to Length(S) do
  begin
    if S[I] = '-' then
      Dashes := Dashes + 1
    else if not ((S[I] >= '0') and (S[I] <= '9')) then
    begin
      Result := False;
      exit;
    end;
  end;
  Result := Dashes >= 6;
end;

function PtrToString(P: Cardinal): string;
var
  N: Integer;
begin
  Result := '';
  if P = 0 then
    exit;
  N := lstrlenW(P);
  if N <= 0 then
    exit;
  SetLength(Result, N);
  lstrcpynW(Result, P, N + 1);
end;

function ConsoleSessionString(SessionId: Cardinal; InfoClass: Integer): string;
var
  Buf, Bytes: Cardinal;
begin
  Result := '';
  Buf := 0;
  Bytes := 0;
  if WTSQuerySessionInformationW(0, SessionId, InfoClass, Buf, Bytes) then
  begin
    Result := PtrToString(Buf);
    WTSFreeMemory(Buf);
  end;
end;

// DOMAIN\user (or a bare user) -> string SID; '' when it cannot be resolved.
function SidOfAccount(const Account: string): string;
var
  Sid: AnsiString;
  SidSize, DomSize, StrPtr: Cardinal;
  Dom: string;
  Use: Integer;
begin
  Result := '';
  SidSize := 256;
  DomSize := 256;
  Sid := StringOfChar(#0, SidSize);
  Dom := StringOfChar(#0, DomSize);
  if not LookupAccountNameW(0, Account, Sid, SidSize, Dom, DomSize, Use) then
    exit;
  StrPtr := 0;
  if ConvertSidToStringSidW(Sid, StrPtr) then
  begin
    Result := PtrToString(StrPtr);
    LocalFree(StrPtr);
  end;
end;

// String SID -> DOMAIN\user for display; the SID itself when it cannot be resolved.
function AccountOfSid(const S: string): string;
var
  Bin, NameSize, DomSize: Cardinal;
  Name, Dom: string;
  Use: Integer;
begin
  Result := S;
  Bin := 0;
  if not ConvertStringSidToSidW(S, Bin) then
    exit;
  NameSize := 256;
  DomSize := 256;
  Name := StringOfChar(#0, NameSize);
  Dom := StringOfChar(#0, DomSize);
  if LookupAccountSidW(0, Bin, Name, NameSize, Dom, DomSize, Use) then
    Result := Copy(Dom, 1, DomSize) + '\' + Copy(Name, 1, NameSize);
  LocalFree(Bin);
end;

// The value of a /NAME=value switch on Setup's command line ('' when absent).
function CmdLineValue(const Name: string): string;
var
  I: Integer;
  P: string;
begin
  Result := '';
  for I := 1 to ParamCount do
  begin
    P := ParamStr(I);
    if CompareText(Copy(P, 1, Length(Name) + 1), Name + '=') = 0 then
    begin
      Result := Trim(Copy(P, Length(Name) + 2, MaxInt));
      exit;
    end;
  end;
end;

// Establish the owner: /OWNER= if given, else the user of the active console session.
// Returns '' and sets Why when there is none (Setup then stops).
function ResolveOwner(var Why: string): string;
var
  Session: Cardinal;
  Arg, User, Dom: string;
begin
  Result := '';
  Arg := CmdLineValue('/OWNER');
  if Arg <> '' then
  begin
    if IsUserSid(Arg) then
      Result := Arg
    else
      Result := SidOfAccount(Arg);
    if not IsUserSid(Result) then
    begin
      Why := FmtMessage(CustomMessage('WhyOwnerArg'), [Arg]);
      Result := '';
    end;
    exit;
  end;
  Session := WTSGetActiveConsoleSessionId();
  if Session = NO_CONSOLE_SESSION then
  begin
    Why := CustomMessage('WhyNoConsole');
    exit;
  end;
  User := ConsoleSessionString(Session, WTS_USER_NAME);
  Dom := ConsoleSessionString(Session, WTS_DOMAIN_NAME);
  if User = '' then
  begin
    Why := CustomMessage('WhyNoConsoleUser');
    exit;
  end;
  if Dom <> '' then
    Result := SidOfAccount(Dom + '\' + User)
  else
    Result := SidOfAccount(User);
  if not IsUserSid(Result) then
  begin
    Why := FmtMessage(CustomMessage('WhyUnresolved'), [Dom + '\' + User, Result]);
    Result := '';
  end;
end;

function GetOwnerSid(): string;
var
  Why: string;
begin
  if not OwnerResolved then
  begin
    OwnerSid := ResolveOwner(Why);
    OwnerResolved := True;
    if OwnerSid <> '' then
    begin
      OwnerName := AccountOfSid(OwnerSid);
      Log('Owner: ' + OwnerName + ' (' + OwnerSid + ')');
    end else
      Log('Owner could not be established: ' + Why);
  end;
  Result := OwnerSid;
end;


{ ---- Stage 9 (R7): the recognition models -----------------------------------------------------

  buffalo_l is not redistributed (decision 9-02). When models\buffalo_l in the program folder lacks
  any of the five pinned files, Setup obtains the official buffalo_l.zip -- downloaded from the source
  insightface itself uses, or a local copy (/MODELZIP=<path> or the file chooser) -- checks its
  size and SHA-256 against the pins, and unpacks exactly the five pinned files, each checked again.
  The InsightFace terms are shown first and must be accepted; a silent install needs
  /ACCEPTMODELLICENSE and fails closed without it. }
const
  MODEL_COUNT = 5;

var
  ModelsNeeded: Boolean;
  ModelZip: string;
  ModelsPage: TWizardPage;
  ModelsTerms: TNewMemo;
  RadioDownload, RadioLocal: TNewRadioButton;
  LocalZipEdit: TNewEdit;
  BrowseButton: TNewButton;
  AcceptBox: TNewCheckBox;
  DownloadPage: TDownloadWizardPage;
  StackStopped, InstallDone: Boolean;
  FailCode: Integer;

function ExpectedAppDir(): string;
begin
  Result := ExpandConstant('{autopf}') + '\{#MyAppShortName}';
end;

function ModelName(I: Integer): string;
begin
  case I of
    1: Result := '{#Model1}';
    2: Result := '{#Model2}';
    3: Result := '{#Model3}';
    4: Result := '{#Model4}';
  else
    Result := '{#Model5}';
  end;
end;

function ModelSha(I: Integer): string;
begin
  case I of
    1: Result := '{#ModelSHA1}';
    2: Result := '{#ModelSHA2}';
    3: Result := '{#ModelSHA3}';
    4: Result := '{#ModelSHA4}';
  else
    Result := '{#ModelSHA5}';
  end;
end;

function Sha256Of(const Path: string): string;
begin
  Result := '';
  try
    Result := Lowercase(GetSHA256OfFile(Path));
  except
    Log('SHA-256 of ' + Path + ' failed: ' + GetExceptionMessage);
  end;
end;

// The five pinned files are present in Dir with their pinned hashes.
function ModelsValidIn(const Dir: string): Boolean;
var
  I: Integer;
begin
  Result := False;
  for I := 1 to MODEL_COUNT do
    if Sha256Of(Dir + '\' + ModelName(I)) <> ModelSha(I) then
      exit;
  Result := True;
end;

// A buffalo_l.zip is the pinned one: its size and SHA-256.
function ZipValid(const Path: string): Boolean;
var
  Size: Integer;
begin
  Result := False;
  if not FileExists(Path) then
    exit;
  if not FileSize(Path, Size) or (Size <> {#BuffaloBytes}) then
  begin
    Log('buffalo_l.zip size mismatch: ' + Path);
    exit;
  end;
  Result := Sha256Of(Path) = '{#BuffaloSHA256}';
  if not Result then
    Log('buffalo_l.zip SHA-256 mismatch: ' + Path);
end;

{ ---- Stage 9 (R17): stopping / restarting the installed stack without PowerShell --------------

  The Task Scheduler and WMI over COM. Tasks and processes are "ours" by PATH: the task's action or
  the process image lies inside the install directory -- never by a name prefix (F-239), and in any
  session (F-227). }
function PathUnder(const Path, Dir: string): Boolean;
begin
  Result := (Path <> '') and (CompareText(Copy(Path, 1, Length(Dir) + 1), Dir + '\') = 0);
end;

function TaskActionPath(const Task: Variant): string;
var
  Acts, A: Variant;
begin
  Result := '';
  try
    Acts := Task.Definition.Actions;
    if Acts.Count >= 1 then
    begin
      A := Acts.Item(1);
      Result := A.Path;
    end;
  except
    Result := '';
  end;
end;

// Stop (Run=False) or start (Run=True) every task whose action is inside Dir. Returns how many.
function ForOurTasks(const Dir: string; Run: Boolean): Integer;
var
  Svc, Folder, Tasks, T: Variant;
  I: Integer;
begin
  Result := 0;
  try
    Svc := CreateOleObject('Schedule.Service');
    Svc.Connect();
    Folder := Svc.GetFolder('\');
    Tasks := Folder.GetTasks(1);
    for I := 1 to Tasks.Count do
    begin
      T := Tasks.Item(I);
      if PathUnder(TaskActionPath(T), Dir) then
      begin
        Result := Result + 1;
        try
          if Run then
            T.Run('')
          else
            T.Stop(0);
        except
          Log('task ' + T.Name + ': ' + GetExceptionMessage);
        end;
      end;
    end;
  except
    Log('Task Scheduler not reachable: ' + GetExceptionMessage);
  end;
end;

// Count (and with Kill, terminate) the processes of this install, in every session.
function StackProcesses(const Dir: string; Kill: Boolean): Integer;
var
  Loc, Wmi, Procs, P: Variant;
  I: Integer;
  Path: string;
begin
  Result := 0;
  try
    Loc := CreateOleObject('WbemScripting.SWbemLocator');
    Wmi := Loc.ConnectServer('.', 'root\CIMV2');
    Procs := Wmi.ExecQuery('SELECT ProcessId, ExecutablePath FROM Win32_Process WHERE '
      + 'Name = ''face_service.exe'' OR Name = ''face_unlock_tray.exe'' OR '
      + 'Name = ''face_unlock_watchdog.exe''');
    for I := 0 to Procs.Count - 1 do
    begin
      P := Procs.ItemIndex(I);
      Path := '';
      try
        Path := P.ExecutablePath;
      except
        Path := '';
      end;
      if PathUnder(Path, Dir) then
      begin
        Result := Result + 1;
        if Kill then
          try
            P.Terminate(0);
          except
            Log('terminate failed: ' + GetExceptionMessage);
          end;
      end;
    end;
  except
    Log('process scan failed: ' + GetExceptionMessage);
  end;
end;

// Graceful first (the service releases its camera and marks the stop deliberate), as the ORIGINAL
// user -- the pipe admits its owner only (F-228) -- then the scheduler, then a bounded kill.
function StopStack(const Dir: string): Integer;
var
  ResultCode, Waited: Integer;
begin
  if FileExists(Dir + '\{#MyAppExeName}') then
    try
      ExecAsOriginalUser(Dir + '\{#MyAppExeName}', '--pipe-shutdown', Dir, SW_HIDE,
                         ewWaitUntilTerminated, ResultCode);
      Log('graceful shutdown request: exit ' + IntToStr(ResultCode));
    except
      Log('graceful shutdown request not possible: ' + GetExceptionMessage);    // F-214
    end;
  ForOurTasks(Dir, False);
  StackProcesses(Dir, True);
  Waited := 0;
  Result := StackProcesses(Dir, False);
  while (Result > 0) and (Waited < 10000) do
  begin
    Sleep(250);
    Waited := Waited + 250;
    Result := StackProcesses(Dir, False);
  end;
end;

// InitializeSetup: the owner (R1), the Windows version (R17), the directory (R17), the models (R7).
function InitializeSetup(): Boolean;
var
  Why, Recorded, DirArg: string;
  Ver: TWindowsVersion;
begin
  Result := False;
  FailCode := 0;
  OwnerSid := ResolveOwner(Why);
  OwnerResolved := True;
  if OwnerSid = '' then
  begin
    Log('Setup stops: ' + Why);
    if not WizardSilent() then
      MsgBox(FmtMessage(CustomMessage('CannotInstall'), [Why]), mbCriticalError, MB_OK);
    exit;
  end;
  OwnerName := AccountOfSid(OwnerSid);
  Log('Owner: ' + OwnerName + ' (' + OwnerSid + ')');
  if RegQueryStringValue(HKLM, 'Software\{#MyAppShortName}', 'OriginalUserSid', Recorded)
     and IsUserSid(Recorded) and (CompareText(Recorded, OwnerSid) <> 0) then
  begin
    if WizardSilent() then
    begin
      if not CmdLineParamExists('/FORCEOWNER') then
      begin
        Log('Setup stops: Face Unlock belongs to ' + AccountOfSid(Recorded) + ' (' + Recorded
            + '); installing for ' + OwnerName + ' needs /FORCEOWNER.');
        exit;
      end;
      Log('Owner changes from ' + Recorded + ' to ' + OwnerSid + ' (/FORCEOWNER).');
    end else if MsgBox(FmtMessage(CustomMessage('BelongsToOther'), [AccountOfSid(Recorded), OwnerName]),
                       mbConfirmation, MB_YESNO) <> IDYES then
    begin
      Log('Setup stops: the user kept the recorded owner ' + Recorded + '.');
      exit;
    end;
  end;

  // R17: /DIR= outside Program Files\WindowsFaceUnlock is refused (the directory is fixed).
  DirArg := CmdLineValue('/DIR');
  if (DirArg <> '') and (CompareText(RemoveBackslashUnlessRoot(DirArg), ExpectedAppDir()) <> 0) then
  begin
    Log('Setup stops: /DIR=' + DirArg + ' is not ' + ExpectedAppDir());
    if not WizardSilent() then
      MsgBox(FmtMessage(CustomMessage('DirRefused'), [ExpectedAppDir()]), mbCriticalError, MB_OK);
    exit;
  end;

  // R17: officially supported are Windows 11 24H2 and 25H2 (build 26100+); older builds are warned.
  GetWindowsVersionEx(Ver);
  if (Ver.Build < 26100) and not WizardSilent() then
    MsgBox(CustomMessage('WinOld'), mbInformation, MB_OK);
  if Ver.Build < 26100 then
    Log('Windows build ' + IntToStr(Ver.Build) + ' is below 26100: not officially supported');

  // R7: are the models already there (an upgrade)? Otherwise consent + a source are required.
  ModelsNeeded := not ModelsValidIn(ExpectedAppDir() + '\models\buffalo_l');
  ModelZip := '';
  if ModelsNeeded then
  begin
    Log('recognition models: needed');
    if CmdLineValue('/MODELZIP') <> '' then
    begin
      if not ZipValid(CmdLineValue('/MODELZIP')) then
      begin
        Log('Setup stops: /MODELZIP is not the pinned buffalo_l.zip');
        if not WizardSilent() then
          MsgBox(CustomMessage('ModelsBadFile'), mbCriticalError, MB_OK);
        exit;
      end;
      ModelZip := CmdLineValue('/MODELZIP');
    end;
    if WizardSilent() and not CmdLineParamExists('/ACCEPTMODELLICENSE') then
    begin
      // Fails closed: no consent, no download, no install.
      Log('Setup stops: a silent install that has to obtain the InsightFace models needs '
          + '/ACCEPTMODELLICENSE (the models are licensed for non-commercial research use only).');
      exit;
    end;
  end else
    Log('recognition models: present and pinned');
  Result := True;
end;

procedure BrowseClick(Sender: TObject);
var
  FileName: string;
begin
  FileName := LocalZipEdit.Text;
  if GetOpenFileName(CustomMessage('ModelsBrowseTitle'), FileName, '', 'buffalo_l.zip|buffalo_l.zip|*.zip|*.zip', 'zip') then
  begin
    LocalZipEdit.Text := FileName;
    RadioLocal.Checked := True;
  end;
end;

procedure InitializeWizard();
var
  Top: Integer;
begin
  ModelsPage := CreateCustomPage(wpSelectTasks, CustomMessage('ModelsTitle'), CustomMessage('ModelsDesc'));
  ModelsTerms := TNewMemo.Create(ModelsPage);
  ModelsTerms.Parent := ModelsPage.Surface;
  ModelsTerms.Left := 0;
  ModelsTerms.Top := 0;
  ModelsTerms.Width := ModelsPage.SurfaceWidth;
  ModelsTerms.Height := ScaleY(110);
  ModelsTerms.ScrollBars := ssVertical;
  ModelsTerms.ReadOnly := True;
  ModelsTerms.Text := CustomMessage('ModelsTerms');
  Top := ModelsTerms.Top + ModelsTerms.Height + ScaleY(8);
  AcceptBox := TNewCheckBox.Create(ModelsPage);
  AcceptBox.Parent := ModelsPage.Surface;
  AcceptBox.Top := Top;
  AcceptBox.Width := ModelsPage.SurfaceWidth;
  AcceptBox.Height := ScaleY(34);
  AcceptBox.Caption := CustomMessage('ModelsAccept');
  AcceptBox.Checked := CmdLineParamExists('/ACCEPTMODELLICENSE');
  Top := Top + AcceptBox.Height + ScaleY(6);
  RadioDownload := TNewRadioButton.Create(ModelsPage);
  RadioDownload.Parent := ModelsPage.Surface;
  RadioDownload.Top := Top;
  RadioDownload.Width := ModelsPage.SurfaceWidth;
  RadioDownload.Caption := CustomMessage('ModelsDownload');
  RadioDownload.Checked := ModelZip = '';
  Top := Top + ScaleY(22);
  RadioLocal := TNewRadioButton.Create(ModelsPage);
  RadioLocal.Parent := ModelsPage.Surface;
  RadioLocal.Top := Top;
  RadioLocal.Width := ModelsPage.SurfaceWidth;
  RadioLocal.Caption := CustomMessage('ModelsLocal');
  RadioLocal.Checked := ModelZip <> '';
  Top := Top + ScaleY(22);
  LocalZipEdit := TNewEdit.Create(ModelsPage);
  LocalZipEdit.Parent := ModelsPage.Surface;
  LocalZipEdit.Top := Top;
  LocalZipEdit.Left := ScaleX(18);
  LocalZipEdit.Width := ModelsPage.SurfaceWidth - ScaleX(110);
  LocalZipEdit.Text := ModelZip;
  BrowseButton := TNewButton.Create(ModelsPage);
  BrowseButton.Parent := ModelsPage.Surface;
  BrowseButton.Top := Top - ScaleY(1);
  BrowseButton.Left := LocalZipEdit.Left + LocalZipEdit.Width + ScaleX(8);
  BrowseButton.Width := ScaleX(80);
  BrowseButton.Height := ScaleY(23);
  BrowseButton.Caption := CustomMessage('ModelsBrowse');
  BrowseButton.OnClick := @BrowseClick;

  DownloadPage := CreateDownloadPage(CustomMessage('ModelsTitle'), CustomMessage('ModelsDownloading'), nil);
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := (PageID = ModelsPage.ID) and not ModelsNeeded;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if (CurPageID = ModelsPage.ID) and ModelsNeeded then
  begin
    if not AcceptBox.Checked then
    begin
      MsgBox(CustomMessage('ModelsNeedAccept'), mbError, MB_OK);
      Result := False;
      exit;
    end;
    if RadioLocal.Checked then
    begin
      if not ZipValid(LocalZipEdit.Text) then
      begin
        MsgBox(CustomMessage('ModelsBadFile'), mbError, MB_OK);
        Result := False;
        exit;
      end;
      ModelZip := LocalZipEdit.Text;
    end else
      ModelZip := '';
  end;
  // Interactive download with a progress page, after the Ready page.
  if (CurPageID = wpReady) and ModelsNeeded and (ModelZip = '') then
  begin
    DownloadPage.Clear;
    DownloadPage.Add('{#BuffaloURL}', 'buffalo_l.zip', '{#BuffaloSHA256}');
    DownloadPage.Show;
    try
      try
        DownloadPage.Download;          // verifies the SHA-256 itself
        ModelZip := ExpandConstant('{tmp}\buffalo_l.zip');
      except
        if not DownloadPage.AbortedByUser then
          MsgBox(FmtMessage(CustomMessage('ModelsDownloadFailed'), [GetExceptionMessage]), mbCriticalError, MB_OK);
        Result := False;
      end;
    finally
      DownloadPage.Hide;
    end;
  end;
end;

// The Ready page names the owner (R1) and says where the models come from (R7).
function UpdateReadyMemo(Space, NewLine, MemoUserInfoInfo, MemoDirInfo, MemoTypeInfo,
  MemoComponentsInfo, MemoGroupInfo, MemoTasksInfo: String): String;
begin
  Result := CustomMessage('ReadyOwner') + NewLine
            + Space + OwnerName + NewLine + Space + OwnerSid + NewLine;
  if MemoDirInfo <> '' then
    Result := Result + NewLine + MemoDirInfo + NewLine;
  if MemoTasksInfo <> '' then
    Result := Result + NewLine + MemoTasksInfo + NewLine;
  if ModelsNeeded then
    Result := Result + NewLine + CustomMessage('ReadyModels') + NewLine + Space
              + '{#BuffaloURL}' + NewLine;
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

// The owner's data directory: <profile>\.face-unlock; '' when the owner's profile is unknown.
function DataDirFor(const Sid: string): string;
var
  Profile: string;
begin
  Result := '';
  Profile := ProfileDirOf(Sid);
  if Profile <> '' then
    Result := Profile + '\.face-unlock';
end;

{ Before [Files]: obtain the models (a silent install downloads here -- there is no Ready page),
  then stop a running stack. A non-empty result halts Setup on the Preparing page with that text.
  If Setup is then cancelled or fails, DeinitializeSetup starts the stopped tasks again (F-238). }
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Dir: string;
  Left: Integer;
begin
  Result := '';
  if ModelsNeeded and (ModelZip = '') then
  begin
    try
      DownloadTemporaryFile('{#BuffaloURL}', 'buffalo_l.zip', '{#BuffaloSHA256}', nil);
      ModelZip := ExpandConstant('{tmp}\buffalo_l.zip');
    except
      Result := FmtMessage(CustomMessage('ModelsDownloadFailed'), [GetExceptionMessage]);
      FailCode := 23;
      exit;
    end;
  end;
  Dir := ExpectedAppDir();
  if (not FileExists(Dir + '\{#MyAppExeName}')) and (StackProcesses(Dir, False) = 0) then
    exit;                                   // a first install: nothing runs
  Left := StopStack(Dir);
  StackStopped := True;
  if Left > 0 then
    Result := FmtMessage(CustomMessage('StillRunning'), [IntToStr(Left)]);
end;

// Unpack exactly the five pinned files into {app}\models\buffalo_l, each checked.
function InstallModels(): Boolean;
var
  Tmp, Dest: string;
  I: Integer;
begin
  Result := not ModelsNeeded;
  if Result then
    exit;
  WizardForm.StatusLabel.Caption := CustomMessage('StatusModels');
  Tmp := ExpandConstant('{tmp}\buffalo_l_unpacked');
  Dest := ExpandConstant('{app}\models\buffalo_l');
  try
    ExtractArchive(ModelZip, Tmp, '', False, nil);
  except
    Log('model archive extraction failed: ' + GetExceptionMessage);
    exit;
  end;
  if not ModelsValidIn(Tmp) then
  begin
    Log('the extracted model files do not match the pins');
    exit;
  end;
  DelTree(Dest, True, True, True);
  if not ForceDirectories(Dest) then
    exit;
  for I := 1 to MODEL_COUNT do
    if not FileCopy(Tmp + '\' + ModelName(I), Dest + '\' + ModelName(I), False) then
    begin
      Log('copying ' + ModelName(I) + ' failed');
      exit;
    end;
  Result := ModelsValidIn(Dest);
  Log('recognition models installed: ' + IntToStr(Ord(Result)));
end;

function RunTrayFlag(const Params: string): Integer;
begin
  if not Exec(ExpandConstant('{app}\{#MyAppExeName}'), Params, ExpandConstant('{app}'), SW_HIDE,
              ewWaitUntilTerminated, Result) then
    Result := -1;
  Log(Params + ': exit ' + IntToStr(Result));
end;

procedure Fail(Code: Integer; const Msg: string);
begin
  if FailCode = 0 then
    FailCode := Code;
  Log('FAILED (' + IntToStr(Code) + '): ' + Msg);
  if not WizardSilent() then
    MsgBox(Msg, mbError, MB_OK);
end;

// Register (or, when the task was unticked, unregister) the provider -- only from {app}, only after
// the ACL check says administrators alone can write there.
procedure RegisterProvider(AclOk: Boolean);
var
  Dll: string;
  ResultCode: Integer;
begin
  Dll := ExpandConstant('{app}\credential_provider\FaceCredentialProvider.dll');
  if not FileExists(Dll) then
    exit;
  if not WizardIsTaskSelected('cp') then
  begin
    Exec(ExpandConstant('{sys}\regsvr32.exe'), '/u /s "' + Dll + '"', '', SW_HIDE,
         ewWaitUntilTerminated, ResultCode);
    Log('sign-in tile not selected: regsvr32 /u exit ' + IntToStr(ResultCode));
    exit;
  end;
  if not AclOk then
  begin
    Fail(22, CustomMessage('AclFailed'));
    exit;
  end;
  WizardForm.StatusLabel.Caption := CustomMessage('StatusRegCP');
  if not Exec(ExpandConstant('{sys}\regsvr32.exe'), '/s "' + Dll + '"', '', SW_HIDE,
              ewWaitUntilTerminated, ResultCode) or (ResultCode <> 0) then
    Fail(22, FmtMessage(CustomMessage('CPFailed'), [IntToStr(ResultCode)]));
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  AclOk: Boolean;
begin
  if CurStep = ssPostInstall then
  begin
    // R1: the owner, always rewritten (F-213).
    RegWriteStringValue(HKLM, 'Software\{#MyAppShortName}', 'OriginalUserSid', GetOwnerSid());
    AclOk := RunTrayFlag('--verify-acl') = 0;
    RegisterProvider(AclOk);
    if not InstallModels() then
      Fail(23, CustomMessage('ModelsInstallFailed'));
    WizardForm.StatusLabel.Caption := CustomMessage('StatusRegTasks');
    if RunTrayFlag('--register --user-sid ' + GetOwnerSid()) <> 0 then
      Fail(21, FmtMessage(CustomMessage('TasksFailed'), [ExpandConstant('{app}\logs\register_tasks.log')]));
    InstallDone := True;
  end;
end;

// R17: a failure after the files were copied still ends a silent install with a non-zero code:
// 21 tasks, 22 sign-in tile, 23 models.
function GetCustomSetupExitCode(): Integer;
begin
  Result := FailCode;
end;

procedure DeinitializeSetup();
begin
  // F-238: Setup stopped the old stack and then did not finish -- start its tasks again.
  if StackStopped and not InstallDone then
    ForOurTasks(ExpectedAppDir(), True);
end;

function CredentialsSaved(): Boolean;
begin
  Result := (DataDirFor(GetOwnerSid()) <> '') and FileExists(DataDirFor(GetOwnerSid()) + '\credentials.bin');
end;

function EnrollmentExists(): Boolean;
begin
  Result := (DataDirFor(GetOwnerSid()) <> '') and FileExists(DataDirFor(GetOwnerSid()) + '\embeddings.npz');
end;

var
  UninstallUserSid: string;

function InitializeUninstall(): Boolean;
begin
  if not RegQueryStringValue(HKLM, 'Software\{#MyAppShortName}', 'OriginalUserSid', UninstallUserSid)
     or not IsUserSid(UninstallUserSid) then
    UninstallUserSid := '';
  Result := True;
end;

{ The owner's data is KEPT unless they say otherwise: a silent uninstall never deletes it
  (/REMOVEDATA forces removal for scripted teardown), and without a recorded owner nothing is
  offered at all -- never the uninstalling administrator's own profile (F-216). }
function WantsDataRemoved(DataDir: string): Boolean;
begin
  Result := False;
  if CmdLineParamExists('/REMOVEDATA') then
  begin
    Result := True;
    exit;
  end;
  if UninstallSilent() then
    exit;
  Result := MsgBox(FmtMessage(CustomMessage('RemoveData'), [DataDir]),
                   mbConfirmation, MB_YESNO) = IDYES;
end;

// FILE_ATTRIBUTE_REPARSE_POINT ($400) on the entry itself (FindFirst without a wildcard).
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

{ The data directory is removed with rmdir /S /Q, which deletes a junction or a symbolic link as
  a link and never enters it (measured in 8b); a data directory that IS a reparse point is not
  touched at all (F-02, F-236). }
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: string;
  ResultCode: Integer;
begin
  if (CurUninstallStep = usPostUninstall) and (UninstallUserSid <> '') then
  begin
    DataDir := DataDirFor(UninstallUserSid);
    if (DataDir <> '') and DirExists(DataDir) and WantsDataRemoved(DataDir) then
    begin
      if IsReparsePoint(DataDir) then
        Log('Data directory is a reparse point, not removed: ' + DataDir)
      else
        Exec(ExpandConstant('{cmd}'), '/C rmdir /S /Q "' + DataDir + '"',
             '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    end;
  end;
end;
