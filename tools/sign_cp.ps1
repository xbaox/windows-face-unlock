<#
.SYNOPSIS
    Authenticode-sign the Face Unlock Credential Provider DLL.

.DESCRIPTION
    Stage 7d-J. Nothing in this repository signed the DLL: no signtool
    invocation, no certificate, no POST_BUILD step in the CP CMakeLists, and the
    release workflow's SignPath step submits the INSTALLER only. This script is
    the missing piece for the DLL itself.

    READ credential_provider\SIGNING.md BEFORE USING THIS. The short version:
    Windows does NOT require a Credential Provider to be signed in order to load
    it on a normal Windows 10/11 installation, so signing here buys provenance
    and a cleaner story under enterprise code-integrity policies -- not the
    ability to log in. Anyone expecting a signature to fix a tile that does not
    appear is chasing the wrong thing.

    Three ways to get a key, exactly one of which must be given:

      -SelfSigned            create a fresh code-signing certificate in
                             CurrentUser\My and use it. Development only. The
                             private key is created NON-EXPORTABLE: it signs from
                             this user store and cannot be copied out of it.
      -Thumbprint <hex>      use a certificate already installed in
                             CurrentUser\My or LocalMachine\My. This is the
                             option for a real purchased certificate that lives
                             in the store (or on a token, if its CSP is set up).

    A self-signed certificate is NOT trusted by anything until it is installed
    into a trust store, and that is a machine-wide security decision, so it never
    happens implicitly. -TrustLocally is a separate, explicit switch, it is the
    only thing in this script that writes to a MACHINE certificate store, and it
    is refused unless -IUnderstandTrustLocally is given as well. How to remove
    everything this script creates: see NOTES.

    Stage 9 (act 9b R18, P-24, F-244): installer\build.py no longer calls this script. Release
    builds sign through Azure Trusted Signing with the signer pinned by subject (build.py); the
    development key this script created before 8b was plaintext-exportable and is retired from
    the build path. This script remains a DEVELOPER tool for signing a local test DLL.

    Stage 9 (F-243): the -PfxPath mode is gone -- Get-PfxCertificate has no -Password in Windows
    PowerShell 5.1, so it never worked here; import the .pfx into CurrentUser\My and use
    -Thumbprint instead.

.PARAMETER DllPath
    The DLL to sign. Defaults to the dev build tree, build-cp\Release.

.PARAMETER TimestampServer
    RFC 3161 timestamp URL. Timestamping is what keeps a signature valid after
    the certificate expires, so it is on by default.

.PARAMETER TrustLocally
    Install the signing certificate into LocalMachine\Root and
    LocalMachine\TrustedPublisher. Development convenience for a self-signed
    certificate; do not do this with anything you did not create yourself.
    Needs elevation AND -IUnderstandTrustLocally AND -SelfSigned; refused otherwise, BEFORE
    anything is signed (Stage 9, F-242).

.PARAMETER IUnderstandTrustLocally
    Required together with -TrustLocally. Defect (8a F-40): one switch used to
    add a developer certificate to the machine Root store -> every process on the
    machine then trusts code signed with that key -> the decision now has to be
    spelled out twice. There is deliberately no interactive prompt: a build or a
    script must fail closed, never sit waiting for a keypress.

.PARAMETER DryRun
    Print the plan and the DLL's CURRENT signature status, then exit. Changes
    nothing -- no certificate is created, no file is signed, no store is touched.

.EXAMPLE
    .\tools\sign_cp.ps1 -DryRun
.EXAMPLE
    .\tools\sign_cp.ps1 -SelfSigned
.EXAMPLE
    .\tools\sign_cp.ps1 -SelfSigned -TrustLocally -IUnderstandTrustLocally
.EXAMPLE
    .\tools\sign_cp.ps1 -Thumbprint 1A2B3C...

