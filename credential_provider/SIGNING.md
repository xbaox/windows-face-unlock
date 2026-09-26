# Code signing

## What signing is for here

Windows does not require a Credential Provider to be signed for LogonUI to load it on an ordinary
Windows 10/11 installation, so a signature is not what makes the face tile appear. It matters for:

- **Smart App Control (SAC)** and enterprise code-integrity policies, which block unsigned or
  self-signed executables and DLLs -- including the provider LogonUI loads, the three product
  executables and every DLL in the bundle (F-70). A public-trust signature is the only thing that
  lets Face Unlock run on such a machine.
- Provenance: users and administrators can see who published the files.

## The release pipeline (act 9b R18, P-24)

- **Azure Trusted Signing** through `signtool` and the Azure.CodeSigning dlib, driven by
  `installer\build.py`. In CI the `sign` job logs in with OIDC and has no repository rights.
- The expected signer is **pinned by subject** (`FU_SIGN_SUBJECT`), and every signed file must
  verify `Valid` -- no "unknown chain" allowance.
- Order: the CP DLL on its own step -> PyInstaller -> every unsigned PE of ours -> the gate
  (which compares PE content without the certificate table) -> Setup and the uninstaller through
  Inno's SignTool.
- **NVIDIA files are never re-signed or modified** (their EULA). The cuDNN DLLs carry NVIDIA's own
  signature; for the NVIDIA DLLs that ship unsigned in the official wheels (CUDA runtime, cuBLAS,
  cuFFT, NVRTC) the fallback is a **catalog (`.cat`) signature** under our identity, installed by
  the installer, which leaves the files byte-identical. It is designed and verified in a VM in 9f.
- Without credentials every signing step prints `SIGNING SKIPPED` and says what is missing; such a
  build is a test build, not a release.

Until 9f no release is signed. The 0.1.0 / 0.1.1 builds carried a development certificate
(`CN=Windows Face Unlock (development)`) on the CP DLL only; that key was created
plaintext-exportable before Stage 8b and is **retired** -- the build no longer accepts a local
certificate thumbprint (F-244).

## Signing a local test DLL (developers only)

`tools\sign_cp.ps1` signs a DLL with a certificate in `CurrentUser\My` (`-Thumbprint`) or a fresh,
non-exportable self-signed development certificate (`-SelfSigned`). `-TrustLocally` (only with
`-SelfSigned`, only elevated, only together with `-IUnderstandTrustLocally`) adds that certificate
to the machine's Root and TrustedPublisher stores -- a machine-wide trust decision; the script
prints how to undo it. The `.pfx` mode is gone: `Get-PfxCertificate -Password` does not exist in
Windows PowerShell 5.1 (F-243) -- import the `.pfx` into your store and use `-Thumbprint`.

```powershell
.\tools\sign_cp.ps1 -DryRun                     # what is signed now; changes nothing
.\tools\sign_cp.ps1 -SelfSigned                  # development certificate, this user only
.\tools\sign_cp.ps1 -Thumbprint <40-hex thumbprint>
```

A development signature never makes a build releasable.
