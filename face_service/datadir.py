"""Data-directory custody: heal the ACL of APP_DIR at service start (Stage 8b; F-01, F-23, F-02, F-12).

Defect -> consequence -> fix, which is the whole reason this module exists:

* Defect. Up to 0.1.0 the installer created ``%USERPROFILE%\\.face-unlock`` with
  ``Permissions: users-modify``: an explicit, inheritable ``BUILTIN\\Users: Modify`` ACE that every
  file below inherited -- the DPAPI blob, the per-install entropy, the gallery, the enrollment
  images, config.toml (8a, elev.txt). ``pipe_entropy.bin`` was "born locked" only when it was
  CREATED; a file that already existed kept whatever it had.
* Consequence. Other local accounts could read the biometric files and alter files this service
  trusts at start-up. The restrictive ACL on the profile root does not help: files are reachable
  by full path. A reinstall does not remove an ACE Inno added, so fixing the installer alone leaves
  every installed machine as it is.
* Fix. The installer no longer touches the ACL (the service creates the directory), and the
  service heals the directory itself before the pipe exists:
    - APP_DIR gets a PROTECTED DACL: SELF / SYSTEM / BUILTIN\\Administrators, FullAccess, OI CI.
      The owner is not changed.
    - Every child gets exactly the inherited copy of that DACL (explicit ACEs dropped), except the
      secret files (credentials.bin*, pipe_entropy.bin*), which get the protected SELF + SYSTEM
      descriptor of credentials._build_secret_file_sa.
    - A read-only verification walk then checks that no ACE other than SELF / SYSTEM / BA is left,
      that every owner is one of those three, and that the secrets are protected.
  Whatever cannot be healed or verified is reported, and the service then refuses every face
  function with ``custody`` (Stage 9, R9: unlock, unlock_gesture, presence, verify, enrollment;
  ping answers "refusing: custody"). It was ``insecure-data-dir`` on unlock only (act A-2).

Reparse points (junctions, symlinks, mount points) and hard links are never followed or modified.
Any of them inside the data tree is a finding in its own right -- nothing the service writes there
creates one -- so it fails the heal instead of being skipped. Every object is opened with
FILE_FLAG_OPEN_REPARSE_POINT and inspected and re-secured through that one handle, so the check and
the change cannot land on two different objects. The DACL is written with SetKernelObjectSecurity on
the handle, which neither computes inheritance from a path nor propagates on its own: the walk
below is the only propagation, and it never descends through a reparse point.

Pure standard library + pywin32; no numpy, no camera, no engine. Safe to call on a missing
directory (it is created) and cheap on a healthy one (nothing is rewritten when the descriptor
already matches).
"""
from __future__ import annotations

import ctypes
import logging
import os
import re
import stat
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path

import pywintypes      # type: ignore
import win32con        # type: ignore
import win32file       # type: ignore
import win32security   # type: ignore

log = logging.getLogger(__name__)

SYSTEM_SID = "S-1-5-18"
ADMINS_SID = "S-1-5-32-544"

# File-name prefixes of the two secrets. The trailing "*" of the act is the prefix match: it also
# covers the ".tmp" sibling an interrupted atomic write can leave behind.
SECRET_PREFIXES = ("credentials.bin", "pipe_entropy.bin")

# Sub-directories the service itself reads or writes. Named in the act: a reparse point on either
# (or on APP_DIR) makes the tree unsafe. The generic rule below already covers them; the names stay
# here so the log says which one it was.
TRUSTED_SUBDIRS = ("enroll", "debug_frames")

# Exactly the names _maybe_dump_frame writes: "%Y%m%d-%H%M%S-<ms>_<tag>.npy|.png", tag = probe or
# verify<N>. The purge and the ring prune delete these and nothing else, so a file somebody put in
# debug_frames\ by hand is never removed by a pattern that was only ever meant for dumps.
DUMP_NAME_RE = re.compile(r"^\d{8}-\d{6}-\d{3}_[A-Za-z0-9]+\.(?:npy|png)$")

_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_FILE_ATTRIBUTE_DIRECTORY = 0x10
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_SHARE_ALL = (win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE | win32con.FILE_SHARE_DELETE)

