"""Update CHECK -- is a newer release published? (Stage 9, act 9b R15.) It installs nothing.

* The repository is ONE constant, ``REPO`` (changed in stage 9g); every URL derives from it.
* ``check_latest_status`` asks GitHub's ``releases/latest`` once and never raises. Its status tells
  apart: a newer/older final release ("ok"), nothing published or only a pre-release
  ("no-release", HTTP 404), a release without the installer asset ("no-asset", with the tag), a
  rate limit (403/429), a proxy asking for credentials (407), other HTTP errors, a network error
  and an unusable answer (F-183). The tray shows each with its own text on a manual check and
  stays silent about all but "a newer version exists" on the background check.
* The background check runs at most once per 24 hours (``due_for_auto_check`` over a small state
  file in the data directory) and only while ``update_check`` is on (F-194, F-175).
* Installing updates stays OFF until stage 9f. Stage 8b kept a disabled download-verify-run path
  here; it had no caller and several latent defects (F-196: the file hashed in one open and run by
  path in another, a server-chosen file name, any lone .sha256 accepted, a sweep that deleted every
  file in the folder, no size cap) -- so it is gone rather than kept dormant (D-97). 9f brings it
  back designed around WinVerifyTrust + a publisher pin checked on the same handle that runs.
"""
from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from face_service._version import __version__

log = logging.getLogger(__name__)

REPO = "xbaox/windows-face-unlock"                # the ONE place the repository is named (9g)
RELEASES_LATEST_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE_URL = f"https://github.com/{REPO}/releases/latest"
# F-194: the product name only -- not the exact installed version -- goes to GitHub.
USER_AGENT = "windows-face-unlock-updater"

# Exactly what installer.iss emits: OutputBaseFilename=WindowsFaceUnlock-Setup-{version}.
INSTALLER_RE = re.compile(r"^WindowsFaceUnlock-Setup-[0-9A-Za-z.\-]+\.exe$", re.IGNORECASE)

# A FINAL release tag: vX.Y.Z and nothing after it.
_FINAL_TAG_RE = re.compile(r"^v?\d+\.\d+\.\d+$")
_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:[-.].*)?$")

AUTO_CHECK_INTERVAL_S = 24 * 3600.0


@dataclass
class ReleaseInfo:
    tag: str                # e.g. "v0.2.0"
    version: str            # e.g. "0.2.0"
    body: str               # release notes (markdown)
    asset_name: str         # installer filename

    def is_newer_than(self, current: str) -> bool:
        return _parse_version(self.version) > _parse_version(current)


def _parse_version(v: str) -> tuple[int, int, int]:
    m = _VERSION_RE.match(v.strip())
    if not m:
        return (0, 0, 0)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def _http_get(url: str, timeout: float = 15.0) -> bytes:
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/vnd.github+json",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(1 << 20)          # a release document is small; never read unbounded


def check_latest_status(timeout: float = 10.0) -> "tuple[ReleaseInfo | None, str]":
    """``(release, "ok")`` or ``(None, status)``; status is one of "no-release", "no-asset:<tag>",
    "rate-limited", "proxy-auth", "http <code>", "network", "bad-response". Never raises."""
    try:
        payload = json.loads(_http_get(RELEASES_LATEST_URL, timeout=timeout))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            log.info("releases/latest: 404 -- no release has been published")
            return None, "no-release"
        if e.code in (403, 429):
            log.info("releases/latest: HTTP %s -- rate limited", e.code)
            return None, "rate-limited"
        if e.code == 407:
            log.info("releases/latest: HTTP 407 -- the proxy asks for credentials")
            return None, "proxy-auth"
        log.info("releases/latest: HTTP %s", e.code)
        return None, f"http {e.code}"
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        log.info("releases/latest: network error (%s)", e.__class__.__name__)   # no traceback
        return None, "network"
    except Exception as e:
        log.info("releases/latest: unusable answer (%r)", e)
        return None, "bad-response"
    if not isinstance(payload, dict):
        return None, "bad-response"

    tag = payload.get("tag_name") or ""
    if payload.get("prerelease") or payload.get("draft") or not _FINAL_TAG_RE.match(tag):
        log.info("releases/latest: %r is a pre-release or not a final tag -- ignored", tag)
        return None, "no-release"
    assets = payload.get("assets") or []
    installer = next((a for a in assets if INSTALLER_RE.match(str(a.get("name") or ""))), None)
    if installer is None:
        log.info("latest release %s has no WindowsFaceUnlock-Setup-*.exe asset", tag)
        return None, f"no-asset:{tag}"
    log.info("releases/latest: %s (current %s)", tag, __version__)
    return ReleaseInfo(tag=tag, version=tag.lstrip("v"), body=str(payload.get("body") or ""),
                       asset_name=str(installer.get("name"))), "ok"


def current_version() -> str:
    return __version__


# ---- the 24-hour gate of the background check ------------------------------------------------

def _state_path() -> Path:
    from face_service.config import APP_DIR
    return APP_DIR / "update_state.json"


def due_for_auto_check(now: "float | None" = None, path: "Path | None" = None) -> bool:
    """True when the last background check is 24 h or more ago (or there was none, or the stored
    time is unusable -- in the future, not a number)."""
    now = time.time() if now is None else now
    path = _state_path() if path is None else path
    try:
        last = float(json.loads(path.read_text(encoding="utf-8")).get("last_check", 0))
    except Exception:
        return True
    if not (0 < last <= now):
        return True
    return now - last >= AUTO_CHECK_INTERVAL_S


def record_auto_check(now: "float | None" = None, path: "Path | None" = None) -> None:
    now = time.time() if now is None else now
    path = _state_path() if path is None else path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps({"last_check": now}), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        log.debug("update state not written", exc_info=True)
