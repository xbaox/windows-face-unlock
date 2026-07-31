from __future__ import annotations
import json
import logging
import threading
import time
from dataclasses import dataclass, field

import ctypes
import pywintypes  # type: ignore
import win32file  # type: ignore

from face_service.config import Config, PIPE_NAME
from face_service.i18n import t

from .remote_session import is_remote_context, session_locked

log = logging.getLogger(__name__)


def _lock_workstation() -> None:
    ctypes.windll.user32.LockWorkStation()


# QUERY_USER_NOTIFICATION_STATE (shellapi.h). Only the three below mean "something is owning the
# whole screen"; the rest (1 = not present, 5 = accepts notifications, 6 = quiet time,
# 7 = running Windows Store app) do not, and 4 in that header is QUNS_PRESENTATION_MODE.
_QUNS_BUSY = 2                       # a full-screen application is running (non-D3D)
_QUNS_RUNNING_D3D_FULL_SCREEN = 3    # a full-screen D3D application -- i.e. a game
_QUNS_PRESENTATION_MODE = 4          # presentation mode
_FULLSCREEN_STATES = frozenset((_QUNS_BUSY, _QUNS_RUNNING_D3D_FULL_SCREEN, _QUNS_PRESENTATION_MODE))


def _fullscreen_active() -> bool:
    """True while a full-screen app or presentation mode owns the screen.

    ``SHQueryUserNotificationState`` is the very signal Windows uses to decide whether a toast may
    pop, so it already understands games, full-screen video and presentation mode -- far better
    than anything we would reconstruct from window rectangles. Signature:
    ``HRESULT SHQueryUserNotificationState(QUERY_USER_NOTIFICATION_STATE *pquns)`` -- one out
    parameter, S_OK (0) on success.

    FAIL-OPEN by design: no shell32, a non-S_OK HRESULT or an unexpected state all return False,
    which puts the caller back on the ORDINARY absence threshold. This guard can therefore only
    ever make locking less eager, never more -- a broken probe degrades to the status quo.
    """
    try:
        state = ctypes.c_int(0)
        hr = ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(state))
        if hr != 0:
            log.debug("SHQueryUserNotificationState returned hr=0x%08X", hr & 0xFFFFFFFF)
            return False
        return state.value in _FULLSCREEN_STATES
    except Exception as e:
        log.debug("SHQueryUserNotificationState failed: %s", e)
        return False


def _state_of(resp: dict) -> str:
    """Read the probe verdict out of a presence reply, tolerating a pre-7c-6 service.

    The service gained a tri-state ``state`` in 7c-6 and kept ``present`` with its old meaning. An
    older service sends only ``present``, and folding that to present/absent reproduces exactly the
    behaviour this monitor had before -- uncertainty simply never occurs.
    """
    return resp.get("state") or ("present" if bool(resp.get("present")) else "absent")


def _get_idle_seconds() -> float:
    """Seconds since last user input (keyboard/mouse)."""
    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

    lii = LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
        return 0.0
    tick = ctypes.windll.kernel32.GetTickCount()
    return (tick - lii.dwTime) / 1000.0