# Stage 8b-2 (frozen custody). Defect: the attributes and the link count came from
# win32file.GetFileInformationByHandle, whose reply carries three file TIMES that pywin32 turns into
# datetimes through a lazy, native-side import of win32timezone. Consequence: the venv had that
# module and every selftest passed, the PyInstaller bundle did not, and the frozen service failed
# every heal with ModuleNotFoundError -- fail-closed "insecure-data-dir" on every install (8b smoke,
# RED). Fix: ask the kernel for exactly the two facts the check needs, through ctypes on the SAME
# handle, with nothing time-typed in the reply: FileAttributeTagInfo (attributes, reparse) and
# FileStandardInfo (link count, directory). The check itself is unchanged: a reparse point or a
# hard-linked file is unsafe.
_FILE_STANDARD_INFO_CLASS = 1        # FILE_INFO_BY_HANDLE_CLASS.FileStandardInfo
_FILE_ATTRIBUTE_TAG_INFO_CLASS = 9   # FILE_INFO_BY_HANDLE_CLASS.FileAttributeTagInfo


class _FileStandardInfo(ctypes.Structure):
    _fields_ = [("AllocationSize", ctypes.c_longlong), ("EndOfFile", ctypes.c_longlong),
                ("NumberOfLinks", wintypes.DWORD), ("DeletePending", wintypes.BOOLEAN),
                ("Directory", wintypes.BOOLEAN)]


class _FileAttributeTagInfo(ctypes.Structure):
    _fields_ = [("FileAttributes", wintypes.DWORD), ("ReparseTag", wintypes.DWORD)]


_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_GetFileInformationByHandleEx = _kernel32.GetFileInformationByHandleEx
_GetFileInformationByHandleEx.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID,
                                          wintypes.DWORD)
_GetFileInformationByHandleEx.restype = wintypes.BOOL


def _file_facts(handle) -> "tuple[int, int, bool]":
    """(attributes, number of links, is directory) of an open handle. Raises pywintypes.error on
    failure, like the pywin32 call it replaces, so every caller's error path is unchanged."""
    raw = int(handle)
    tag = _FileAttributeTagInfo()
    if not _GetFileInformationByHandleEx(raw, _FILE_ATTRIBUTE_TAG_INFO_CLASS, ctypes.byref(tag),
                                         ctypes.sizeof(tag)):
        raise pywintypes.error(ctypes.get_last_error(), "GetFileInformationByHandleEx",
                               "FileAttributeTagInfo")
    std = _FileStandardInfo()
    if not _GetFileInformationByHandleEx(raw, _FILE_STANDARD_INFO_CLASS, ctypes.byref(std),
                                         ctypes.sizeof(std)):
        raise pywintypes.error(ctypes.get_last_error(), "GetFileInformationByHandleEx",
                               "FileStandardInfo")
    return int(tag.FileAttributes), int(std.NumberOfLinks), bool(std.Directory)


_DACL = win32security.DACL_SECURITY_INFORMATION
_OWNER = win32security.OWNER_SECURITY_INFORMATION
_PROTECTED = win32security.PROTECTED_DACL_SECURITY_INFORMATION
_UNPROTECTED = win32security.UNPROTECTED_DACL_SECURITY_INFORMATION


@dataclass
class CustodyReport:
    """What one heal did. ``ok`` is the only field anything gates on."""
    ok: bool = False
    aces_removed: int = 0      # ACEs for a trustee other than SELF / SYSTEM / BA that were dropped
    relocked: int = 0          # secret files whose descriptor had to be rewritten
    rewritten: int = 0         # objects whose DACL was rewritten (secrets included)
    objects: int = 0           # objects visited (APP_DIR included)
    problems: list = field(default_factory=list)

    def summary(self) -> str:
        return ("aces_removed=%d relocked=%d rewritten=%d objects=%d problems=%d"
                % (self.aces_removed, self.relocked, self.rewritten, self.objects,
                   len(self.problems)))


def self_sid_string() -> str:
    """SELF -- the one helper in face_service.identity (Stage 9, D-74)."""
    from .identity import current_user_sid
    return current_user_sid()


