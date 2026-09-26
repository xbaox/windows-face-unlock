"""One Tk thread for the tray process (Stage 9, act 9b R12).

Before Stage 9 every tray window (Status, Settings, Help, the update prompt) created its OWN
``tk.Tk()`` root on its own thread, and the worker threads of a window called ``root.after`` and
held the window through bound methods. Closing a window while a worker was in flight left the last
reference -- and with it the Tcl interpreter -- to be finalized on the worker thread: the
``Tcl_AsyncDelete`` abort that takes the whole tray down (F-152). A second root created by the
update check became tkinter's default root and broke fonts in the next window (F-158).

Now there is exactly one Tk interpreter, created and used on ONE thread (``UiThread``), hidden,
living for the whole process. Windows are ``Toplevel``s of it. Every other thread talks to it
through a thread-safe queue that the Tk thread drains with ``after``:

* ``ui.post(fn, *args)`` -- run ``fn(*args)`` on the Tk thread (the tray's menu handlers use it);
* ``ui.run_bg(work, *args, reply=key)`` -- run ``work(*args)`` on a worker thread and deliver its
  result to the handler registered under ``key`` on the Tk thread. The worker holds only the
  queue, the key string and the plain arguments -- never a window or a Tk object -- so nothing a
  worker drops can finalize Tk state, and a reply for a window that has closed meanwhile is simply
  discarded (no handler under that key any more).

Single instance per window kind (R12): ``ui.show(kind, factory)`` raises the existing window
instead of opening a second one (F-173).

DPI (R12, F-170): the process is made per-monitor-v2 aware before the first window exists (the
frozen exe also carries it in its manifest) and ``tk scaling`` is set from the real DPI, so text is
sharp at 125-200 % instead of bitmap-stretched.
"""
from __future__ import annotations

import ctypes
import itertools
import logging
import queue
import threading
import tkinter as tk
from typing import Callable

log = logging.getLogger(__name__)

_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4


def enable_dpi_awareness() -> str:
    """Make this process DPI-aware (idempotent; call before the first Tk root). Returns the mode
    that took effect, for the log. Per-monitor-v2 first, then per-monitor, then system-aware."""
    try:
        user32 = ctypes.windll.user32
        fn = getattr(user32, "SetProcessDpiAwarenessContext", None)
        if fn is not None:
            fn.argtypes = [ctypes.c_void_p]
            if fn(ctypes.c_void_p(_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)):
                return "per-monitor-v2"
    except Exception:
        pass
    try:
        if ctypes.windll.shcore.SetProcessDpiAwareness(2) == 0:
            return "per-monitor"
    except Exception:
        pass
    try:
        if ctypes.windll.user32.SetProcessDPIAware():
            return "system"
    except Exception:
        pass
    return "unaware (already set, or not supported)"


def window_dpi(widget) -> int:
    """The DPI of the monitor ``widget`` is on (96 = 100 %)."""
    try:
        hwnd = widget.winfo_id()
        dpi = int(ctypes.windll.user32.GetDpiForWindow(ctypes.c_void_p(hwnd)))
        if dpi > 0:
            return dpi
    except Exception:
        pass
    try:
        return int(ctypes.windll.user32.GetDpiForSystem()) or 96
    except Exception:
        return 96


def apply_scaling(root) -> float:
    """Set ``tk scaling`` (pixels per point) from the real DPI; returns the factor vs 96 dpi.
    Fonts are given in points, so this is what makes them the right physical size."""
    dpi = window_dpi(root)
    try:
        root.tk.call("tk", "scaling", dpi / 72.0)
    except Exception:
        log.debug("tk scaling failed", exc_info=True)
    return dpi / 96.0


def px(widget, n: float) -> int:
    """``n`` logical (96-dpi) pixels at this window's DPI."""
    return int(round(n * window_dpi(widget) / 96.0))


