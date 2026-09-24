"""Hidden frozen self-check of data-directory custody (Stage 8b-2).

    face_service.exe --selfcheck-custody <dir> --out <file.json>

Defect -> consequence -> fix: the 8b build passed every static check and every venv selftest,
yet the FROZEN service could not heal the data directory at all (a lazily imported pywin32 module
missing from the bundle). Consequence: the first place that failure showed was a manual dist smoke
-- one step before the installer. Fix: the build gate runs THIS mode out of the built exe on a
seeded scratch directory, so the real code path is exercised in the real bundle before the stamp.

What it does: heal_data_dir(<dir>) then verify_data_dir(<dir>), and writes one JSON object to
<file>. Exit 0 when the heal succeeded, the verification walk is clean and every secret file is
protected SELF + SYSTEM; exit 1 otherwise; exit 2 on a usage error. It opens no pipe, no camera, no
engine, reads no config and never touches APP_DIR: <dir> is the only path it acts on. The exe is
windowed (stdout / stderr may be None), so the file is the only output channel.
"""
from __future__ import annotations

import json
import logging
import os
import sys


class _ListHandler(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.lines: list = []

    def emit(self, record):
        try:
            self.lines.append("%s %s: %s" % (record.levelname, record.name, record.getMessage()))
        except Exception:
            pass


def _secret_state(path: str, self_sid: str) -> dict:
    import win32security  # type: ignore
    sd = win32security.GetFileSecurity(path, win32security.DACL_SECURITY_INFORMATION)
    dacl = sd.GetSecurityDescriptorDacl()
    sids = sorted({win32security.ConvertSidToStringSid(dacl.GetAce(i)[-1])
                   for i in range(dacl.GetAceCount())}) if dacl is not None else []
    protected = bool(sd.GetSecurityDescriptorControl()[0] & win32security.SE_DACL_PROTECTED)
    return {"protected": protected, "sids": sids,
            "self_system_only": protected and set(sids) <= {self_sid, "S-1-5-18"} and bool(sids)}


def selfcheck_custody_main(argv) -> int:
    """argv: ["--selfcheck-custody", <dir>, "--out", <file>]."""
    try:
        target = argv[argv.index("--selfcheck-custody") + 1]
        out = argv[argv.index("--out") + 1]
    except (ValueError, IndexError):
        return 2
    handler = _ListHandler()
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)
    result: dict = {"dir": target, "ok": False}
    try:
        from face_service import datadir as D
        rep = D.heal_data_dir(target)
        problems = D.verify_data_dir(target)
        self_sid = D.self_sid_string()
        secrets = {}
        for root, dirs, files in os.walk(target):
            dirs[:] = [d for d in dirs if not D.is_reparse(os.path.join(root, d))]   # never follow
            for name in files:
                if name.lower().startswith(D.SECRET_PREFIXES):
                    full = os.path.join(root, name)
                    secrets[os.path.relpath(full, target)] = _secret_state(full, self_sid)
        foreign = sum(1 for p in problems if "foreign ACE" in p)
        result.update({
            "heal": {"ok": rep.ok, "aces_removed": rep.aces_removed, "relocked": rep.relocked,
                     "rewritten": rep.rewritten, "objects": rep.objects, "problems": rep.problems},
            "verify_problems": problems,
            "foreign_ace_problems": foreign,
            "secrets": secrets,
            "win32timezone_loaded": "win32timezone" in sys.modules,
            "frozen": bool(getattr(sys, "frozen", False)),
        })
        result["ok"] = (rep.ok and not problems and foreign == 0
                        and all(s["self_system_only"] for s in secrets.values()))
    except Exception as e:   # the report must be written whatever happened
        result["exception"] = repr(e)
    result["log"] = handler.lines
    try:
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2)
    except Exception:
        return 1
    return 0 if result["ok"] else 1
