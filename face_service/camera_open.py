"""Camera-open retry and per-attempt wait ceiling (Stage 3 / Step 4, extended in 7b-2).

Two pieces, both camera- and service-free so they stay unit-testable with a fake ``open_fn``
(see ``tools.camera_busy_selftest``):

* ``open_with_retry`` -- the original pure loop. It bounds how many attempts are made and refuses
  to START another once the wall-clock budget is spent. What it does NOT bound is an attempt that
  has already begun: the budget is only ever checked BETWEEN attempts, because a plain call cannot
  be interrupted from the outside. Unchanged, and still used directly by the probes/harnesses.

* ``BoundedOpener`` -- adds the missing half: a hard ceiling on how long we WAIT for one attempt.
  Each attempt runs on its own thread and is joined with a timeout, so a webcam driver wedged
  inside a native ``VideoCapture()``/``read()`` can no longer stall the (sequential) pipe server
  behind it.

The honest limit of both: a native call cannot be cancelled. Blowing the ceiling stops us WAITING,
not the call -- it may still be running, and the device may stay busy until it returns. What
``BoundedOpener`` does guarantee is that a capture produced by an abandoned attempt is released by
that same thread instead of leaking, and that no second attempt is launched while the first is
still in flight.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable

log = logging.getLogger(__name__)


def open_with_retry(open_fn: Callable[[], bool], *, retries, pause_s, timeout_s,
                    clock, sleep) -> bool:
    """Try ``open_fn()`` until it succeeds or the budget is spent. Returns True on success, else
    False (camera busy).

    Attempts = 1 initial try + ``retries`` extra (so ``retries`` is the number of RE-tries; 0 means
    a single attempt). An attempt succeeds when ``open_fn()`` returns truthy without raising; a
    raised exception OR a falsy return counts as a failed attempt (never propagated). Between
    attempts it pauses ``pause_s`` -- but only while attempts remain AND the elapsed
    ``clock() - start`` is still under ``timeout_s`` (once the budget is spent no further attempt
    is STARTED).

    Scope, stated plainly: ``timeout_s`` bounds how many attempts are made, not how long one takes.
    The check sits BETWEEN attempts, after ``open_fn()`` has already returned, so a single call that
    blocks for minutes blocks this loop for minutes -- a plain call cannot be interrupted from the
    outside. Bounding that is ``BoundedOpener``'s job below. ``clock``/``sleep`` are injected, so
    tests drive it without real time.
    """
    attempts = 1 + max(0, int(retries))
    start = clock()
    for i in range(attempts):
        try:
            if open_fn():
                return True
        except Exception:
            pass
        if i + 1 >= attempts:
            break
        if clock() - start >= timeout_s:
            break
        sleep(pause_s)
    return False


class BoundedOpener:
    """Run camera-open attempts with a hard ceiling on how long each one is WAITED for.

    ``open_with_retry`` above bounds the attempt COUNT; this bounds the attempt DURATION, which is
    the half that actually matters when a webcam driver wedges: on this hardware neither
    ``CAP_PROP_OPEN_TIMEOUT_MSEC`` nor ``CAP_PROP_READ_TIMEOUT_MSEC`` is honored, so a native
    ``VideoCapture()`` or ``read()`` can block for minutes, and the pipe server -- which handles one
    request at a time -- is stuck behind it for exactly that long.

    Each attempt runs on a daemon thread and is joined for at most ``cap_s``. What that buys and
    what it does not:

      * it does NOT cancel the native call. Nothing can; the call runs to completion on its thread
        and the device may stay busy until it does. Blowing the ceiling stops us WAITING, no more.
      * a capture the abandoned attempt eventually produces is released BY THAT THREAD, inside the
        thread, before it exits -- so a late success does not leak a handle nobody owns.
      * while that thread is alive, a further ``open`` is refused outright rather than racing a
        second capture onto the same device.

    Blowing the ceiling also ABORTS the retry loop. Retrying would mean waiting on the same stuck
    device again, turning one ``cap_s`` stall into ``(1 + retries) * cap_s`` for the same answer.

    NOT designed for concurrent ``open`` calls from several threads: the in-flight worker is single
    state, so two callers would interleave over it. The one production caller is the sequential pipe
    server (``FaceService._acquire_camera``, itself under ``_cam_lock``), which never does that.

    ``clock``/``sleep`` are injectable for tests; the join timeout is real time either way, since a
    thread join cannot be driven by a fake clock.
    """

    def __init__(self, *, clock=time.monotonic, sleep=time.sleep):
        self._clock = clock
        self._sleep = sleep
        self._worker: "threading.Thread | None" = None
        # Set when the CURRENT worker's result is no longer wanted; the worker reads it to decide
        # whether it must close the capture it produced. Kept as instance state (rather than purely
        # local) so the in-flight attempt is inspectable.
        self._abandoned: "threading.Event | None" = None

    def open(self, open_fn: "Callable[[float], bool]", close_fn: "Callable[[], None]", *,
             retries, pause_s, timeout_s, cap_s) -> bool:
        """Attempt ``open_fn(deadline)`` until it succeeds, the budget is spent, or one wedges.

        ``open_fn`` takes ONE argument: a monotonic deadline, freshly computed as ``clock() + cap_s``
        for EVERY attempt, so a cooperative opener can give up on its own before the ceiling has to.
        ``close_fn`` takes none and MUST be idempotent -- it is called on an abandoned attempt
        regardless of what that attempt returned, and it may run concurrently with the caller having
        moved on.

        Returns True on success, False on a busy device, a wedged attempt, or a refusal because a
        previous attempt is still in flight. Never raises: an ``open_fn`` that throws counts as a
        failed attempt, exactly as in ``open_with_retry``.
        """
        prev = self._worker
        if prev is not None and prev.is_alive():
            # Do not even call open_fn: a second capture on a device the previous attempt may still
            # be holding is how you turn one wedged handle into two.
            log.warning("previous camera open still in flight; refusing to start another")
            return False

        attempts = 1 + max(0, int(retries))
        start = self._clock()
        for i in range(attempts):
            ok, wedged = self._attempt(open_fn, close_fn, cap_s)
            if ok:
                return True
            if wedged:
                return False          # stuck device: another attempt is just more waiting
            if i + 1 >= attempts:
                break
            if self._clock() - start >= timeout_s:
                break
            self._sleep(pause_s)
        return False

    def _attempt(self, open_fn, close_fn, cap_s):
        """One attempt. Returns ``(opened, wedged)``; ``wedged`` means the ceiling was blown.

        The handoff between "the worker published a result" and "the caller gave up" is taken under
        one lock, so exactly one of the two happens: either the caller sees ``done`` and owns the
        result, or it marks the attempt abandoned and the worker -- which then sees that flag under
        the same lock -- owns the cleanup. Without the lock the worker could finish in the instant
        between the join timing out and the flag being set, and the capture would leak.
        """
        abandoned = threading.Event()
        lock = threading.Lock()
        outcome = {"done": False, "ok": False}
        started = self._clock()
        deadline = started + cap_s

        def _run():
            try:
                ok = bool(open_fn(deadline))
            except Exception:
                log.exception("camera open attempt raised; counting it as a failed open")
                ok = False
            with lock:
                outcome["done"] = True
                outcome["ok"] = ok
                give_back = abandoned.is_set()
            if not give_back:
                return
            # Nobody is waiting for this any more. Release whatever it produced -- close_fn is
            # idempotent, so calling it after a FAILED attempt is a no-op rather than a special
            # case. This runs before the thread exits on purpose: while it is alive, open() refuses
            # to start another attempt, so the close can never race a fresh capture.
            try:
                close_fn()
            except Exception:
                log.exception("releasing an abandoned camera open failed")
            log.info("wedged camera open completed after %.1fs; released abandoned capture",
                     self._clock() - started)

        th = threading.Thread(target=_run, name="camera-open", daemon=True)
        self._worker = th
        self._abandoned = abandoned
        th.start()
        th.join(cap_s)
        with lock:
            if outcome["done"]:
                return outcome["ok"], False
            abandoned.set()
        log.warning("camera open wedged after %.1fs; abandoning attempt, not retrying", cap_s)
        return False, True
