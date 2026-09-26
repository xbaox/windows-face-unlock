from __future__ import annotations
import collections
import json
import logging
import os
import threading
import time
from dataclasses import dataclass

import ctypes

from face_service.config import APP_DIR, Config
from face_service.i18n import t

from face_service.session_state import session_locked_wts

from .remote_session import is_remote_context, session_locked

log = logging.getLogger(__name__)


def _lock_workstation() -> bool:
    """LockWorkStation, with its BOOL checked (Stage 9, F-149): a refused lock is logged with the
    Win32 error and reported as not locked, instead of counting a lock that never happened."""
    ok = bool(ctypes.windll.user32.LockWorkStation())
    if not ok:
        log.error("LockWorkStation failed: winerror=%d", ctypes.GetLastError())
    return ok


# Stage 9 (F-147): the tray's Pause survives a tray restart, a logon and a crash. One small file in
# the data directory; present = paused. Written atomically (temp + replace).
PAUSE_PATH = APP_DIR / "presence_paused.json"


def _read_pause(path=None) -> bool:
    path = PAUSE_PATH if path is None else path
    try:
        return bool(json.loads(path.read_text(encoding="utf-8")).get("paused"))
    except FileNotFoundError:
        return False
    except Exception as e:
        log.warning("presence pause file unreadable (%r) -- treating as paused", e)
        return True


def _write_pause(paused: bool, path=None) -> None:
    path = PAUSE_PATH if path is None else path
    try:
        if not paused:
            path.unlink(missing_ok=True)
            return
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps({"paused": True, "at": time.time()}), encoding="utf-8")
        os.replace(tmp, path)
    except Exception as e:
        log.warning("presence pause not persisted: %r", e)


# --- "is this session locked" (7c-7) ---------------------------------------------------------
# The WTS detector (and why the desktop predicate is only its fallback) lives in
# face_service.session_state since Stage 9: the service warms the camera on the same signal.
_session_locked_wts = session_locked_wts


def _is_session_locked() -> bool:
    """The gate's answer. WTS decides when it can; otherwise the old desktop predicate is asked.

    A REPLACEMENT with a safety net rather than an OR of the two: the desktop predicate is the one
    that has been observed wrong here (both silently False while locked, and once True for a single
    sample while unlocked), so it must not be able to override an authoritative answer -- it only
    speaks when WTS has no opinion at all.
    """
    wts = _session_locked_wts()
    if wts is not None:
        return wts
    return session_locked()


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


# The probe states the monitor decides on. Anything else -- "unknown" (Stage 9) or a state this
# monitor has never heard of -- is no decision (act 9b R10).
_DECIDING_STATES = frozenset(("present", "absent", "uncertain"))


def _state_of(resp: dict) -> str:
    """Read the probe verdict out of a presence reply, tolerating a pre-7c-6 service.

    The service gained a tri-state ``state`` in 7c-6 and kept ``present`` with its old meaning. An
    older service sends only ``present``, and folding that to present/absent reproduces exactly the
    behaviour this monitor had before -- uncertainty simply never occurs.
    """
    return resp.get("state") or ("present" if bool(resp.get("present")) else "absent")


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


def _idle_ms(tick: int, last_input: int) -> int:
    """Milliseconds since ``last_input``, as UNSIGNED 32-bit arithmetic.

    Both values are DWORD tick counts that wrap every ~49.7 days, and dwTime wraps with them. A
    plain subtraction goes hugely negative for the ~49.7 days after each wrap, which would read as
    "input arrived just now, forever" -- the exact failure that turns this feature into a permanent
    "user is present" and stops the machine ever locking. Masking to 32 bits is what makes the
    difference correct across the boundary, and it also repairs a signed GetTickCount return.
    """
    return (tick - last_input) & 0xFFFFFFFF


_input_probe_warned = False


