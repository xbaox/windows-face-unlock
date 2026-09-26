#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Register or unregister the Face Unlock Credential Provider.

.DESCRIPTION
    Runs regsvr32 against the CP DLL and then VERIFIES the outcome by reading
    the registry, because regsvr32 /s reports nothing and its exit code is not
    trustworthy on its own. Success is printed only after the registry agrees.

    Before anything runs, the DLL path must be ADMIN-ONLY: LogonUI loads the
    registered DLL as SYSTEM, and regsvr32 (register or unregister) loads it in
    this elevated process. Defect (8a F-10): the dev fallback build-cp\Release
    sits under C:\dev, which grants Authenticated Users Modify -> any local
    account could replace the file SYSTEM loads -> the script now refuses any
    DLL that a principal other than SYSTEM, Administrators or TrustedInstaller
    can modify, delete, re-permission or take ownership of -- on the file, its
    folder, or any folder above it. For a dev build, copy the DLL into an
    admin-only folder (e.g. under %ProgramFiles%) and pass -DllPath. There is no
    override switch. The check is Find-NonAdminWriteAccess below; it only reads
    ACLs and can be exercised on its own (see NOTES).

    Exit codes:
        0  requested state reached and verified
        1  DLL not found, or the host cannot register an x64 DLL
        2  regsvr32 returned a non-zero exit code
        3  regsvr32 claimed success but the registry says otherwise
        4  refused: the DLL path is writable by a non-admin principal
           (also under -DryRun, which predicts the real run)

.PARAMETER Action
    register (default) or unregister.

.PARAMETER DllPath
    Optional explicit path to FaceCredentialProvider.dll. When omitted the
    script probes the installed layout first, then the dev build tree.

.PARAMETER DryRun
    Preflight only. Resolves the DLL, prints both probe candidates and which
    of them exists, prints the exact regsvr32 command line that would run, and
    reports the current state of the registry keys the verification checks --
    then exits 0 without executing anything. Nothing is registered,
    unregistered or written.

    Elevation is still required: a dry run that skipped the admin check would
    not be validating the conditions the real run executes under.

.EXAMPLE
    .\register.ps1 -Action register
.EXAMPLE
    .\register.ps1 -Action unregister
.EXAMPLE
    .\register.ps1 -DryRun
.EXAMPLE
    .\register.ps1 -Action unregister -DryRun

.NOTES
    Exercising the path check without elevation and without registering
    anything (the function only reads ACLs):

        $t = $null; $e = $null
        $ast = [System.Management.Automation.Language.Parser]::ParseFile(
            (Resolve-Path .\credential_provider\register.ps1), [ref]$t, [ref]$e)
        $fn = $ast.Find({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
                                    $n.Name -eq 'Find-NonAdminWriteAccess' }, $true)
        . ([scriptblock]::Create($fn.Extent.Text))
        Find-NonAdminWriteAccess -LiteralPath 'C:\Program Files\WindowsFaceUnlock\credential_provider\FaceCredentialProvider.dll'

    No output means admin-only. Each finding names the path, the principal and
    the right that disqualifies it.

    If an older dev registration still points at an unsafe path, unregister is
    refused too (regsvr32 /u loads the DLL elevated). Remove the two registry
    keys directly instead -- tools\uninstall.ps1 does exactly that after this
    script declines.
