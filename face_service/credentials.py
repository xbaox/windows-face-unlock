"""Store the user's Windows password encrypted via DPAPI (user scope).

DPAPI-encrypted blobs can only be decrypted by the SAME Windows user on the
SAME machine. They are safe from other users but *not* from malware running
as that same user. For a higher security bar you would use a TPM-backed
Credential Manager entry.

Stage 4 Step 6 (custody): new blobs are encrypted with a per-install random secret stored in
``pipe_entropy.bin`` (a file locked to SELF+SYSTEM via a protected DACL) used as the DPAPI *entropy*,
instead of the old public hardcoded constant. Pre-Stage-4 (v1) blobs are auto-migrated to v2 on the
first read. DPAPI stays user-scope (that already excludes other users); this only raises the bar for
same-user code from "copy a constant off GitHub" to "read a local file at runtime" -- a speed-bump,
not the real fix (which is SYSTEM-side custody, a later stage).
"""
from __future__ import annotations
import json
import logging
import os
from pathlib import Path

import win32api      # type: ignore
import win32con      # type: ignore
import win32crypt    # type: ignore
import win32file     # type: ignore
import win32security  # type: ignore

from .config import CREDS_PATH

log = logging.getLogger(__name__)

# LEGACY entropy for v1 blobs. Public (shipped in source) -> no real secrecy; kept ONLY to read /
# migrate pre-Stage-4 blobs. NEW encryptions use the per-install secret below, never this constant.
ENTROPY = b"face-unlock:v1"

# Per-install DPAPI entropy secret + the version marker distinguishing v1 (legacy) from v2 blobs.
# A v1 DPAPI blob begins with the DPAPI provider magic (01 00 00 00 ...), never with this prefix,
# so prefix-detection is unambiguous.
ENTROPY_PATH = CREDS_PATH.parent / "pipe_entropy.bin"
_V2_PREFIX = b"v2:"


def _self_sid_string() -> str:
    """String SID of the account this process runs as (SELF). Duplicated (not imported from
    service.py) so this module stays free of the heavy service/recognizer import chain."""
    th = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        sid = win32security.GetTokenInformation(th, win32security.TokenUser)[0]
    finally:
        win32api.CloseHandle(th)
    return win32security.ConvertSidToStringSid(sid)


def _build_secret_file_sa() -> win32security.SECURITY_ATTRIBUTES:
    """SECURITY_ATTRIBUTES with a PROTECTED DACL granting only SELF + SYSTEM (no Everyone, no
    inherited ACEs) -- the same SDDL mechanism as the Batch-1 pipe descriptor. 'P' strips the
    inheritable user-profile ACEs so the secret is not readable by other principals."""
    self_sid = _self_sid_string()
    sddl = f"D:P(A;;FA;;;{self_sid})(A;;FA;;;SY)"
    sd = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
        sddl, win32security.SDDL_REVISION_1)
    sa = win32security.SECURITY_ATTRIBUTES()
    sa.SECURITY_DESCRIPTOR = sd
    sa.bInheritHandle = 0
    return sa


def _write_locked_file(path: Path, data: bytes) -> None:
    """Create ``path`` with the restrictive SELF+SYSTEM descriptor and write ``data`` -- born locked,
    with no default-perms window (the SA is applied at creation)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    sa = _build_secret_file_sa()
    h = win32file.CreateFile(str(path), win32con.GENERIC_WRITE, 0, sa,
                             win32con.CREATE_ALWAYS, 0, None)
    try:
        win32file.WriteFile(h, data)
    finally:
        win32file.CloseHandle(h)


def _load_entropy_secret() -> "bytes | None":
    try:
        if ENTROPY_PATH.exists():
            data = ENTROPY_PATH.read_bytes()
            return data or None
    except Exception:
        pass
    return None


def _ensure_entropy_secret() -> bytes:
    """Return the per-install entropy secret, generating + persisting it (locked DACL) if absent."""
    existing = _load_entropy_secret()
    if existing is not None:
        return existing
    secret = os.urandom(32)
    _write_locked_file(ENTROPY_PATH, secret)
    return secret


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def save_password(username: str, password: str, domain: str = ".") -> None:
    blob = json.dumps({"u": username, "p": password, "d": domain}).encode("utf-8")
    secret = _ensure_entropy_secret()
    enc = _V2_PREFIX + win32crypt.CryptProtectData(blob, "face-unlock", secret, None, None, 0)
    CREDS_PATH.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_bytes(CREDS_PATH, enc)


def load_password() -> "dict | None":
    """Decrypt the stored credential blob, or None if absent / undecryptable. NEVER raises: a failure
    returns None exactly like an absent blob, so the unlock path degrades to the password tile
    instead of crashing (lockout risk 0 -- the lockout counter is already settled before this call).
    A legacy v1 blob is transparently migrated to v2 in place on first read."""
    if not CREDS_PATH.exists():
        return None
    try:
        enc = CREDS_PATH.read_bytes()
        if enc.startswith(_V2_PREFIX):
            secret = _load_entropy_secret()
            if secret is None:
                log.warning("credentials are v2 but the entropy secret is missing")
                return None
            _, data = win32crypt.CryptUnprotectData(enc[len(_V2_PREFIX):], secret, None, None, 0)
            return json.loads(data.decode("utf-8"))
        # v1 legacy blob (no marker): decrypt with the old public entropy, then migrate to v2.
        _, data = win32crypt.CryptUnprotectData(enc, ENTROPY, None, None, 0)
        try:
            secret = _ensure_entropy_secret()   # generates the per-install secret if not present yet
            new_blob = _V2_PREFIX + win32crypt.CryptProtectData(
                data, "face-unlock", secret, None, None, 0)
            _atomic_write_bytes(CREDS_PATH, new_blob)
            log.info("migrated credential blob v1 -> v2 (per-install entropy)")
        except Exception as e:   # migration is best-effort -- still return the plaintext we recovered
            log.warning("credential v1->v2 migration skipped: %s", e)
        return json.loads(data.decode("utf-8"))
    except Exception as e:
        log.warning("load_password failed (%s); treating as no-credentials", e)
        return None


def clear_password() -> None:
    if CREDS_PATH.exists():
        CREDS_PATH.unlink()