def _input_idle_seconds() -> "float | None":
    """Seconds since the last keyboard/mouse input, or None when it cannot be determined.

    None is not "idle forever" and not "active now": it means this probe has no opinion, and the
    caller falls back to the camera exactly as if the feature were off. Failing that way round is
    deliberate -- a broken input probe must never be able to assert presence.
    """
    global _input_probe_warned
    try:
        lii = _LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(_LASTINPUTINFO)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
            raise OSError("GetLastInputInfo returned 0")
        ctypes.windll.kernel32.GetTickCount.restype = ctypes.c_uint32
        tick = int(ctypes.windll.kernel32.GetTickCount())
    except Exception as e:
        if not _input_probe_warned:
            _input_probe_warned = True
            log.warning("input-idle probe unavailable (%s); presence falls back to the camera", e)
        else:
            log.debug("input-idle probe unavailable: %s", e)
        return None
    return _idle_ms(tick, int(lii.dwTime)) / 1000.0


def pipe_call(req: dict, timeout_s: float = 30.0) -> dict | None:
    """Send a JSON request to the FaceService pipe and return the response, or None.

    Stage 8b (F-20). Defect: this connected to whatever owned the pipe name without checking who
    that was, and its ReadFile had no deadline (``timeout_s`` bounded only the connect). The tray,
    the presence loop, the Status window and the enrollment wizard all come through here.
    Consequence: a squatter on the name received their requests, and a server that accepted and
    never answered hung the caller for good. Fix: face_service.pipe_io.exchange -- server SID
    must be SELF or SYSTEM before anything is written, and ``timeout_s`` now bounds the WHOLE
    exchange, read included."""
    from face_service.pipe_io import exchange
    resp, why = exchange(req, timeout_s)
    if resp is None:
        if why in ("no-pipe", "busy"):
            log.warning("FaceService pipe not available (%s)", why)
        else:
            log.warning("pipe call failed: %s", why)
    return resp


# Backwards-compat alias
_pipe_call = pipe_call


@dataclass
class TickSnapshot:
    at: float = 0.0
    result: str = "-"       # present / absent / uncertain / unknown / skipped / error
    reason: str = ""
    strikes: int = 0
    mode: str = ""
    why: str = ""           # Stage 9: the cause of an "unknown" (busy / leased / not-found / ...)


