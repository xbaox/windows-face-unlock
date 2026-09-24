"""tools/updater_selftest.py -- Stage 8b package F proof: the updater is notify-only (F-27, F-28).

The network is MOCKED (_http_get / _download / subprocess.Popen replaced); nothing is fetched,
downloaded or launched.
  [1] APPLY_ENABLED is False and download_and_launch refuses BEFORE any download or launch.
  [2] 404 -> "no-release" (not "network"); a network failure -> "network".
  [3] a pre-release (flagged, or a tag with a suffix) and a draft are ignored as "no-release".
  [4] a final newer release is reported with its tag; "v0.2.0-rc1" is never offered as 0.2.0.

Run:  python -m tools.updater_selftest
Exit 0 = all pass; 1 = a failure.
"""
from __future__ import annotations
import io
import json
import sys
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

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
               "browser_download_url": "https://example.invalid/setup.exe"},
              {"name": f"WindowsFaceUnlock-Setup-{tag.lstrip('v')}.exe.sha256",
               "browser_download_url": "https://example.invalid/setup.exe.sha256"}] if asset else []
    return json.dumps({"tag_name": tag, "prerelease": prerelease, "draft": draft,
                       "body": "notes", "assets": assets}).encode()


def main() -> int:
    calls = {"http": 0, "download": 0, "popen": 0}
    orig = (U._http_get, U._download, U.subprocess.Popen)

    def _dl(*_a, **_k):
        calls["download"] += 1

    def _popen(*_a, **_k):
        calls["popen"] += 1

    U._download, U.subprocess.Popen = _dl, _popen
    try:
        print("[1] notify-only")
        check("APPLY_ENABLED is False", U.APPLY_ENABLED is False)
        rel = U.ReleaseInfo("v9.9.9", "9.9.9", "", "WindowsFaceUnlock-Setup-9.9.9.exe",
                            "https://example.invalid/setup.exe", "https://example.invalid/x.sha256")
        ok, msg = U.download_and_launch(rel)
        check("download_and_launch refuses", ok is False and U.RELEASES_PAGE_URL in msg, msg)
        check("nothing downloaded or launched", calls["download"] == 0 and calls["popen"] == 0,
              calls)

        print("[2] 404 vs network")

        def _404(*_a, **_k):
            raise urllib.error.HTTPError(U.RELEASES_LATEST_URL, 404, "Not Found", {}, io.BytesIO())

        U._http_get = _404
        check("404 -> no-release", U.check_latest_status() == (None, "no-release"))

        def _net(*_a, **_k):
            raise OSError("getaddrinfo failed")

        U._http_get = _net
        check("network error -> network", U.check_latest_status() == (None, "network"))

        print("[3] pre-releases and drafts are ignored")
        for label, body in (("flagged prerelease", _release("v9.0.0", prerelease=True)),
                            ("suffix tag", _release("v9.0.0-rc1")),
                            ("draft", _release("v9.0.0", draft=True))):
            U._http_get = lambda *_a, b=body, **_k: b
            check(f"{label} -> no-release", U.check_latest_status() == (None, "no-release"))

        print("[4] a final newer release")
        U._http_get = lambda *_a, **_k: _release("v9.0.0")
        r, status = U.check_latest_status()
        check("reported", status == "ok" and r is not None and r.tag == "v9.0.0", (r, status))
        check("newer than the current version", r is not None and r.is_newer_than(U.__version__))
        U._http_get = lambda *_a, **_k: _release("v9.0.0", asset=False)
        check("no installer asset -> no-asset", U.check_latest_status() == (None, "no-asset"))
    finally:
        U._http_get, U._download, U.subprocess.Popen = orig

    if FAILS:
        print(f"\nUPDATER SELFTEST FAILED: {len(FAILS)} check(s): {FAILS}")
        return 1
    print("\nUPDATER SELFTEST OK: notify-only (nothing downloaded or run); 404 is 'no release', "
          "not a network error; pre-releases and drafts are ignored; a final newer tag is reported.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
