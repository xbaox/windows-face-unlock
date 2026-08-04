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
                             CurrentUser\My and use it. Development only.
      -Thumbprint <hex>      use a certificate already installed in
                             CurrentUser\My or LocalMachine\My. This is the
                             option for a real purchased certificate that lives
                             in the store (or on a token, if its CSP is set up).
      -PfxPath <file>        use a .pfx on disk. -PfxPassword optional; you are
                             prompted securely if the file needs one and none was
                             given.

    A self-signed certificate is NOT trusted by anything until it is installed
    into a trust store, and that is a machine-wide security decision, so it never
    happens implicitly. -TrustLocally is a separate, explicit switch, and it is
    the only thing in this script that writes to a certificate store.

.PARAMETER DllPath
    The DLL to sign. Defaults to the dev build tree, build-cp\Release.

.PARAMETER TimestampServer
    RFC 3161 timestamp URL. Timestamping is what keeps a signature valid after
    the certificate expires, so it is on by default.

.PARAMETER TrustLocally
    Install the signing certificate into LocalMachine\Root and
    LocalMachine\TrustedPublisher. Development convenience for a self-signed
    certificate; do not do this with anything you did not create yourself.

.PARAMETER DryRun
    Print the plan and the DLL's CURRENT signature status, then exit. Changes
    nothing -- no certificate is created, no file is signed, no store is touched.

.EXAMPLE
    .\tools\sign_cp.ps1 -DryRun
.EXAMPLE
    .\tools\sign_cp.ps1 -SelfSigned -TrustLocally
.EXAMPLE
    .\tools\sign_cp.ps1 -Thumbprint 1A2B3C...
.EXAMPLE
    .\tools\sign_cp.ps1 -PfxPath C:\keys\codesign.pfx
#>
[CmdletBinding(DefaultParameterSetName = 'Inspect')]
param(
    [Parameter(ParameterSetName = 'SelfSigned', Mandatory = $true)]
    [switch]$SelfSigned,

    [Parameter(ParameterSetName = 'Thumbprint', Mandatory = $true)]
    [string]$Thumbprint,

    [Parameter(ParameterSetName = 'Pfx', Mandatory = $true)]
    [string]$PfxPath,

    [Parameter(ParameterSetName = 'Pfx')]
    [System.Security.SecureString]$PfxPassword,

    [string]$DllPath,

    [string]$Subject = 'CN=Windows Face Unlock (development)',

    [string]$TimestampServer = 'http://timestamp.digicert.com',

    [switch]$TrustLocally,

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
    Write-Host 'Pass -SelfSigned, -Thumbprint <hex> or -PfxPath <file> to sign.' -ForegroundColor DarkGray
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
            Write-Host "     Type      : CodeSigningCert, 3 years"
        }
        'Thumbprint' { Write-Host "  1. look up certificate $Thumbprint in CurrentUser\My then LocalMachine\My" }
        'Pfx'        { Write-Host "  1. load the certificate from $PfxPath" }
    }
    Write-Host "  2. Set-AuthenticodeSignature on $DllPath"
    Write-Host "     with -TimestampServer $TimestampServer"
    if ($TrustLocally) {
        Write-Host '  3. install the certificate into LocalMachine\Root and LocalMachine\TrustedPublisher'
        Write-Host '     (machine-wide trust decision; needs elevation)' -ForegroundColor Yellow
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
            -KeyExportPolicy Exportable `
            -NotAfter (Get-Date).AddYears(3)
        Write-Host "  created: $($fuCert.Thumbprint)"
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
    'Pfx' {
        if (-not (Test-Path -LiteralPath $PfxPath -PathType Leaf)) {
            Write-Host "ERROR: no .pfx at $PfxPath" -ForegroundColor Red
            exit 1
        }
        if (-not $PfxPassword) {
            $PfxPassword = Read-Host -Prompt 'PFX password (blank if none)' -AsSecureString
        }
        $fuCert = Get-PfxCertificate -FilePath $PfxPath -Password $PfxPassword
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
        }
        finally { $fuStore.Close() }
    }
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
exit 0