#>
[CmdletBinding()]
param(
    [ValidateSet('register', 'unregister')]
    [string]$Action = 'register',

    [string]$DllPath,

    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# CLSID_FaceCredentialProvider -- keep in sync with credential_provider/guid.h
# (DEFINE_GUID 0x8414d7b6, 0xd536, 0x461b, 0xb3,0x1b, 0xad,0xf7,0x7b,0x3a,0x89,0x74).
$Clsid    = '{8414D7B6-D536-461B-B31B-ADF77B3A8974}'
$CpKey    = "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Authentication\Credential Providers\$Clsid"
$ClsidKey = "HKLM:\SOFTWARE\Classes\CLSID\$Clsid"

# --- admin-only path check (8a F-10) ------------------------------------------
# Returns one finding per (path, principal, right) that lets a principal other than
# SYSTEM, BUILTIN\Administrators or TrustedInstaller change what this path resolves
# to. Returns nothing when the path is admin-only. Reads ACLs only.
#
# Rights that count, per level of the path:
#   the file          write/append data, delete, change permissions, take ownership
#   its folder        the same minus append, plus add-file and delete-child
#                     (replacing the file needs one of those)
#   every folder up   delete, delete-child, change permissions, take ownership
#                     (replacing a folder on the way down needs one of those)
# Plain "create folders" on an ancestor -- which the volume root grants to
# Authenticated Users by default -- cannot replace an existing path component and
# is not counted. Inherit-only ACEs (e.g. CREATOR OWNER) do not apply to the object
# they sit on and are skipped; their effect shows up on the child they apply to,
# which is checked in its own right. A non-admin OWNER counts too: an owner can
# always rewrite the DACL. A reparse point anywhere on the path is refused
# outright, because the ACLs read here would not be the ones of the real target.
function Find-NonAdminWriteAccess {
    param([Parameter(Mandatory = $true)][string]$LiteralPath)

    $trusted = @(
        'S-1-5-18',                                                        # SYSTEM
        'S-1-5-32-544',                                                    # BUILTIN\Administrators
        'S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464'   # NT SERVICE\TrustedInstaller
    )
    $R = [System.Security.AccessControl.FileSystemRights]
    $generic = 0x10000000 -bor 0x40000000                                  # GENERIC_ALL | GENERIC_WRITE
    $common  = [int]$R::Delete -bor [int]$R::ChangePermissions -bor [int]$R::TakeOwnership -bor $generic
    $masks = @{
        file     = $common -bor [int]$R::WriteData -bor [int]$R::AppendData
        parent   = $common -bor [int]$R::WriteData -bor [int]$R::DeleteSubdirectoriesAndFiles
        ancestor = $common -bor [int]$R::DeleteSubdirectoriesAndFiles
    }
    $sidType = [System.Security.Principal.SecurityIdentifier]

    $findings = @()
    $item = Get-Item -LiteralPath $LiteralPath -Force -ErrorAction Stop
    $level = if ($item -is [System.IO.DirectoryInfo]) { 'parent' } else { 'file' }
    while ($null -ne $item) {
        $path = $item.FullName
        if ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
            $findings += [pscustomobject]@{ Path = $path; Principal = '-'; Right = 'reparse point';
                                            Level = $level }
        }
        $acl = Get-Acl -LiteralPath $path
        $owner = $acl.GetOwner($sidType).Value
        if ($trusted -notcontains $owner) {
            $name = $owner
            try { $name = ([System.Security.Principal.SecurityIdentifier]$owner).Translate(
                              [System.Security.Principal.NTAccount]).Value } catch { }
            $findings += [pscustomobject]@{ Path = $path; Principal = $name; Right = 'owner (can rewrite the ACL)';
                                            Level = $level }
        }
        foreach ($ace in $acl.GetAccessRules($true, $true, $sidType)) {
            if ($ace.AccessControlType -ne [System.Security.AccessControl.AccessControlType]::Allow) { continue }
            if ($ace.PropagationFlags -band [System.Security.AccessControl.PropagationFlags]::InheritOnly) { continue }
            $sid = $ace.IdentityReference.Value
            if ($trusted -contains $sid) { continue }
            $hit = [int]$ace.FileSystemRights -band $masks[$level]
            if ($hit -eq 0) { continue }
            $name = $sid
            try { $name = $ace.IdentityReference.Translate([System.Security.Principal.NTAccount]).Value } catch { }
            $findings += [pscustomobject]@{ Path = $path; Principal = $name;
                                            Right = [string]$ace.FileSystemRights; Level = $level }
        }
        if ($item -is [System.IO.DirectoryInfo]) { $item = $item.Parent } else { $item = $item.Directory }
        $level = if ($level -eq 'file') { 'parent' } else { 'ancestor' }
    }
    return $findings
}

# A 32-bit host would use the 32-bit regsvr32 (which cannot load an x64 DLL)
# AND would read HKLM:\SOFTWARE redirected into WOW6432Node -- so the
# verification below would be reading the wrong hive and could report a false
# result either way. Refuse rather than lie.
if (-not [Environment]::Is64BitProcess) {
    Write-Host "ERROR: this must run in 64-bit PowerShell." -ForegroundColor Red
    Write-Host "       A 32-bit host cannot register the x64 DLL and reads a redirected registry view."
    Write-Host "       Use C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
    exit 1
}

