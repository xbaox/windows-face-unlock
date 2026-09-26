"""tools/updater_selftest.py -- the update CHECK (Stage 9, act 9b R15; D-97, F-183, F-194, F-196).

The network is MOCKED (_http_get replaced); nothing is fetched, downloaded or launched.
  [1] the module installs nothing: no download / launch path exists at all (D-97, F-196).
  [2] each failure has its own status: 404 -> no-release, 403/429 -> rate-limited, 407 ->
      proxy-auth, other HTTP -> "http <code>", network -> network, garbage -> bad-response;
      a release without the installer asset -> "no-asset:<tag>" (F-183).
  [3] a pre-release (flagged, or a tag with a suffix) and a draft are ignored as "no-release".
  [4] a final newer release is reported with its tag; "v0.2.0-rc1" is never offered as 0.2.0.
  [5] the background check is due at most once per 24 h, and a broken or future stamp makes it
      due (F-194); the User-Agent carries no version; the repository is one constant (R15, 9g).

Run:  python -m tools.updater_selftest
Exit 0 = all pass; 1 = a failure.
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if not os.environ.get("FACE_UNLOCK_HOME"):
    os.environ["FACE_UNLOCK_HOME"] = tempfile.mkdtemp(prefix="faceunlock_upd_")

from presence_monitor import updater as U

FAILS: list[str] = []


def check(name, cond, got=None):
    if cond:
        print(f"  ok    {name}")
    else:
        print(f"  FAIL  {name}" + (f" (got={got!r})" if got is not None else ""))
        FAILS.append(name)


def _release(tag, *, prerelease=False, draft=False, asset=True):
    assets = [{"name": f"WindowsFaceUnlock-Setup-{tag.lstrip('v')}.exe",
               "browser_download_url": "https://example.invalid/setup.exe"}] if asset else []
    return json.dumps({"tag_name": tag, "prerelease": prerelease, "draft": draft,
                       "body": "notes", "assets": assets}).encode()


def _with(answer):
    def fake(url, timeout=15.0):
        if isinstance(answer, BaseException):
            raise answer
        return answer
    U._http_get = fake
    return U.check_latest_status(timeout=1.0)


def _http(code):
    return urllib.error.HTTPError("https://x", code, "x", {}, io.BytesIO(b""))


def main() -> int:
    real = U._http_get
    try:
        print("[1] nothing is installed from here")
        src = Path(U.__file__).read_text(encoding="utf-8")
        check("no download / launch code", all(x not in src for x in
              ("def download_and_launch", "def _download", "subprocess", "APPLY_ENABLED =")))

        print("[2] one status per failure")
        check("404 -> no-release", _with(_http(404)) == (None, "no-release"))
        check("403 -> rate-limited", _with(_http(403)) == (None, "rate-limited"))
        check("429 -> rate-limited", _with(_http(429)) == (None, "rate-limited"))
        check("407 -> proxy-auth", _with(_http(407)) == (None, "proxy-auth"))
        check("500 -> http 500", _with(_http(500)) == (None, "http 500"))
        check("URLError -> network", _with(urllib.error.URLError("down")) == (None, "network"))
        check("garbage -> bad-response", _with(b"not json")[1] == "bad-response")
        check("a list -> bad-response", _with(b"[1]")[1] == "bad-response")
        check("no installer asset -> no-asset:<tag>", _with(_release("v9.9.9", asset=False)) ==
              (None, "no-asset:v9.9.9"))

        print("[3] pre-releases and drafts are ignored")
        check("prerelease flag", _with(_release("v9.9.9", prerelease=True))[1] == "no-release")
        check("draft flag", _with(_release("v9.9.9", draft=True))[1] == "no-release")
        check("suffix tag", _with(_release("v9.9.9-rc1"))[1] == "no-release")

        print("[4] a final release")
        rel, st = _with(_release("v9.9.9"))
        check("reported with its tag", st == "ok" and rel.tag == "v9.9.9" and rel.is_newer_than("0.1.1"))
        check("an older one is not newer", not _with(_release("v0.0.1"))[0].is_newer_than("0.1.1"))
        check("an odd asset name with a path is not taken",
              not U.INSTALLER_RE.match("WindowsFaceUnlock-Setup-1\\..\\x.exe"))

        print("[5] the 24-hour gate, the UA, the one constant")
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "update_state.json"
            check("no stamp -> due", U.due_for_auto_check(1000.0, p))
            U.record_auto_check(1000.0, p)
            check("an hour later -> not due", not U.due_for_auto_check(1000.0 + 3600, p))
            check("24 h later -> due", U.due_for_auto_check(1000.0 + 24 * 3600, p))
            U.record_auto_check(5e9, p)
            check("a stamp in the future -> due", U.due_for_auto_check(1000.0, p))
            p.write_text("{", encoding="utf-8")
            check("a broken stamp -> due", U.due_for_auto_check(1000.0, p))
        check("the User-Agent carries no version", U.__version__ not in U.USER_AGENT)
        check("every URL derives from REPO", U.REPO in U.RELEASES_LATEST_URL and U.REPO in U.RELEASES_PAGE_URL
              and src.count("xbaox") == 1)
    finally:
        U._http_get = real

    print()
    if FAILS:
        print(f"UPDATER SELFTEST FAILED: {len(FAILS)} check(s): {FAILS}")
        return 1
    print("UPDATER SELFTEST OK: check-only (no install path), one status per failure, pre-releases "
          "ignored, at most one background check per 24 h, no version in the User-Agent, one repo "
          "constant.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
