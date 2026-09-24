# Signing the Credential Provider DLL

## Read this first: signing does not gate loading

**Windows does not require a Credential Provider to be Authenticode-signed in
order to load it** on a normal Windows 10/11 x64 installation. A CP is an
in-process COM server loaded by `LogonUI.exe`, an ordinary user-mode process. The
policies that would demand a signature — WDAC / Device Guard user-mode code
integrity, HVCI — are opt-in enterprise configurations, not the default.

The evidence is in this repository's own troubleshooting notes. `INSTALL.md`
documents the case where LogonUI has loaded a **stale, unsigned** DLL and the
tile then returns `Parameter is incorrect` from `GetSerialization`. A DLL blocked
by a signature gate never reaches the point of returning an HRESULT at all.

So if your tile does not appear, **signing is not the fix**. Check the
registration (`register.ps1 -Action register` verifies by reading the registry
back), check that you rebuilt into `build-cp\` — the tree the registered CLSID
actually points at — and lock/unlock once to make LogonUI reload.

What signing *does* buy:

- **Provenance.** The file says who built it and that it has not been altered.
- **Enterprise readiness.** On a machine that *does* enforce UMCI, an unsigned CP
  will not load. Shipping signed means you are not the reason it fails there.
- **A quieter installer story.** SmartScreen reputation attaches to the signed
  installer (see `installer/README.md`), which is a separate artefact from this
  DLL — the release workflow's SignPath step submits the installer only.

## Quick start (development, self-signed)

```powershell
# Admin PowerShell, from the repo root.
# 1. See what is there now. Changes nothing.
.\tools\sign_cp.ps1 -DryRun

# 2. Create a dev certificate, sign, and trust it on THIS machine only.
.\tools\sign_cp.ps1 -SelfSigned -TrustLocally -IUnderstandTrustLocally
```

Since Stage 8b the self-signed key is created **non-exportable**, and
`-TrustLocally` refuses to run without `-IUnderstandTrustLocally` (a script cannot
add a root certificate by accident). The removal steps are in the script's help
(`Get-Help .\tools\sign_cp.ps1 -Full`, `.NOTES`) and are printed after trust is added.

`-TrustLocally` is a separate switch on purpose. Signing a file and *trusting the
signer machine-wide* are different decisions, and the second one installs your
certificate into `LocalMachine\Root` — the same store as every public CA. Without
that switch nothing is written to any certificate store, and the signature is
simply "present but from an untrusted issuer", which is the correct state for a
build you are about to hand to someone else.

Do not use `-TrustLocally` with a certificate you did not create yourself.

## Real certificate

An OV/EV code-signing certificate from a CA. Two ways in:

```powershell
# Already installed in CurrentUser\My or LocalMachine\My (including most tokens):
.\tools\sign_cp.ps1 -Thumbprint 1A2B3C4D...

# A .pfx on disk (prompts securely if it needs a password):
.\tools\sign_cp.ps1 -PfxPath C:\keys\codesign.pfx
```

Notes that matter in practice:

- **EV certificates live on hardware tokens.** The private key cannot be
  exported, so `-PfxPath` will not work; install the token's CSP and use
  `-Thumbprint`. Signing will prompt for the token PIN.
- **Timestamping is on by default** (`-TimestampServer`,
  `http://timestamp.digicert.com`). Without it the signature stops validating the
  day the certificate expires. With it, it stays valid for the life of the
  timestamp. There is no good reason to turn this off.
- **SHA-256** is used explicitly; SHA-1 signatures are rejected by current
  Windows.
- Signing does **not** change the CLSID and does **not** require re-running
  `register.ps1`. Re-register only if you rebuilt the DLL, in which case the
  usual rule applies — rebuild into `build-cp\`.

## Where this fits in the build

Stage 8b (F-13): signing is a **required** step of `installer/build.py` (step 2).
The 7l rebuild shipped an unsigned DLL because the old hook silently did nothing
without `SIGN_CP`; now a build without it aborts unless `--allow-unsigned-cp` is
given, and that choice is recorded in the gate stamp. After signing, the signer's
thumbprint must equal `SIGN_CP`, and the gate re-checks the staged copy.

```powershell
# the certificate already in the store (a dev certificate made once with -SelfSigned,
# or a real one)
$env:SIGN_CP = "1A2B3C4D..."      # a 40-char thumbprint
python installer\build.py --half 1
```

`SIGN_CP=self` is refused: a certificate minted during the build has a thumbprint
nobody could have pinned.

## What is still unsigned

- **The installer executable.** Handled separately by SignPath in
  `.github/workflows/release.yml`, and only when all three `SIGNPATH_*` secrets
  are present. Without them the release ships an unsigned installer and
  SmartScreen says "Unknown publisher".
- **The frozen Python executables** (`face_service.exe`, `face_unlock_tray.exe`,
  `face_unlock_watchdog.exe`). They are not loaded by LogonUI and have no
  signature requirement; they inherit whatever trust the installer has.

If SignPath is ever configured to sign nested files, the DLL inside the installer
would be covered by that instead — but that configuration lives in the SignPath
project, not in this repository, so nothing here can assert it.