# Stage 9 (F-98): on ARM64 Windows LogonUI is a native ARM64 process and cannot load this x64 DLL at
# all -- say so instead of failing later with a generic regsvr32 error.
if ((Get-CimInstance Win32_Processor -ErrorAction SilentlyContinue | Select-Object -First 1).Architecture -eq 12) {
    Write-Host "ERROR: this is ARM64 Windows. The Face Unlock sign-in tile is built for x64 and cannot" -ForegroundColor Red
    Write-Host "       be loaded by the ARM64 sign-in screen; Face Unlock supports x64 Windows only."
    exit 1
}

# --- locate the DLL ----------------------------------------------------------
$candidates = @(
    (Join-Path $PSScriptRoot 'FaceCredentialProvider.dll'),                     # installed layout: next to this script
    (Join-Path $PSScriptRoot '..\build-cp\Release\FaceCredentialProvider.dll')  # dev layout: repo build tree
)
$candidateLabels = @('installed layout: next to this script',
                     'dev layout: repo build tree')

if ($DllPath) {
    if (-not (Test-Path -LiteralPath $DllPath -PathType Leaf)) {
        Write-Host "ERROR: -DllPath does not exist: $DllPath" -ForegroundColor Red
        exit 1
    }
    $resolved = (Resolve-Path -LiteralPath $DllPath).Path
}
else {
    $resolved = $null
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            $resolved = (Resolve-Path -LiteralPath $candidate).Path
            break
        }
    }
    if (-not $resolved) {
        Write-Host "ERROR: FaceCredentialProvider.dll not found. Looked in:" -ForegroundColor Red
        foreach ($candidate in $candidates) { Write-Host "  $candidate" }
        Write-Host ""
        Write-Host "Build it first, from the repo root:"
        Write-Host '  cmake -S credential_provider -B build-cp -A x64'
        Write-Host '  cmake --build build-cp --config Release'
        Write-Host "Or pass an explicit path with -DllPath."
        exit 1
    }
}

Write-Host "DLL:    $resolved"
Write-Host "Action: $Action"

# --- refuse a DLL a non-admin can replace (8a F-10) -------------------------------
# Read-only, so it sits above the dry-run gate and -DryRun reports the same verdict
# the real run would act on. Applies to unregister as well: regsvr32 /u loads the DLL
# into this elevated process just like register does.
$aclFindings = @(Find-NonAdminWriteAccess -LiteralPath $resolved)
if ($aclFindings.Count -gt 0) {
    Write-Host ""
    Write-Host "REFUSED: the DLL path is writable by a non-admin principal." -ForegroundColor Red
    Write-Host "  LogonUI loads this DLL as SYSTEM and regsvr32 loads it elevated, so anyone"
    Write-Host "  who can change the file or a folder above it controls code that runs as"
    Write-Host "  SYSTEM. Findings:"
    foreach ($f in $aclFindings) {
        Write-Host ("    [{0}] {1}" -f $f.Level, $f.Path)
        Write-Host ("        {0}: {1}" -f $f.Principal, $f.Right)
    }
    Write-Host ""
    Write-Host "Fix: install through the installer (Program Files is admin-only), or copy the"
    Write-Host "     DLL into an admin-only folder, e.g. `"$env:ProgramFiles\WindowsFaceUnlock-dev`","
    Write-Host "     and pass -DllPath. Nothing was registered or unregistered."
    if ($Action -eq 'unregister') {
        Write-Host "To drop a stale registration without loading the DLL, delete these keys"
        Write-Host "(tools\uninstall.ps1 does this after this script declines):"
        Write-Host "  $CpKey"
        Write-Host "  $ClsidKey"
    }
    exit 4
}
Write-Host "Path:   admin-only (file, folder and every folder above it)"

# --- build the command -------------------------------------------------------
# The path is quoted inside the argument so directories with spaces
# (e.g. C:\Program Files\WindowsFaceUnlock) survive the command line.
$quoted = '"' + $resolved + '"'
$regsvrArgs = if ($Action -eq 'register') { @('/s', $quoted) } else { @('/u', '/s', $quoted) }

# --- dry-run gate ------------------------------------------------------------
# Everything above this point only reads: Test-Path, Resolve-Path, Write-Host.
# Every call that changes the machine is below it, so "is the dry-run path
# clean" is answerable by walking the AST rather than by reading carefully.
if ($DryRun) {
    Write-Host ""
    Write-Host "=== DRY RUN - preflight only, nothing will be executed ===" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "Elevation           : satisfied (#Requires -RunAsAdministrator let this start)"
    Write-Host "Host                : 64-bit PowerShell"
    Write-Host ""
    if ($DllPath) {
        Write-Host "DLL source          : -DllPath was supplied explicitly"
    }
    else {
        Write-Host "DLL candidates, in probe order:"
        for ($i = 0; $i -lt $candidates.Count; $i++) {
            $mark = if (Test-Path -LiteralPath $candidates[$i] -PathType Leaf) { 'FOUND  ' } else { 'missing' }
            Write-Host ("  [{0}] {1}" -f $mark, $candidates[$i])
            Write-Host ("            {0}" -f $candidateLabels[$i])
        }
    }
    Write-Host ("Selected            : {0}" -f $resolved)
    Write-Host ""
    Write-Host "Command that WOULD run:"
    Write-Host ("  regsvr32.exe {0}" -f ($regsvrArgs -join ' '))
    Write-Host ""
    Write-Host "Verification key (this one gates the exit code):"
    Write-Host ("  {0}" -f $CpKey)
    Write-Host ("    now      : {0}" -f $(if (Test-Path -LiteralPath $CpKey) { 'PRESENT' } else { 'ABSENT' }))
    Write-Host ("    required : {0} after a successful '{1}'" -f `
                $(if ($Action -eq 'register') { 'PRESENT' } else { 'ABSENT' }), $Action)
    Write-Host ""
    Write-Host "COM class key (reported only, does not gate the exit code):"
    Write-Host ("  {0}" -f $ClsidKey)
    Write-Host ("    now      : {0}" -f $(if (Test-Path -LiteralPath $ClsidKey) { 'PRESENT' } else { 'ABSENT' }))
    Write-Host ""
    Write-Host "DRY RUN - no registration was changed and no registry key was written." -ForegroundColor Cyan
    exit 0
}