def bind_standard_keys(win, *, ok: "Callable[[], None] | None" = None,
                       cancel: "Callable[[], None] | None" = None) -> None:
    """R12 keyboard: Enter runs the default action, Escape the close/cancel action."""
    if ok is not None:
        win.bind("<Return>", lambda _e: ok())
        win.bind("<KP_Enter>", lambda _e: ok())
    if cancel is not None:
        win.bind("<Escape>", lambda _e: cancel())


def raise_window(win) -> None:
    """Bring a window to the front (deiconify, lift, focus). Never raises."""
    try:
        win.deiconify()
        win.lift()
        win.attributes("-topmost", True)
        win.after(200, lambda: _untop(win))
        win.focus_force()
    except Exception:
        log.debug("raise_window failed", exc_info=True)


def _untop(win) -> None:
    try:
        win.attributes("-topmost", False)
    except Exception:
        pass


def _bg_runner(q: "queue.Queue", key: str, work: Callable, args: tuple) -> None:
    """Worker body. Holds the queue, the reply key and plain arguments only."""
    try:
        result = work(*args)
    except Exception as e:           # a worker never raises into the void
        log.exception("background task %s failed", key)
        result = e
    q.put(("reply", key, result))


class UiThread:
    """The process's single Tk thread. Create once; everything else goes through its queue."""

    POLL_MS = 40

    def __init__(self, name: str = "ui"):
        self._q: "queue.Queue" = queue.Queue()
        self._handlers: dict = {}
        self._windows: dict = {}
        self._ids = itertools.count(1)
        self._ready = threading.Event()
        self.root: "tk.Tk | None" = None
        self.scale = 1.0
        self._thread = threading.Thread(target=self._main, name=name, daemon=True)
        self._thread.start()
        self._ready.wait(10)

    # ---- any thread -------------------------------------------------------------------------
    def post(self, fn: Callable, *args) -> None:
        """Run ``fn(*args)`` on the Tk thread."""
        self._q.put(("call", fn, args))

    def run_bg(self, work: Callable, *args, reply: str) -> None:
        """Run ``work(*args)`` on a worker; its result (or exception) goes to the ``reply``
        handler on the Tk thread -- if that handler still exists by then."""
        threading.Thread(target=_bg_runner, args=(self._q, reply, work, args),
                         name=f"bg-{reply}", daemon=True).start()

    def new_key(self, prefix: str) -> str:
        return f"{prefix}-{next(self._ids)}"

    # ---- Tk thread only ----------------------------------------------------------------------
    def on(self, key: str, handler: Callable) -> None:
        self._handlers[key] = handler

    def off(self, key: str) -> None:
        self._handlers.pop(key, None)

    def show(self, kind: str, factory: Callable) -> None:
        """Open the single window of ``kind``, or raise it when it is already open (R12)."""
        win = self._windows.get(kind)
        if win is not None and getattr(win, "alive", False):
            raise_window(win.top)
            return
        try:
            win = factory(self)
        except Exception:
            log.exception("opening the %s window failed", kind)
            return
        self._windows[kind] = win

    def forget(self, kind: str) -> None:
        self._windows.pop(kind, None)

    def _main(self) -> None:
        try:
            self.root = tk.Tk()
            self.root.withdraw()
            self.scale = apply_scaling(self.root)
        except Exception:
            log.exception("Tk could not start; the tray windows are unavailable")
            self._ready.set()
            return
        self._ready.set()
        self.root.after(self.POLL_MS, self._drain)
        try:
            self.root.mainloop()
        except Exception:
            log.exception("Tk main loop ended with an error")

    def _drain(self) -> None:
        try:
            while True:
                item = self._q.get_nowait()
                kind = item[0]
                try:
                    if kind == "call":
                        item[1](*item[2])
                    elif kind == "reply":
                        handler = self._handlers.get(item[1])
                        if handler is not None:
                            handler(item[2])
                except Exception:
                    log.exception("UI callback failed")
        except queue.Empty:
            pass
        try:
            self.root.after(self.POLL_MS, self._drain)
        except Exception:
            pass

    def stop(self) -> None:
        def _quit():
            try:
                self.root.quit()
            except Exception:
                pass
        self.post(_quit)