.NOTES
    REMOVING WHAT THIS SCRIPT CREATED. Replace <THUMBPRINT> with the value the
    script printed ("created:" / "Thumb").

    Machine trust -- only if -TrustLocally was used. Elevated PowerShell:
        Get-ChildItem Cert:\LocalMachine\Root, Cert:\LocalMachine\TrustedPublisher |
            Where-Object Thumbprint -eq '<THUMBPRINT>' | Remove-Item
    or, from an elevated prompt:
        certutil -delstore Root <THUMBPRINT>
        certutil -delstore TrustedPublisher <THUMBPRINT>

    The self-signed certificate and its private key (-SelfSigned). As the user:
        Remove-Item -LiteralPath 'Cert:\CurrentUser\My\<THUMBPRINT>' -DeleteKey

    Check that nothing is left:
        Get-ChildItem Cert:\CurrentUser\My, Cert:\LocalMachine\Root, Cert:\LocalMachine\TrustedPublisher |
            Where-Object Thumbprint -eq '<THUMBPRINT>'

    Removing the certificate does not unsign DLLs already signed with it; their
    signature simply stops chaining to a trusted root on this machine.
#>
[CmdletBinding(DefaultParameterSetName = 'Inspect')]
param(
    [Parameter(ParameterSetName = 'SelfSigned', Mandatory = $true)]
    [switch]$SelfSigned,

    [Parameter(ParameterSetName = 'Thumbprint', Mandatory = $true)]
    [string]$Thumbprint,

    [string]$DllPath,

    [string]$Subject = 'CN=Windows Face Unlock (development)',

    [string]$TimestampServer = 'http://timestamp.digicert.com',

    [switch]$TrustLocally,

    [switch]$IUnderstandTrustLocally,

    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# Naming rule inherited from register_tasks.ps1: no local may collide with a
# parameter name in any casing. Internals are fu*-prefixed.
$fuRepoRoot = Split-Path -Parent $PSScriptRoot
if (-not $DllPath) {
    $DllPath = Join-Path $fuRepoRoot 'build-cp\Release\FaceCredentialProvider.dll'
}

Write-Host ''
Write-Host 'Face Unlock -- Credential Provider signing' -ForegroundColor White
Write-Host ('=' * 60)
Write-Host "DLL            : $DllPath"
Write-Host "Mode           : $($PSCmdlet.ParameterSetName)"
Write-Host "Timestamp      : $TimestampServer"
Write-Host "Trust locally  : $TrustLocally"

# Refused before anything else -- -DryRun included, so a dry run never presents a plan
# the real run would reject. See .PARAMETER IUnderstandTrustLocally.
# Stage 9 (F-242): -TrustLocally only for a certificate this script creates, and only elevated --
# checked here, before anything is signed or any store is touched.
if ($TrustLocally -and $PSCmdlet.ParameterSetName -ne 'SelfSigned') {
    Write-Host 'REFUSED: -TrustLocally is only for a certificate created with -SelfSigned.' -ForegroundColor Red
    exit 1
}
if ($TrustLocally -and -not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
                              ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host 'REFUSED: -TrustLocally writes machine stores and needs an elevated PowerShell.' -ForegroundColor Red
    exit 1
}
if ($TrustLocally -and -not $IUnderstandTrustLocally) {
    Write-Host ''
    Write-Host 'REFUSED: -TrustLocally adds this certificate to LocalMachine\Root and' -ForegroundColor Red
    Write-Host '         LocalMachine\TrustedPublisher -> every process on this machine then' -ForegroundColor Red
    Write-Host '         trusts code signed with its key. Signing does not need it (SIGNING.md).' -ForegroundColor Red
    Write-Host '         To do it anyway for a certificate you created, repeat the command with' -ForegroundColor Red
    Write-Host '         -IUnderstandTrustLocally. Removal: Get-Help .\tools\sign_cp.ps1 -Full (NOTES).' -ForegroundColor Red
    exit 1
}

if (-not (Test-Path -LiteralPath $DllPath -PathType Leaf)) {
    Write-Host ''
    Write-Host "ERROR: no DLL at $DllPath" -ForegroundColor Red
    Write-Host 'Build it first:' -ForegroundColor DarkGray
    Write-Host '  cmake -S credential_provider -B build-cp -A x64 -G "Visual Studio 17 2022"' -ForegroundColor DarkGray
    Write-Host '  cmake --build build-cp --config Release' -ForegroundColor DarkGray
    exit 1
}

# Current state, always shown: the most common reason to run this is to find out
# whether the DLL on disk is signed at all.
Write-Host ''
Write-Host 'Current signature' -ForegroundColor Cyan
$fuSig = Get-AuthenticodeSignature -LiteralPath $DllPath
Write-Host "  Status  : $($fuSig.Status)"
Write-Host "  Message : $($fuSig.StatusMessage)"
if ($fuSig.SignerCertificate) {
    Write-Host "  Subject : $($fuSig.SignerCertificate.Subject)"
    Write-Host "  Thumb   : $($fuSig.SignerCertificate.Thumbprint)"
    Write-Host "  Expires : $($fuSig.SignerCertificate.NotAfter)"
}
else {
    Write-Host '  (unsigned)'
}

if ($PSCmdlet.ParameterSetName -eq 'Inspect') {
    Write-Host ''
    Write-Host 'No signing mode given -- inspection only. Nothing was changed.' -ForegroundColor Green
    Write-Host 'Pass -SelfSigned or -Thumbprint <hex> to sign.' -ForegroundColor DarkGray
    exit 0
}

# ---------------------------------------------------------------------------
# Everything above this line only READS: Test-Path and Get-AuthenticodeSignature.
# -DryRun exits here, so "is the dry-run path clean" is answerable by walking the
# AST -- the same property credential_provider\register.ps1 was built to have.
# ---------------------------------------------------------------------------
if ($DryRun) {
    Write-Host ''
    Write-Host 'DRY RUN -- planned actions' -ForegroundColor Yellow
    switch ($PSCmdlet.ParameterSetName) {
        'SelfSigned' {
            Write-Host "  1. create a code-signing certificate in Cert:\CurrentUser\My"
            Write-Host "     Subject   : $Subject"
            Write-Host "     Type      : CodeSigningCert, 3 years, private key NonExportable"
        }
        'Thumbprint' { Write-Host "  1. look up certificate $Thumbprint in CurrentUser\My then LocalMachine\My" }
    }
    Write-Host "  2. Set-AuthenticodeSignature on $DllPath"
    Write-Host "     with -TimestampServer $TimestampServer"
    if ($TrustLocally) {
        Write-Host '  3. install the certificate into LocalMachine\Root and LocalMachine\TrustedPublisher'
        Write-Host '     (machine-wide trust decision; needs elevation; confirmed by -IUnderstandTrustLocally)' -ForegroundColor Yellow
    }
    else {
        Write-Host '  3. (skipped) no certificate store is touched without -TrustLocally'
    }
    Write-Host ''
    Write-Host 'Nothing was changed.' -ForegroundColor Green
    exit 0
}

# ===========================================================================
# Mutating from here down.
# ===========================================================================
$fuCert = $null
switch ($PSCmdlet.ParameterSetName) {
    'SelfSigned' {
        Write-Host ''
        Write-Host 'Creating a self-signed code-signing certificate...' -ForegroundColor Cyan
        $fuCert = New-SelfSignedCertificate `
            -Subject $Subject `
            -Type CodeSigningCert `
            -CertStoreLocation 'Cert:\CurrentUser\My' `
            -KeyUsage DigitalSignature `
            -KeyExportPolicy NonExportable `
            -NotAfter (Get-Date).AddYears(3)
        Write-Host "  created: $($fuCert.Thumbprint)"
        Write-Host '  (private key NonExportable; removal: Get-Help .\tools\sign_cp.ps1 -Full, NOTES)' -ForegroundColor DarkGray
    }
    'Thumbprint' {
        $fuCert = Get-ChildItem -Path 'Cert:\CurrentUser\My', 'Cert:\LocalMachine\My' `
                                -ErrorAction SilentlyContinue |
                  Where-Object { $_.Thumbprint -eq $Thumbprint.Replace(' ', '') } |
                  Select-Object -First 1
        if (-not $fuCert) {
            Write-Host "ERROR: no certificate with thumbprint $Thumbprint in CurrentUser\My or LocalMachine\My." -ForegroundColor Red
            exit 1
        }
    }
}

if (-not $fuCert.HasPrivateKey) {
    Write-Host 'ERROR: that certificate has no private key, so it cannot sign.' -ForegroundColor Red
    exit 1
}

Write-Host ''
Write-Host 'Signing...' -ForegroundColor Cyan
$fuResult = Set-AuthenticodeSignature -LiteralPath $DllPath `
                                      -Certificate $fuCert `
                                      -TimestampServer $TimestampServer `
                                      -HashAlgorithm SHA256
Write-Host "  Status  : $($fuResult.Status)"
Write-Host "  Message : $($fuResult.StatusMessage)"

if ($fuResult.Status -ne 'Valid' -and $fuResult.Status -ne 'UnknownError') {
    Write-Host ''
    Write-Host "Signing did not produce a valid signature ($($fuResult.Status))." -ForegroundColor Red
    exit 2
}

if ($TrustLocally) {
    Write-Host ''
    Write-Host 'Installing the certificate into the machine trust stores...' -ForegroundColor Yellow
    Write-Host '  (this is a machine-wide trust decision -- see SIGNING.md)' -ForegroundColor DarkGray
    $fuPublic = [System.Security.Cryptography.X509Certificates.X509Certificate2]::new($fuCert.RawData)
    $fuTrustFailed = $false
    foreach ($fuStoreName in @('Root', 'TrustedPublisher')) {
        $fuStore = [System.Security.Cryptography.X509Certificates.X509Store]::new(
            $fuStoreName, 'LocalMachine')
        try {
            $fuStore.Open('ReadWrite')
            $fuStore.Add($fuPublic)
            Write-Host "  added to LocalMachine\$fuStoreName"
        }
        catch {
            Write-Host ("  FAILED for LocalMachine\{0}: {1}" -f $fuStoreName, $_.Exception.Message) -ForegroundColor Red
            $fuTrustFailed = $true
        }
        finally { $fuStore.Close() }
    }
    Write-Host '  To undo (elevated):' -ForegroundColor DarkGray
    Write-Host ("    certutil -delstore Root {0}" -f $fuCert.Thumbprint) -ForegroundColor DarkGray
    Write-Host ("    certutil -delstore TrustedPublisher {0}" -f $fuCert.Thumbprint) -ForegroundColor DarkGray
}

Write-Host ''
Write-Host 'Final signature' -ForegroundColor Cyan
$fuFinal = Get-AuthenticodeSignature -LiteralPath $DllPath
Write-Host "  Status  : $($fuFinal.Status)"
if ($fuFinal.SignerCertificate) {
    Write-Host "  Subject : $($fuFinal.SignerCertificate.Subject)"
    Write-Host "  Thumb   : $($fuFinal.SignerCertificate.Thumbprint)"
}
Write-Host ''
Write-Host 'Done. Re-register the DLL only if you also rebuilt it -- signing does not' -ForegroundColor Green
Write-Host 'change the CLSID and does not require re-running register.ps1.' -ForegroundColor DarkGray
if ($TrustLocally -and $fuTrustFailed) {
    Write-Host 'The DLL is signed, but adding the certificate to a machine store FAILED (see above).' -ForegroundColor Red
    exit 3
}
exit 0