# --- run regsvr32 ------------------------------------------------------------
$proc = Start-Process -FilePath 'regsvr32.exe' -ArgumentList $regsvrArgs `
                      -Wait -PassThru -WindowStyle Hidden
if ($proc.ExitCode -ne 0) {
    Write-Host "ERROR: regsvr32 exited with code $($proc.ExitCode)." -ForegroundColor Red
    Write-Host "       Command: regsvr32.exe $($regsvrArgs -join ' ')"
    exit 2
}

# --- verify by fact, not by exit code ---------------------------------------
$cpPresent    = Test-Path -LiteralPath $CpKey
$clsidPresent = Test-Path -LiteralPath $ClsidKey

if ($Action -eq 'register') {
    if (-not $cpPresent) {
        Write-Host "ERROR: regsvr32 reported success but the Credential Provider is NOT registered." -ForegroundColor Red
        Write-Host "       Missing key: $CpKey"
        exit 3
    }
}
else {
    if ($cpPresent) {
        Write-Host "ERROR: regsvr32 reported success but the Credential Provider is STILL registered." -ForegroundColor Red
        Write-Host "       Leftover key: $CpKey"
        exit 3
    }
}

# Informational only: a stale COM class does not by itself put a tile on the
# lock screen, so it does not gate the exit code -- but it is worth seeing when
# chasing an incomplete uninstall.
if ($Action -eq 'unregister' -and $clsidPresent) {
    Write-Host "NOTE: the COM class key still exists: $ClsidKey" -ForegroundColor Yellow
    Write-Host "      The provider is unregistered (no tile), but the CLSID entry lingers."
}

# --- success -----------------------------------------------------------------
if ($Action -eq 'register') {
    Write-Host "Registered and verified: $resolved" -ForegroundColor Green
    Write-Host ""
    Write-Host "Before you rely on this, test the fallback:" -ForegroundColor Yellow
    Write-Host "  1. stop the FaceUnlock-Service task (or kill the service process)"
    Write-Host "  2. lock the workstation (Win+L)"
    Write-Host "  3. confirm you can still sign in with PIN / password"
    Write-Host "A Credential Provider that cannot be bypassed is a lockout waiting to happen."
}
else {
    Write-Host "Unregistered and verified: $resolved" -ForegroundColor Green
    Write-Host "Lock and unlock once (Win+L) for LogonUI to drop the tile."
}

exit 0