class PresenceMonitor:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._stop = threading.Event()
        self._paused = threading.Event()
        self.on_update = None       # set by the tray: called when the shown state changes
        self._pause_path = PAUSE_PATH
        if _read_pause(self._pause_path):
            self._paused.set()          # F-147: a Pause outlives the tray process
            log.info("presence monitor starts paused (persisted pause)")
        self._strikes = 0
        # True once THIS absence episode has been logged as running on the fullscreen threshold,
        # so the switch is announced once per episode instead of on every tick. Cleared with the
        # strikes it belongs to -- see _reset_strikes.
        self._fs_episode = False
        # Consecutive "uncertain" probes (7c-6): a near face that anti-screen flagged. Counted
        # separately from strikes because uncertainty must NOT lock on its own; it only converts
        # into strikes once the run is long enough to stop looking like a camera artefact.
        self._uncertain = 0
        # Whether the LAST tick found the session locked, so the locked->unlocked edge can be
        # noticed once (7c-7) instead of being re-announced every interval.
        self._was_locked = False
        self._last = TickSnapshot()
        self._lock_count = 0
        self._state_lock = threading.Lock()
        # Event-notification dedup state: one toast per lockout episode, one
        # per reachability transition (baseline set silently on first tick).
        self._svc_reachable: bool | None = None
        self._lockout_notified = False
        # Stage 9 (act 9b R14): every event also lands here -- the Status window's "Recent events"
        # list is the channel that works when a toast is not shown. And the last status reply, so
        # the tray can show the service's state (refusing + why, a rejected password) without a
        # second poll of its own.
        self.events: "collections.deque" = collections.deque(maxlen=20)
        self.on_event = None        # set by the tray: (gate, message) -> shows the toast
        self._last_status: "dict | None" = None

    def _reset_strikes(self) -> None:
        """End the current absence episode. The strike count, the uncertain run and the
        fullscreen-episode flag are one state, so they are cleared in one place and can never
        drift apart -- including after a lock, so the next episode starts from zero."""
        self._strikes = 0
        self._uncertain = 0
        self._fs_episode = False

    def pause(self) -> None:
        self._paused.set()
        _write_pause(True, self._pause_path)
        log.info("presence monitor paused")

    def resume(self) -> None:
        self._paused.clear()
        _write_pause(False, self._pause_path)
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
        was_on = self.cfg.auto_lock
        self.cfg = cfg
        self._reset_strikes()
        if was_on and not cfg.auto_lock:
            log.info("auto-lock switched off: presence probes stop")
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
                "last_why": last.why,
                "last_mode": last.mode,
                "interval_s": self.cfg.presence_interval_s,
                "absent_strikes": self.cfg.presence_absent_strikes,
                "mode": self.cfg.presence_mode,
                "auto_lock": bool(self.cfg.auto_lock),
                "events": list(self.events),
            }

    def _set_last(self, result: str, reason: str = "", mode: str = "", why: str = "") -> None:
        with self._state_lock:
            changed = (self._last.result, self._last.why) != (result, why)
            self._last = TickSnapshot(
                at=time.time(),
                result=result,
                reason=reason,
                strikes=self._strikes,
                mode=mode or self.cfg.presence_mode,
                why=why,
            )
        cb = self.on_update
        if changed and cb is not None:
            try:
                cb()                      # the tray repaints its icon and tooltip
            except Exception:
                log.debug("on_update callback failed", exc_info=True)

    def _notify(self, gate: str, message: str) -> None:
        """Record the event (always) and hand it to the tray for a toast (gated by config there)."""
        with self._state_lock:
            self.events.appendleft((time.time(), message))
        cb = self.on_event
        if cb is None:
            return
        try:
            cb(gate, message)
        except Exception:
            log.exception("event notification failed")

    def record_event(self, message: str) -> None:
        """An event for the Status list only (no toast) -- e.g. the result of a manual check."""
        with self._state_lock:
            self.events.appendleft((time.time(), message))

    def last_status(self) -> "dict | None":
        with self._state_lock:
            return self._last_status

    def _check_service_events(self) -> bool:
        """Event toasts from the EXISTING ``status`` command (cheap, no
        camera): service reachable<->unreachable transitions and the start of
        a face-lockout episode. Returns whether the service answered, so
        _tick can skip the camera probe instead of burning a second timeout
        on a dead pipe."""
        resp = pipe_call({"cmd": "status"}, timeout_s=5.0)
        reachable = bool(resp and resp.get("ok"))
        def _sig(st):
            return None if not st else (st.get("state"), st.get("why"), st.get("password_rejected"),
                                        st.get("enrollment"))
        with self._state_lock:
            changed = _sig(self._last_status) != _sig(resp if reachable else None)
            self._last_status = resp if reachable else None
        if changed and self.on_update is not None:
            try:
                self.on_update()          # the tray's state line follows the service (R12, R13)
            except Exception:
                log.debug("on_update callback failed", exc_info=True)
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
            # _is_session_locked, not session_locked: this gate exists so a toast is never burned
            # on the lock screen, and the desktop predicate answers False there on this hardware
            # (7c-7), which defeated it. The authoritative detector restores the 7c-3 intent.
            elif not self._lockout_notified and not _is_session_locked():
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
        #    probe (so the LED stays dark while nobody is there), no confirmation re-probe and no
        #    strike. See _is_session_locked for why the detector changed in 7c-7.
        if _is_session_locked():
            if not self._was_locked:
                self._was_locked = True
                log.info("session locked: skipping presence ticks until it is unlocked")
            else:
                log.debug("skip: session is locked")
            self._reset_strikes()
            self._set_last("skipped", "session-locked")
            return
        if self._was_locked:
            # The belt to the gate's braces. A strike earned just before the lock -- or during it,
            # if the gate was bypassed by a detector that could not tell -- must not be spent on
            # the session the user just unlocked with their face. This also covers a manual Win+L,
            # where strikes accrued before the lock would otherwise survive it.
            self._was_locked = False
            log.info("session unlocked: presence counters reset (%s)", self._fmt_counters())
            self._reset_strikes()

        # Stage 9 (F-154): the status poll -- the one source of the lockout / service toasts and of
        # the tray's state line -- runs on EVERY unlocked tick, before the input gate. It used to
        # run only on idle ticks, so a user who kept typing after a PIN sign-in never saw the
        # lockout notice. It is a cheap pipe round trip and never touches the camera.
        reachable = self._check_service_events()

        # Stage 9 (F-147): Pause is checked BEFORE the input gate, so Status says "paused" -- not
        # "present src=input" -- while it is on.
        if self._paused.is_set():
            self._reset_strikes()
            self._set_last("skipped", "paused")
            return

        # Stage 9 (act 9b R10): auto_lock off means presence is off -- not a single camera probe.
        if not self.cfg.auto_lock:
            self._reset_strikes()
            self._set_last("skipped", "auto-lock-off")
            return

        # 1. Live input IS presence (7c-8). Typing or moving the mouse proves the user is at the
        #    machine far better than a frame does, and it proves it while they are looking at a
        #    phone, reading something on the desk, or simply turned away -- all of which take the
        #    face out of frame and used to be counted as absence. A tick answered by input never
        #    wakes the camera (the status poll above is the only pipe call it makes). Beyond the idle
        #    threshold nothing changes and the camera decides exactly as before.
        idle_limit = self.cfg.presence_input_idle_s
        if idle_limit > 0:
            idle = _input_idle_seconds()
            if idle is not None and idle < idle_limit:
                self._reset_strikes()
                log.info("presence tick: state=present src=input idle=%.1fs/%.0fs %s d4=-",
                         idle, idle_limit, self._fmt_counters())
                self._set_last("present", f"src=input idle={idle:.0f}s/{idle_limit:.0f}s")
                return

        # (The status poll and Pause are handled at the top of the tick since Stage 9.)

        # 2. Skip if this is a remote session — face check doesn't make sense
        #    when nobody is physically at the machine.
        remote, reason = is_remote_context()
        if remote:
            log.info("skip: remote context (%s)", reason)
            self._reset_strikes()
            self._set_last("skipped", reason)
            return

        # NOTE (rewritten in 7c-8; it used to say the opposite). Input is now consulted, but ONLY
        # as evidence of presence and only above in step 1 -- it can end a tick as present, never
        # as absent. Reaching here means the input probe stayed silent, so the walk-away property
        # is unchanged: with nobody typing, an unrecognised face still locks the machine even if
        # someone else is sitting at it.

        # 3. Probe camera via FaceService
        if not reachable:
            # Service down (status poll above already burned the wait) —
            # don't lock blindly and don't repeat the wait on the camera probe.
            log.warning("presence probe: service unavailable; skipping")
            self._set_last("error", "service-unavailable")
            return
        resp = pipe_call({"cmd": "presence"}, timeout_s=20.0)
        if resp is None or not resp.get("ok"):
            # Service down — don't lock blindly. Stage 8b (F-05). Defect: only a MISSING reply was
            # skipped; {"ok": false, "reason": ...} (a handler exception, an engine error) went on
            # to _state_of, which read no "state" and no "present" as ABSENT. Consequence: a
            # failing service earned an absence strike per tick and, with auto_lock on, locked a
            # user who was sitting right there. Fix: a refusal is not evidence either -- skip, no
            # strike, exactly like an unreachable service.
            why = "service-unavailable" if resp is None else f"service-error {resp.get('reason')}"
            log.warning("presence probe: %s; skipping", why)
            self._set_last("error", why)
            return

        state = _state_of(resp)
        mode = resp.get("mode", self.cfg.presence_mode)
        log.info("presence probe: state=%s src=camera real=%s mode=%s %s d4=-",
                 state, resp.get("real"), mode, self._fmt_counters())
        if state not in _DECIDING_STATES:
            self._no_decision(resp, mode, "probe")
            return

        if state == "present":
            counters = self._fmt_counters()      # as OBSERVED, before the reset zeroes them
            self._reset_strikes()
            self._set_last("present", f"src=camera real={resp.get('real')} {counters} d4=-", mode)
            return

        if state == "uncertain":
            self._on_uncertain(mode, "probe", "-")
            return

        # --- state == "absent": confirm before spending a strike (7c-6 / D4) ------------------
        # A lock is expensive and a single bad burst is cheap to re-check, so an absent answer is
        # asked again after a short wait. _stop.wait -- never time.sleep: a Quit arriving mid-wait
        # must end the tick immediately, and it must NOT go on to lock on the way out.
        delay = self.cfg.presence_confirm_delay_s
        if delay <= 0:
            self._award_strike(mode, "absent", "-")   # confirmation disabled: old behaviour
            return
        if self._stop.wait(delay):
            log.debug("absence confirmation abandoned: monitor stopping")
            return
        resp2 = pipe_call({"cmd": "presence"}, timeout_s=20.0)
        if resp2 is None or not resp2.get("ok"):
            # Same rule as the first probe: an unreachable -- or refusing (Stage 8b, F-05) --
            # service is not evidence of absence.
            why = ("service-unavailable" if resp2 is None
                   else f"service-error {resp2.get('reason')}")
            log.warning("absence confirmation: %s; skipping", why)
            self._set_last("error", f"{why} {self._fmt_counters()} d4=unreachable")
            return
        state2 = _state_of(resp2)
        if state2 not in _DECIDING_STATES:
            self._no_decision(resp2, mode, "confirm")
            return
        if state2 == "present":
            counters = self._fmt_counters()          # as OBSERVED, before the reset zeroes them
            log.info("absence retracted by confirmation probe after %.1fs: state=present %s "
                     "d4=retracted", delay, counters)
            self._reset_strikes()
            self._set_last("present", f"confirm: retracted {counters} d4=retracted", mode)
            return
        if state2 == "uncertain":
            self._on_uncertain(mode, "confirm", "uncertain")
            return
        self._award_strike(mode, "absent confirmed", "confirmed")

    def _no_decision(self, resp: dict, mode: str, origin: str) -> None:
        """Stage 9 (act 9b R10, F-143): the service could not see ("unknown": busy, leased, not
        connected, black, a hung read) -- or answered a state this monitor does not know. Neither
        presence nor absence: the counters stay as they are, and Status shows the cause."""
        why = str(resp.get("why") or resp.get("state") or "?")
        log.info("presence tick: state=%s src=camera (%s) why=%s %s -- no decision",
                 resp.get("state"), origin, why, self._fmt_counters())
        self._set_last("unknown", f"src=camera why={why} ({origin})", mode, why=why)

    def _fmt_counters(self, limit: "int | None" = None) -> str:
        """The two running counters, in the one format every tick outcome reports them in.

        ``limit`` is passed in only by _award_strike, which is where the fullscreen-aware threshold
        is resolved; everywhere else the ordinary threshold is the one in force.
        """
        return "streak=%d/%d strikes=%d/%s" % (
            self._uncertain, self.cfg.presence_uncertain_streak, self._strikes,
            self.cfg.presence_absent_strikes if limit is None else limit)

    def _on_uncertain(self, mode: str, origin: str, d4: str) -> None:
        """One uncertain probe: never locks by itself, but a long enough run converts.

        The run is deliberately NOT cleared on conversion -- once it is long enough, every further
        uncertain probe earns another strike, so a persistent signal still converges on a lock at
        the normal cadence instead of stalling forever one step below the bar.
        """
        self._uncertain += 1
        m = self.cfg.presence_uncertain_streak
        if self._uncertain < m:
            log.info("presence tick: state=uncertain src=camera (%s) %s d4=%s — not counting an absence",
                     origin, self._fmt_counters(), d4)
            self._set_last("uncertain",
                           f"src=camera {self._fmt_counters()} d4={d4} ({origin})", mode)
            return
        # The run IS the confirmation, so this path deliberately skips the D4 re-probe.
        log.info("presence tick: state=uncertain src=camera (%s) %s d4=%s — run converts to an absence strike",
                 origin, self._fmt_counters(), d4)
        self._award_strike(mode, f"uncertain {self._uncertain}/{m}", d4)

    def _award_strike(self, mode: str, why: str, d4: str) -> None:
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
        counters = self._fmt_counters(limit)
        log.info("presence tick: state=absent src=camera %s d4=%s [%s]", counters, d4, why)
        self._set_last(
            "absent",
            (f"src=camera {counters} d4={d4} [{why}]{suffix}" if limit > 0
             else f"src=camera {counters} d4={d4} (fullscreen: never lock) [{why}]{suffix}"),
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
            if _lock_workstation():            # F-149: count only a lock that happened
                with self._state_lock:
                    self._lock_count += 1
            else:
                self._set_last("error", "lock-failed", mode)

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
    from face_service.logging_setup import setup_logging
    setup_logging(LOG_PATH.with_name("presence.log"))
    cfg = Config.load()
    from .tray import run_with_tray
    run_with_tray(cfg)

# (Stage 9, D-95: no __main__ block -- `python -m presence_monitor` is the one entry point, and it
# holds the tray's single-instance mutex.)