def is_reparse(path) -> bool:
    """True when ``path`` itself is a reparse point (junction, symlink, mount point). lstat does not
    follow the link, so this looks at the entry, never at its target. A missing path is False."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return bool(getattr(st, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _is_secret(name: str) -> bool:
    return any(name.lower().startswith(p) for p in SECRET_PREFIXES)


def _sd_from_sddl(sddl: str):
    return win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
        sddl, win32security.SDDL_REVISION_1)


def _dacl_sddl(sd) -> str:
    """DACL as SDDL, with the auto-inherited ("AI") control bit dropped: a file created by normal
    inheritance carries it and one written by SetKernelObjectSecurity does not, while the ACEs are
    the same -- comparing with it would rewrite every healthy file on every start."""
    s = win32security.ConvertSecurityDescriptorToStringSecurityDescriptor(
        sd, win32security.SDDL_REVISION_1, _DACL)
    head, sep, rest = s.partition("(")
    return head.replace("AI", "") + sep + rest


class _Targets:
    """The four descriptors the heal writes, each with its canonical SDDL for the compare."""

    def __init__(self, self_sid: str):
        self.self_sid = self_sid
        three = (self_sid, SYSTEM_SID, ADMINS_SID)
        self.allowed = frozenset(three)
        self.root = _sd_from_sddl("D:P" + "".join("(A;OICI;FA;;;%s)" % s for s in three))
        self.child_dir = _sd_from_sddl("D:" + "".join("(A;OICIID;FA;;;%s)" % s for s in three))
        self.child_file = _sd_from_sddl("D:" + "".join("(A;ID;FA;;;%s)" % s for s in three))
        # The very descriptor credentials.py creates secrets with, so the heal and the writer can
        # never disagree about what "locked" means.
        from .credentials import _build_secret_file_sa
        self.secret = _build_secret_file_sa().SECURITY_DESCRIPTOR
        self.canon = {id(sd): _dacl_sddl(sd)
                      for sd in (self.root, self.child_dir, self.child_file, self.secret)}


def _open_no_follow(path: str, access: int):
    return win32file.CreateFile(
        path, access, _SHARE_ALL, None, win32con.OPEN_EXISTING,
        _FILE_FLAG_OPEN_REPARSE_POINT | _FILE_FLAG_BACKUP_SEMANTICS, None)


def _foreign_aces(dacl, allowed) -> int:
    if dacl is None:
        return 0
    n = 0
    for i in range(dacl.GetAceCount()):
        ace = dacl.GetAce(i)
        try:
            sid = win32security.ConvertSidToStringSid(ace[-1])
        except Exception:
            n += 1
            continue
        if sid not in allowed:
            n += 1
    return n


def _heal_one(path: str, kind: str, t: _Targets, rep: CustodyReport) -> "bool | None":
    """Re-secure one object through a no-follow handle. Returns True for a directory the walk may
    descend into, False for a file, None for an object that must not be touched (reparse point,
    hard link, open failure) -- that last case is recorded as a problem."""
    rep.objects += 1
    try:
        h = _open_no_follow(path, win32con.READ_CONTROL | win32con.WRITE_DAC)
    except pywintypes.error as e:
        rep.problems.append("cannot open %s (winerror=%d)" % (path, e.winerror))
        return None
    try:
        attrs, nlinks, is_dir = _file_facts(h)
        if attrs & _FILE_ATTRIBUTE_REPARSE_POINT:
            rep.problems.append("reparse point %s (not followed, not modified)" % path)
            return None
        if not is_dir and nlinks > 1:
            rep.problems.append("hard-linked file %s (links=%d; not modified)" % (path, nlinks))
            return None
        if kind == "root":
            target, flag = t.root, _PROTECTED
        elif is_dir:
            target, flag = t.child_dir, _UNPROTECTED
        elif _is_secret(os.path.basename(path)):
            target, flag = t.secret, _PROTECTED
        else:
            target, flag = t.child_file, _UNPROTECTED
        cur = win32security.GetKernelObjectSecurity(h, _DACL)
        if _dacl_sddl(cur) != t.canon[id(target)]:
            foreign = _foreign_aces(cur.GetSecurityDescriptorDacl(), t.allowed)
            win32security.SetKernelObjectSecurity(h, _DACL | flag, target)
            rep.aces_removed += foreign
            rep.rewritten += 1
            if target is t.secret:
                rep.relocked += 1
        return is_dir
    except pywintypes.error as e:
        rep.problems.append("cannot re-secure %s (winerror=%d)" % (path, e.winerror))
        return None
    finally:
        win32file.CloseHandle(h)


def _walk_heal(dirpath: str, t: _Targets, rep: CustodyReport) -> None:
    # The directory's own DACL was rewritten by the caller BEFORE this listing, so from here on
    # nobody but SELF / SYSTEM / BA can add an entry to it while it is being walked.
    try:
        entries = sorted(os.scandir(dirpath), key=lambda e: e.name)
    except OSError as e:
        rep.problems.append("cannot list %s (%s)" % (dirpath, e))
        return
    for entry in entries:
        if _heal_one(entry.path, "child", t, rep):
            _walk_heal(entry.path, t, rep)


def verify_data_dir(app_dir, self_sid: "str | None" = None) -> list:
    """Read-only custody check of the whole tree. Returns the list of problems (empty = clean).

    Clean means: no reparse point and no hard-linked file anywhere; every owner is SELF, SYSTEM or
    BA; every ACE on every object names SELF, SYSTEM or BA; APP_DIR's DACL is protected; each secret
    file is protected and grants only SELF + SYSTEM."""
    self_sid = self_sid or self_sid_string()
    allowed = {self_sid, SYSTEM_SID, ADMINS_SID}
    problems: list = []

    def check(path: str, kind: str) -> "bool | None":
        try:
            h = _open_no_follow(path, win32con.READ_CONTROL)
        except pywintypes.error as e:
            problems.append("cannot open %s (winerror=%d)" % (path, e.winerror))
            return None
        try:
            attrs, nlinks, is_dir = _file_facts(h)
            if attrs & _FILE_ATTRIBUTE_REPARSE_POINT:
                problems.append("reparse point %s" % path)
                return None
            if not is_dir and nlinks > 1:
                problems.append("hard-linked file %s" % path)
                return None
            sd = win32security.GetKernelObjectSecurity(h, _DACL | _OWNER)
            owner = win32security.ConvertSidToStringSid(sd.GetSecurityDescriptorOwner())
            if owner not in allowed:
                problems.append("foreign owner %s on %s" % (owner, path))
            dacl = sd.GetSecurityDescriptorDacl()
            if dacl is None:
                problems.append("NULL DACL on %s" % path)
                return is_dir
            n = _foreign_aces(dacl, allowed)
            if n:
                problems.append("%d foreign ACE(s) on %s" % (n, path))
            protected = bool(sd.GetSecurityDescriptorControl()[0] & win32security.SE_DACL_PROTECTED)
            if kind == "root" and not protected:
                problems.append("APP_DIR DACL not protected")
            if not is_dir and _is_secret(os.path.basename(path)):
                sids = {win32security.ConvertSidToStringSid(dacl.GetAce(i)[-1])
                        for i in range(dacl.GetAceCount())}
                if not protected or not sids <= {self_sid, SYSTEM_SID}:
                    problems.append("secret %s not locked to SELF+SYSTEM" % path)
            return is_dir
        except pywintypes.error as e:
            problems.append("cannot read %s (winerror=%d)" % (path, e.winerror))
            return None
        finally:
            win32file.CloseHandle(h)

    def walk(dirpath: str) -> None:
        try:
            entries = sorted(os.scandir(dirpath), key=lambda e: e.name)
        except OSError as e:
            problems.append("cannot list %s (%s)" % (dirpath, e))
            return
        for entry in entries:
            if check(entry.path, "child"):
                walk(entry.path)

    root = str(app_dir)
    if check(root, "root"):
        walk(root)
    return problems


def heal_data_dir(app_dir) -> CustodyReport:
    """Heal, then verify, the custody of ``app_dir``. Never raises; the report says what happened.

    Logs ONE INFO line with the outcome on success and ONE ERROR line on failure; the caller turns
    ``ok=False`` into the ``custody`` refusal (Stage 9, R9; the wire token was insecure-data-dir)."""
    rep = CustodyReport()
    root = str(app_dir)
    try:
        if is_reparse(root):
            # Not even the root's own DACL is touched: a link here points somewhere this service
            # does not own, and "fixing" it would re-secure that target instead.
            rep.problems.append("APP_DIR is a reparse point (not followed, not modified)")
        else:
            Path(root).mkdir(parents=True, exist_ok=True)
            for name in TRUSTED_SUBDIRS:
                sub = os.path.join(root, name)
                if is_reparse(sub):
                    rep.problems.append("%s is a reparse point (not followed, not modified)" % sub)
            t = _Targets(self_sid_string())
            if _heal_one(root, "root", t, rep):
                _walk_heal(root, t, rep)
            if not rep.problems:
                rep.problems.extend(verify_data_dir(root, t.self_sid))
    except Exception as e:   # never let custody take the service down: it fails closed instead
        rep.problems.append("heal raised %r" % (e,))
    rep.ok = not rep.problems
    if rep.ok:
        log.info("data dir custody healed: %s (%s)", rep.summary(), root)
    else:
        log.error("data dir custody FAILED: %s -- face functions refused (custody); first "
                  "problems: %s", rep.summary(), "; ".join(rep.problems[:5]))
    return rep


def purge_debug_frames(app_dir) -> int:
    """Delete leftover frame dumps when ``debug_dump_frames`` is off (F-12 / D-16).

    Defect: the dump directory was never emptied once the knob went back off, so raw face frames
    stayed on disk indefinitely (40 files, 26.7 MB on the live machine). Fix: at start-up, with the
    knob off, remove the files whose NAMES match the dump pattern -- nothing else -- never through a
    reparse point, and remove the directory once it is empty. Returns the number of files removed;
    never raises."""
    d = os.path.join(str(app_dir), "debug_frames")
    removed = 0
    try:
        if not os.path.lexists(d):
            return 0
        if is_reparse(d):
            log.warning("debug_frames is a reparse point -- not purged (%s)", d)
            return 0
        for entry in list(os.scandir(d)):
            if not DUMP_NAME_RE.match(entry.name) or is_reparse(entry.path):
                continue
            if not entry.is_file(follow_symlinks=False):
                continue
            try:
                os.unlink(entry.path)
                removed += 1
            except OSError as e:
                log.warning("debug_frames purge: %s not removed (%s)", entry.name, e)
        try:
            if not any(os.scandir(d)):
                os.rmdir(d)
        except OSError:
            pass
        if removed:
            log.info("debug_frames purged on start: %d dump file(s) removed "
                     "(debug_dump_frames is off)", removed)
    except Exception as e:
        log.warning("debug_frames purge failed: %r", e)
    return removed


def remove_tree_no_follow(path) -> tuple:
    """Delete ``path`` and everything under it WITHOUT following reparse points.

    A reparse point met on the way is unlinked as a link (os.rmdir on a directory junction, os.unlink
    on a file symlink) and its target is never entered. Returns ``(removed, problems)``; never
    raises. Used where the service deletes inside its own tree (clear_enrollment)."""
    removed = 0
    problems: list = []

    def rm(p: str) -> None:
        nonlocal removed
        try:
            st = os.lstat(p)
        except FileNotFoundError:
            return
        except OSError as e:
            problems.append("%s: %s" % (p, e))
            return
        reparse = bool(getattr(st, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)
        is_dir = stat.S_ISDIR(st.st_mode) or bool(
            getattr(st, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_DIRECTORY)
        try:
            if reparse:
                # The link itself goes; what it points at is not ours and is left alone.
                (os.rmdir if is_dir else os.unlink)(p)
                removed += 1
                return
            if is_dir:
                for entry in list(os.scandir(p)):
                    rm(entry.path)
                os.rmdir(p)
            else:
                os.unlink(p)
            removed += 1
        except OSError as e:
            problems.append("%s: %s" % (p, e))

    rm(str(path))
    return removed, problems