def pipe_call(req: dict, timeout_s: float = 30.0) -> dict | None:
    """Send a JSON request to the FaceService pipe and return the response."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            handle = win32file.CreateFile(
                PIPE_NAME,
                win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                0, None, win32file.OPEN_EXISTING, 0, None,
            )
            break
        except pywintypes.error:
            time.sleep(0.2)
    else:
        log.warning("FaceService pipe not available")
        return None

    try:
        win32file.WriteFile(handle, (json.dumps(req) + "\n").encode("utf-8"))
        _hr, data = win32file.ReadFile(handle, 65536)
        return json.loads(data.decode("utf-8").strip())
    except Exception as e:
        log.warning("pipe call failed: %s", e)
        return None
    finally:
        try:
            win32file.CloseHandle(handle)
        except Exception:
            pass


# Backwards-compat alias
_pipe_call = pipe_call


@dataclass
class TickSnapshot:
    at: float = 0.0
    result: str = "-"       # present / absent / skipped / error
    reason: str = ""
    strikes: int = 0
    mode: str = ""


class PresenceMonitor:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._strikes = 0
        # True once THIS absence episode has been logged as running on the fullscreen threshold,
        # so the switch is announced once per episode instead of on every tick. Cleared with the
        # strikes it belongs to -- see _reset_strikes.
        self._fs_episode = False
        # Consecutive "uncertain" probes (7c-6): a near face that anti-screen flagged. Counted
        # separately from strikes because uncertainty must NOT lock on its own; it only converts
        # into strikes once the run is long enough to stop looking like a camera artefact.
        self._uncertain = 0
        self._last = TickSnapshot()
        self._lock_count = 0
        self._state_lock = threading.Lock()
        # Event-notification dedup state: one toast per lockout episode, one
        # per reachability transition (baseline set silently on first tick).
        self._svc_reachable: bool | None = None
        self._lockout_notified = False

    def _reset_strikes(self) -> None:
        """End the current absence episode. The strike count, the uncertain run and the
        fullscreen-episode flag are one state, so they are cleared in one place and can never
        drift apart -- including after a lock, so the next episode starts from zero."""
        self._strikes = 0
        self._uncertain = 0
        self._fs_episode = False

    def pause(self) -> None:
        self._paused.set()
        log.info("presence monitor paused")

    def resume(self) -> None:
        self._paused.clear()
        self._reset_strikes()
        log.info("presence monitor resumed")
        self.poke_events()

    def poke_events(self) -> None:
        """Out-of-band _check_service_events (after settings Save / tray
        Resume) so event toasts don't wait for the next tick. Daemon thread —
        never blocks the caller; a rare race with the tick thread costs at
        worst one duplicate toast."""
        threading.Thread(target=self._check_service_events,
                         name="event-poll", daemon=True).start()

    def is_paused(self) -> bool:
        return self._paused.is_set()

    def stop(self) -> None:
        self._stop.set()

    def reload_config(self, cfg: Config) -> None:
        self.cfg = cfg
        self._reset_strikes()
        log.info(
            "presence monitor config reloaded: interval=%ss strikes=%s mode=%s",
            cfg.presence_interval_s, cfg.presence_absent_strikes, cfg.presence_mode,
        )

    def snapshot(self) -> dict:
        with self._state_lock:
            last = self._last
            return {
                "paused": self._paused.is_set(),
                "strikes": self._strikes,
                "lock_count": self._lock_count,
                "last_at": last.at,
                "last_result": last.result,
                "last_reason": last.reason,
                "last_mode": last.mode,
                "interval_s": self.cfg.presence_interval_s,
                "absent_strikes": self.cfg.presence_absent_strikes,
                "mode": self.cfg.presence_mode,
            }

    def _set_last(self, result: str, reason: str = "", mode: str = "") -> None:
        with self._state_lock:
            self._last = TickSnapshot(
                at=time.time(),
                result=result,
                reason=reason,
                strikes=self._strikes,
                mode=mode or self.cfg.presence_mode,
            )

    def _notify(self, gate: str, message: str) -> None:
        try:
            from .tray import notify_event  # lazy: tray imports this module
            notify_event(gate, message)
        except Exception:
            log.exception("event notification failed")

    def _check_service_events(self) -> bool:
        """Event toasts from the EXISTING ``status`` command (cheap, no
        camera): service reachable<->unreachable transitions and the start of
        a face-lockout episode. Returns whether the service answered, so
        _tick can skip the camera probe instead of burning a second timeout
        on a dead pipe."""
        resp = pipe_call({"cmd": "status"}, timeout_s=5.0)
        reachable = bool(resp and resp.get("ok"))
        prev, self._svc_reachable = self._svc_reachable, reachable
        if prev is not None and prev != reachable:
            self._notify(
                "notify_service_state",
                t("notify.service_up") if reachable else t("notify.service_down"),
            )
        if reachable:
            lock = resp.get("lockout") or {}
            locked = bool(lock.get("locked"))
            if not locked:
                # Episode over (or never started) -> re-arm for the next one.
                self._lockout_notified = False
            elif not self._lockout_notified and not session_locked():
                self._notify(
                    "notify_lockout",
                    t("notify.lockout", s=int(lock.get("remaining_s", 0))),
                )
                self._lockout_notified = True
            # else: the episode is live but the workstation is locked. Do NOT latch
            # the flag. A Shell_NotifyIcon balloon is invisible on the lock screen
            # and the OS does not queue it, so firing here would burn the toast
            # silently -- and a lockout episode ALWAYS starts locked, because it is
            # raised by the SYSTEM/LogonUI unlock path. Leaving the flag clear makes
            # the first poll after the user unlocks raise it, with a fresh
            # remaining_s from that poll's own status response.
        return reachable

    def _tick(self) -> None:
        # 0. Already on the lock screen? Then there is nothing left to guard: the machine is in the
        #    state this monitor exists to reach. Skip the WHOLE tick — no status poll, no camera
        #    probe (so the LED stays dark while nobody is there) — and end the absence episode, so
        #    the count starts fresh when the user comes back. A failing predicate reports False
        #    (remote_session.session_locked degrades that way on purpose), which is the status quo.
        if session_locked():
            log.debug("skip: session is locked")
            self._reset_strikes()
            self._set_last("skipped", "session-locked")
            return

        # 1. Event toasts ride on the existing `status` poll — before the
        #    pause/remote skips, so notifications work while paused too.
        reachable = self._check_service_events()

        # 1. Skip if paused from the tray
        if self._paused.is_set():
            self._set_last("skipped", "paused")
            return

        # 2. Skip if this is a remote session — face check doesn't make sense
        #    when nobody is physically at the machine.
        remote, reason = is_remote_context()
        if remote:
            log.info("skip: remote context (%s)", reason)
            self._reset_strikes()
            self._set_last("skipped", reason)
            return

        # NOTE: We deliberately do NOT skip based on keyboard/mouse input.
        # The goal is pure face-based presence: if the enrolled face is not
        # in front of the camera, lock — even if someone else is actively
        # using the machine (stronger "walk-away" security).

        # 3. Probe camera via FaceService
        if not reachable:
            # Service down (status poll above already burned the wait) —
            # don't lock blindly and don't repeat the wait on the camera probe.
            log.warning("presence probe: service unavailable; skipping")
            self._set_last("error", "service-unavailable")
            return
        resp = pipe_call({"cmd": "presence"}, timeout_s=20.0)
        if resp is None:
            # Service down — don't lock blindly
            log.warning("presence probe: service unavailable; skipping")
            self._set_last("error", "service-unavailable")
            return

        state = _state_of(resp)
        mode = resp.get("mode", self.cfg.presence_mode)
        log.info("presence probe: state=%s real=%s mode=%s",
                 state, resp.get("real"), mode)

        if state == "present":
            self._reset_strikes()
            self._set_last("present", f"real={resp.get('real')}", mode)
            return

        if state == "uncertain":
            self._on_uncertain(mode, "probe")
            return

        # --- state == "absent": confirm before spending a strike (7c-6 / D4) ------------------
        # A lock is expensive and a single bad burst is cheap to re-check, so an absent answer is
        # asked again after a short wait. _stop.wait -- never time.sleep: a Quit arriving mid-wait
        # must end the tick immediately, and it must NOT go on to lock on the way out.
        delay = self.cfg.presence_confirm_delay_s
        if delay <= 0:
            self._award_strike(mode, "absent")      # confirmation disabled: old behaviour
            return
        if self._stop.wait(delay):
            log.debug("absence confirmation abandoned: monitor stopping")
            return
        resp2 = pipe_call({"cmd": "presence"}, timeout_s=20.0)
        if resp2 is None:
            # Same rule as the first probe: an unreachable service is not evidence of absence.
            log.warning("absence confirmation: service unavailable; skipping")
            self._set_last("error", "service-unavailable")
            return
        state2 = _state_of(resp2)
        if state2 == "present":
            log.info("absence retracted by confirmation probe after %.1fs", delay)
            self._reset_strikes()
            self._set_last("present", "confirm: retracted", mode)
            return
        if state2 == "uncertain":
            self._on_uncertain(mode, "confirm")
            return
        self._award_strike(mode, "absent confirmed")

    def _on_uncertain(self, mode: str, origin: str) -> None:
        """One uncertain probe: never locks by itself, but a long enough run converts.

        The run is deliberately NOT cleared on conversion -- once it is long enough, every further
        uncertain probe earns another strike, so a persistent signal still converges on a lock at
        the normal cadence instead of stalling forever one step below the bar.
        """
        self._uncertain += 1
        m = self.cfg.presence_uncertain_streak
        if self._uncertain < m:
            log.info("presence uncertain (%s) %d/%d — not counting an absence",
                     origin, self._uncertain, m)
            self._set_last("uncertain", f"uncertain {self._uncertain}/{m} ({origin})", mode)
            return
        # The run IS the confirmation, so this path deliberately skips the D4 re-probe.
        log.info("presence uncertain (%s) %d/%d — run converts to an absence strike",
                 origin, self._uncertain, m)
        self._award_strike(mode, f"uncertain {self._uncertain}/{m}")

    def _award_strike(self, mode: str, why: str) -> None:
        """Spend one absence strike, and lock if that reaches the threshold.

        The threshold choice and the lock itself are unchanged from 7c-3 -- only HOW a strike is
        earned moved. Which threshold this absence is judged against is probed only here: a present
        user costs nothing extra, and the answer only ever matters on this path. A fullscreen app or
        presentation mode means the user is demonstrably AT the machine and simply not facing the
        camera, so the walk-away threshold is the wrong one; 0 withholds the lock entirely.
        """
        self._strikes += 1
        limit = self.cfg.presence_absent_strikes
        if _fullscreen_active():
            limit = self.cfg.presence_fullscreen_strikes
            if not self._fs_episode:
                self._fs_episode = True
                log.info("fullscreen/presentation active — absence threshold %d -> %s",
                         self.cfg.presence_absent_strikes,
                         limit if limit > 0 else "never (0 = no lock while fullscreen)")
        suffix = "" if self.cfg.auto_lock else " (auto-lock off)"
        self._set_last(
            "absent",
            (f"strike {self._strikes}/{limit} [{why}]{suffix}" if limit > 0
             else f"strike {self._strikes} (fullscreen: never lock) [{why}]{suffix}"),
            mode,
        )
        if limit > 0 and self._strikes >= limit:
            if not self.cfg.auto_lock:
                # Observe-only: strikes count and show up in Status (with an
                # honest "auto-lock off" reason), the machine stays unlocked.
                log.info("absent %d ticks [%s] — auto_lock off, not locking", self._strikes, why)
                self._reset_strikes()
                return
            log.warning("absent %d ticks [%s] — locking workstation", self._strikes, why)
            self._reset_strikes()
            with self._state_lock:
                self._lock_count += 1
            _lock_workstation()

    def run(self) -> None:
        log.info("presence monitor loop: interval=%ss strikes=%s mode=%s",
                 self.cfg.presence_interval_s,
                 self.cfg.presence_absent_strikes,
                 self.cfg.presence_mode)
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:
                log.exception("tick error")
                self._set_last("error", "exception")
            # wake up sooner if stop is signalled
            self._stop.wait(self.cfg.presence_interval_s)


def main() -> None:
    from face_service.config import LOG_PATH
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(LOG_PATH.with_name("presence.log"), encoding="utf-8"),
                  logging.StreamHandler()],
    )
    cfg = Config.load()
    from .tray import run_with_tray
    run_with_tray(cfg)


if __name__ == "__main__":
    main()
