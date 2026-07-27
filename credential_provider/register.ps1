#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Register or unregister the Face Unlock Credential Provider.

.DESCRIPTION
    Runs regsvr32 against the CP DLL and then VERIFIES the outcome by reading
    the registry, because regsvr32 /s reports nothing and its exit code is not
    trustworthy on its own. Success is printed only after the registry agrees.

    Exit codes:
        0  requested state reached and verified
        1  DLL not found, or the host cannot register an x64 DLL
        2  regsvr32 returned a non-zero exit code
        3  regsvr32 claimed success but the registry says otherwise

.PARAMETER Action
    register (default) or unregister.

.PARAMETER DllPath
    Optional explicit path to FaceCredentialProvider.dll. When omitted the
    script probes the installed layout first, then the dev build tree.

.EXAMPLE
    .\register.ps1 -Action register
.EXAMPLE
    .\register.ps1 -Action unregister
#>
[CmdletBinding()]
param(
    [ValidateSet('register', 'unregister')]
    [string]$Action = 'register',

    [string]$DllPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# CLSID_FaceCredentialProvider -- keep in sync with credential_provider/guid.h
# (DEFINE_GUID 0x8414d7b6, 0xd536, 0x461b, 0xb3,0x1b, 0xad,0xf7,0x7b,0x3a,0x89,0x74).
$Clsid    = '{8414D7B6-D536-461B-B31B-ADF77B3A8974}'
$CpKey    = "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Authentication\Credential Providers\$Clsid"
$ClsidKey = "HKLM:\SOFTWARE\Classes\CLSID\$Clsid"

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

# --- locate the DLL ----------------------------------------------------------
$candidates = @(
    (Join-Path $PSScriptRoot 'FaceCredentialProvider.dll'),                     # installed layout: next to this script
    (Join-Path $PSScriptRoot '..\build-cp\Release\FaceCredentialProvider.dll')  # dev layout: repo build tree
)

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

# --- run regsvr32 ------------------------------------------------------------
# The path is quoted inside the argument so directories with spaces
# (e.g. C:\Program Files\WindowsFaceUnlock) survive the command line.
$quoted = '"' + $resolved + '"'
$regsvrArgs = if ($Action -eq 'register') { @('/s', $quoted) } else { @('/u', '/s', $quoted) }

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
