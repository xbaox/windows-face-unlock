"""The tray's windows -- Status, Settings, Help (Stage 9, act 9b R12).

All three are ``Toplevel``s of the tray's single Tk thread (presence_monitor.ui.UiThread): no
window owns an interpreter, and no worker thread holds a window. Pipe calls run through
``ui.run_bg`` and come back to a handler registered under a per-window key; closing a window
unregisters the key, so a late reply is dropped instead of touching a dead widget (F-152, F-163).

Settings (F-153, F-155, F-164..F-168, P-23, D-94):
* two parts -- "Basic" and "Advanced" (collapsed until asked for); developer knobs are not shown
  at all (they stay in config.toml);
* the body scrolls, the Save / Cancel bar is pinned at the bottom and always visible;
* closing with unsaved changes asks first; enums show human labels, never raw codes; every numeric
  field has its own step and format; validation errors name the field as the user sees it;
* the threshold lives only in Advanced, bounded 0.28-0.35, with a warning; a change that weakens
  the security posture is confirmed before it is saved;
* Save writes only the changed keys, applies once, and says honestly whether the service applied
  it.
"""
from __future__ import annotations

import json
import logging
import os
import time
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Callable

from face_service.config import APP_DIR, CONFIG_PATH, LOG_DIR, WATCHDOG_PAUSE_PATH, Config
from face_service.i18n import set_language, shown_languages, t

from .monitor import PresenceMonitor, pipe_call
from .ui import UiThread, bind_standard_keys, px
from .widgets import InfoButton, attach_tooltip

log = logging.getLogger(__name__)

THRESHOLD_UI_MIN, THRESHOLD_UI_MAX = 0.28, 0.35


def _format_age(ts: float) -> str:
    if not ts or ts <= 0:
        return t("status.val.never")
    age = int(time.time() - ts)
    if age < 60:
        return t("status.age.seconds", n=max(0, age))
    if age < 3600:
        return t("status.age.minutes", m=age // 60, s=age % 60)
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def open_log_folder() -> None:
    """R12 / F-156: the logs folder only -- never the data directory beside it."""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        os.startfile(str(LOG_DIR))  # type: ignore[attr-defined]
    except Exception:
        log.exception("open log folder failed")


# ---------------------------------------------------------------------------------------------
# Pure helpers (unit-tested without Tk)
# ---------------------------------------------------------------------------------------------

REFUSAL_KEYS = ("not-owner", "custody", "no-models", "lockout-store-error")


def service_line(status: "dict | None") -> str:
    """The Service row: serving / refusing with the cause / not running."""
    if not status:
        return t("status.val.not_reachable")
    if status.get("state") == "refusing":
        why = str(status.get("why") or "")
        return t("status.val.refusing", why=t(f"why.{why}") if why in REFUSAL_KEYS else why)
    return t("status.val.running")


def result_text(result: str) -> str:
    key = f"status.result.{result}" if result and result != "-" else "status.result.none"
    return t(key)


def strikes_text(snap: dict) -> str:
    """F-205: with auto-lock off there is nothing the count leads to."""
    if not snap.get("auto_lock"):
        return t("status.val.auto_lock_off")
    return f"{snap.get('strikes', 0)} / {snap.get('absent_strikes', 0)}"


def watchdog_line(now: "float | None" = None) -> str:
    """D-126: when the watchdog last wrote its heartbeat (hourly)."""
    now = time.time() if now is None else now
    try:
        hb = WATCHDOG_PAUSE_PATH.with_name("watchdog_heartbeat.json")
        at = float(json.loads(hb.read_text(encoding="utf-8")).get("at", 0))
    except Exception:
        return t("status.val.no_heartbeat")
    if now - at > 2 * 3600:
        return t("status.val.heartbeat_old", age=_format_age(at))
    return t("status.val.heartbeat", age=_format_age(at))


def password_line(status: "dict | None") -> str:
    from face_service.credentials import password_state
    try:
        state, _info = password_state()
    except Exception:
        state = "unreadable"
    if state == "ok" and status and status.get("password_rejected"):
        return t("status.val.pwd_rejected")
    return t(f"status.val.pwd_{state}")


# ---------------------------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------------------------

STATUS_ROWS = [
    ("status.service", "svc"),
    ("status.enrollment", "enroll"),
    ("status.password", "pwd"),
    ("status.auto_lock", "auto"),
    ("status.last", "last"),
    ("status.result", "result"),
    ("status.reason", "reason"),
    ("status.strikes", "strikes"),
    ("status.lock_count", "locks"),
    ("status.paused", "paused"),
    ("status.watchdog", "watchdog"),
]


class StatusWindow:
    kind = "status"

    def __init__(self, ui: UiThread, monitor: PresenceMonitor):
        self.ui, self.monitor = ui, monitor
        self.alive = True
        self._polling = False
        self._key_poll = ui.new_key("status-poll")
        self._key_ping = ui.new_key("status-ping")
        self._key_probe = ui.new_key("status-probe")
        ui.on(self._key_poll, self._refresh)
        ui.on(self._key_ping, self._show_ping)
        ui.on(self._key_probe, self._show_probe)

        self.top = top = tk.Toplevel(ui.root)
        top.title(t("status.title"))
        top.protocol("WM_DELETE_WINDOW", self.close)
        frm = ttk.Frame(top, padding=12)
        frm.pack(fill="both", expand=True)
        self.vars: dict[str, tk.StringVar] = {}
        for i, (label_key, key) in enumerate(STATUS_ROWS):
            ttk.Label(frm, text=t(label_key) + ":", anchor="e").grid(
                row=i, column=0, sticky="e", padx=4, pady=2)   # F-203: natural width
            v = tk.StringVar(master=top, value="…")
            self.vars[key] = v
            ttk.Label(frm, textvariable=v, anchor="w", wraplength=px(top, 380)).grid(
                row=i, column=1, sticky="we", padx=4, pady=2)
            InfoButton(frm, i18n_key=label_key + ".desc").grid(row=i, column=2, sticky="w", padx=4)
        frm.columnconfigure(1, weight=1)
        r = len(STATUS_ROWS)
        ttk.Label(frm, text=t("status.events")).grid(row=r, column=0, columnspan=3, sticky="w",
                                                     pady=(10, 2))
        self.events = tk.Listbox(frm, height=6, activestyle="none")
        self.events.grid(row=r + 1, column=0, columnspan=3, sticky="nsew")
        frm.rowconfigure(r + 1, weight=1)
        btns = ttk.Frame(frm)
        btns.grid(row=r + 2, column=0, columnspan=3, sticky="we", pady=(10, 0))
        for label_key, fn in (("status.btn.ping", self._ping), ("status.btn.probe", self._probe),
                              ("status.btn.open_log", open_log_folder)):
            b = ttk.Button(btns, text=t(label_key), command=fn)
            b.pack(side="left", padx=4)
            attach_tooltip(b, label_key + ".desc")
        ttk.Button(btns, text=t("status.btn.close"), command=self.close).pack(side="right", padx=4)
        bind_standard_keys(top, cancel=self.close)
        top.minsize(px(top, 520), px(top, 420))
        top.focus_set()
        self._tick()

    def _tick(self) -> None:
        if not self.alive:
            return
        if not self._polling:
            self._polling = True
            self.ui.run_bg(pipe_call, {"cmd": "status"}, 2.0, reply=self._key_poll)
        self.top.after(2000, self._tick)

    def _refresh(self, status) -> None:
        self._polling = False
        if not self.alive:
            return
        status = status if isinstance(status, dict) and status.get("ok") else None
        snap = self.monitor.snapshot()
        v = self.vars
        v["svc"].set(service_line(status))
        if status is None:
            v["enroll"].set("—")
        else:
            v["enroll"].set(t("status.val.yes") if status.get("enrollment") else t("status.val.no_enroll"))
        v["pwd"].set(password_line(status))
        v["auto"].set(t("status.val.on") if snap.get("auto_lock") else t("status.val.off"))
        v["last"].set(_format_age(snap.get("last_at", 0)))
        v["result"].set(result_text(snap.get("last_result", "-")))
        why = snap.get("last_why") or ""
        v["reason"].set((t("status.val.camera_why", why=why) if snap.get("last_result") == "unknown"
                         else snap.get("last_reason", "")) or "—")
        v["strikes"].set(strikes_text(snap))
        v["locks"].set(str(snap.get("lock_count", 0)))
        v["paused"].set(t("status.val.yes") if snap.get("paused") else t("status.val.no"))
        v["watchdog"].set(watchdog_line())
        self.events.delete(0, "end")
        for at, msg in snap.get("events", []):
            self.events.insert("end", f"{time.strftime('%H:%M', time.localtime(at))}  {msg}")
        if not snap.get("events"):
            self.events.insert("end", t("status.events.none"))

    def _ping(self) -> None:
        self.ui.run_bg(pipe_call, {"cmd": "ping"}, 3.0, reply=self._key_ping)

    def _show_ping(self, resp) -> None:
        if not self.alive:
            return
        if isinstance(resp, dict) and resp.get("ok"):
            messagebox.showinfo(t("status.btn.ping"), service_line(resp), parent=self.top)
        else:
            messagebox.showwarning(t("status.btn.ping"), t("status.val.not_reachable"), parent=self.top)

    def _probe(self) -> None:
        self.ui.run_bg(pipe_call, {"cmd": "presence"}, 20.0, reply=self._key_probe)

    def _show_probe(self, resp) -> None:
        if not self.alive:
            return
        messagebox.showinfo(t("status.btn.probe"), probe_text(resp), parent=self.top)

    def close(self) -> None:
        self.alive = False
        for k in (self._key_poll, self._key_ping, self._key_probe):
            self.ui.off(k)
        self.ui.forget(self.kind)
        self.vars.clear()
        try:
            self.top.destroy()
        except tk.TclError:
            pass


def probe_text(resp) -> str:
    """F-176: one localized line for a presence probe, the tri-state (and unknown) included."""
    if not isinstance(resp, dict):
        return t("status.val.not_reachable")
    if not resp.get("ok"):
        return t("probe.error", reason=str(resp.get("reason") or "?"))
    state = str(resp.get("state") or ("present" if resp.get("present") else "absent"))
    if state == "unknown":
        return t("probe.unknown", why=str(resp.get("why") or "?"))
    return t(f"probe.{state}") if state in ("present", "absent", "uncertain") else state


# ---------------------------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------------------------

# (name, kind, extras). kind: bool | choice | camera | lang | int | float.
# extras for numbers: (lo, hi, step, fmt); for choice: the codes, labelled via choice.<name>.<code>.
BASIC_FIELDS: list = [
    ("language", "lang", None),
    ("auto_lock", "bool", None),
    ("presence_interval_s", "int", (10, 3600, 10, "%.0f")),
    ("presence_absent_strikes", "int", (1, 20, 1, "%.0f")),
    ("liveness_mode", "choice", ("paranoid", "fast")),
    ("camera_name", "camera", None),
    ("notify_lockout", "bool", None),
    ("notify_service_state", "bool", None),
    ("update_check", "bool", None),
]
ADVANCED_FIELDS: list = [
    ("threshold", "float", (THRESHOLD_UI_MIN, THRESHOLD_UI_MAX, 0.01, "%.2f")),
    ("anti_screen", "bool", None),
    ("max_face_attempts", "int", (1, 20, 1, "%.0f")),
    ("lockout_seconds", "int", (30, 3600, 30, "%.0f")),
    ("presence_mode", "choice", ("recognition", "detection")),
    ("presence_fullscreen_strikes", "int", (0, 120, 1, "%.0f")),
    ("presence_input_idle_s", "float", (0, 3600, 5, "%.0f")),
    ("low_light_boost", "bool", None),
]
SETTINGS_FIELDS = BASIC_FIELDS + ADVANCED_FIELDS


def weakened(old: Config, new: Config) -> "list[str]":
    """P-23: the changes that lower the security posture -- confirmed before saving."""
    out = []
    if new.threshold > old.threshold:
        out.append("field.threshold")
    if old.anti_screen and not new.anti_screen:
        out.append("field.anti_screen")
    if new.max_face_attempts > old.max_face_attempts:
        out.append("field.max_face_attempts")
    if new.lockout_seconds < old.lockout_seconds:
        out.append("field.lockout_seconds")
    if old.presence_mode == "recognition" and new.presence_mode == "detection":
        out.append("field.presence_mode")
    if old.liveness_mode == "paranoid" and new.liveness_mode == "fast":
        out.append("field.liveness_mode")
    return out


def field_error(name: str, kind: str, extras) -> str:
    """F-168: a validation message that names the field as the user sees it."""
    if kind in ("int", "float") and extras:
        lo, hi, _step, fmt = extras
        return t("settings.err.range", field=t(f"field.{name}"), lo=fmt % lo, hi=fmt % hi)
    return t("settings.err.value", field=t(f"field.{name}"))


class SettingsWindow:
    kind = "settings"

    def __init__(self, ui: UiThread, monitor: PresenceMonitor,
                 on_saved: "Callable[[Config], None] | None" = None):
        self.ui, self.monitor, self.on_saved = ui, monitor, on_saved
        self.alive = True
        self.cfg = Config.load()
        self.vars: dict = {}
        self._camera_names: list = []
        self._key_reload = ui.new_key("settings-reload")
        self._key_cams = ui.new_key("settings-cams")
        ui.on(self._key_reload, self._on_reloaded)
        ui.on(self._key_cams, self._on_cameras)

        self.top = top = tk.Toplevel(ui.root)
        top.title(t("settings.title"))
        top.protocol("WM_DELETE_WINDOW", self.cancel)

        # Pinned bar FIRST, packed at the bottom: it can never be pushed off screen (F-153).
        bar = ttk.Frame(top, padding=(12, 8))
        bar.pack(side="bottom", fill="x")
        ttk.Separator(top).pack(side="bottom", fill="x")
        self.save_btn = ttk.Button(bar, text=t("settings.btn.save"), command=self.save, default="active")
        self.save_btn.pack(side="right", padx=4)
        ttk.Button(bar, text=t("settings.btn.cancel"), command=self.cancel).pack(side="right", padx=4)
        self.msg = ttk.Label(bar, text="", wraplength=px(top, 360))
        self.msg.pack(side="left", padx=4)

        # Scrollable body.
        outer = ttk.Frame(top)
        outer.pack(side="top", fill="both", expand=True)
        canvas = tk.Canvas(outer, highlightthickness=0)
        sb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        body = ttk.Frame(canvas, padding=12)
        win = canvas.create_window((0, 0), window=body, anchor="nw")
        body.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(win, width=e.width))
        canvas.bind_all("<MouseWheel>", lambda e: canvas.yview_scroll(int(-e.delta / 120), "units")
                        if str(e.widget).startswith(str(top)) else None)
        self.canvas = canvas

        ttk.Label(body, text=t("settings.editing", path=str(CONFIG_PATH)), foreground="#555",
                  wraplength=px(top, 520)).pack(anchor="w", pady=(0, 8))
        basic = ttk.LabelFrame(body, text=t("settings.basic"), padding=(8, 4))
        basic.pack(fill="x", pady=(0, 8))
        self._build_fields(basic, BASIC_FIELDS)

        self.adv_shown = tk.BooleanVar(master=top, value=False)
        self.adv_btn = ttk.Button(body, text=t("settings.show_advanced"), command=self._toggle_adv)
        self.adv_btn.pack(anchor="w", pady=(0, 6))
        self.adv = ttk.LabelFrame(body, text=t("settings.advanced"), padding=(8, 4))
        ttk.Label(self.adv, text=t("settings.advanced.note"), foreground="#8a5a00",
                  wraplength=px(top, 520), justify="left").grid(row=0, column=0, columnspan=3,
                                                                sticky="w", pady=(0, 6))
        self._build_fields(self.adv, ADVANCED_FIELDS, first_row=1)

        bind_standard_keys(top, ok=self.save, cancel=self.cancel)
        # Initial height capped to the work area; the body scrolls beyond that.
        top.update_idletasks()
        h = min(body.winfo_reqheight() + px(top, 70), int(top.winfo_screenheight() * 0.85))
        top.geometry(f"{max(body.winfo_reqwidth() + px(top, 30), px(top, 560))}x{h}")
        top.focus_set()
        from face_service.camera_devices import list_video_devices
        ui.run_bg(list_video_devices, reply=self._key_cams)

    # ---- construction ----
    def _build_fields(self, parent, fields, first_row: int = 0) -> None:
        parent.columnconfigure(1, weight=1)
        for i, (name, kind, extras) in enumerate(fields, start=first_row):
            label_key = f"field.{name}"
            ttk.Label(parent, text=t(label_key) + ":", anchor="e").grid(
                row=i, column=0, sticky="e", padx=4, pady=3)
            cur = getattr(self.cfg, name)
            if kind == "bool":
                var = tk.BooleanVar(master=self.top, value=bool(cur))
                w = ttk.Checkbutton(parent, variable=var)
            elif kind in ("choice", "lang", "camera"):
                var = tk.StringVar(master=self.top)
                w = ttk.Combobox(parent, textvariable=var, state="readonly", width=46)
                if kind == "choice":
                    labels = [t(f"choice.{name}.{c}") for c in extras]
                    w.configure(values=labels)
                    var.set(t(f"choice.{name}.{cur}") if cur in extras else str(cur))
                elif kind == "lang":
                    langs = list(shown_languages())
                    w.configure(values=[n for _c, n in langs])
                    var.set(dict(langs).get(cur, dict(langs).get("en", "English")))
                else:
                    var.set(cur if cur else t("settings.camera.by_index", i=self.cfg.camera_index))
                    self.camera_combo = w
            elif kind in ("int", "float"):
                lo, hi, step, fmt = extras
                var = tk.StringVar(master=self.top, value=fmt % cur)
                w = ttk.Spinbox(parent, from_=lo, to=hi, increment=step, format=fmt,
                                textvariable=var, width=10)
            else:
                continue
            w.grid(row=i, column=1, sticky="w", padx=4, pady=3)
            InfoButton(parent, i18n_key=label_key + ".desc").grid(row=i, column=2, sticky="w", padx=4)
            attach_tooltip(w, label_key + ".desc")
            self.vars[name] = (kind, extras, var)

    def _toggle_adv(self) -> None:
        if self.adv_shown.get():
            self.adv.pack_forget()
            self.adv_shown.set(False)
            self.adv_btn.configure(text=t("settings.show_advanced"))
        else:
            self.adv.pack(fill="x", pady=(0, 8))
            self.adv_shown.set(True)
            self.adv_btn.configure(text=t("settings.hide_advanced"))

    def _on_cameras(self, devices) -> None:
        if not self.alive or not isinstance(devices, list):
            return
        self._camera_names = [d.name for d in devices if getattr(d, "name", "")]
        values = [t("settings.camera.by_index", i=self.cfg.camera_index)] + self._camera_names
        cur = self.cfg.camera_name
        if cur and cur not in self._camera_names:
            values.append(cur)                      # configured but not connected right now
        self.camera_combo.configure(values=values)

    # ---- collect / validate ----
    def _collect(self) -> "tuple[Config | None, str]":
        """(new config, "") or (None, localized error)."""
        new = Config.load()
        for name, (kind, extras, var) in self.vars.items():
            raw = var.get()
            if kind == "bool":
                val = bool(raw)
            elif kind == "choice":
                codes = {t(f"choice.{name}.{c}"): c for c in extras}
                val = codes.get(raw, getattr(self.cfg, name))
            elif kind == "lang":
                val = {n: c for c, n in shown_languages()}.get(raw, self.cfg.language)
            elif kind == "camera":
                val = raw if raw in self._camera_names or raw == self.cfg.camera_name else ""
            else:
                lo, hi, _step, _fmt = extras
                try:
                    num = float(str(raw).strip().replace(",", "."))
                except ValueError:
                    return None, field_error(name, kind, extras)
                if not (lo <= num <= hi) or (kind == "int" and num != int(num)):
                    return None, field_error(name, kind, extras)
                val = int(num) if kind == "int" else float(num)
            setattr(new, name, val)
        try:
            new.validate()
        except (ValueError, TypeError) as e:
            bad = next((n for n in self.vars if str(e).startswith(n)), None)
            if bad is not None:
                kind, extras, _v = self.vars[bad]
                return None, field_error(bad, kind, extras)
            return None, t("settings.error", err=e)
        return new, ""

    def _changed(self, new: Config) -> "list[str]":
        return [n for n in self.vars if getattr(new, n) != getattr(self.cfg, n)]

    # ---- actions ----
    def save(self) -> None:
        new, err = self._collect()
        if new is None:
            self._say(err, "err")
            return
        keys = self._changed(new)
        if not keys:
            self._say(t("settings.nothing_changed"), "ok")
            return
        weak = weakened(self.cfg, new)
        if weak and not messagebox.askyesno(
                t("settings.confirm_weaken.title"),
                t("settings.confirm_weaken.body", fields="\n".join("• " + t(k) for k in weak)),
                parent=self.top, icon="warning", default="no"):
            return
        try:
            new.save(keys=keys)
        except Exception as e:
            log.exception("settings save failed")
            self._say(t("settings.error", err=e), "err")
            return
        self.cfg = new
        set_language(new.language)
        self.monitor.reload_config(new)          # D-94: applied once, here
        if self.on_saved:
            try:
                self.on_saved(new)
            except Exception:
                log.exception("on_saved callback failed")
        self.save_btn.configure(state="disabled")
        self._say(t("settings.applying"), "ok")
        self.ui.run_bg(pipe_call, {"cmd": "reload_config"}, 5.0, reply=self._key_reload)

    def _on_reloaded(self, resp) -> None:
        if not self.alive:
            return
        self.save_btn.configure(state="normal")
        if isinstance(resp, dict) and resp.get("ok"):
            self._say(t("settings.saved"), "ok")                  # F-164: only when it is true
        else:
            self._say(t("settings.saved_not_applied"), "warn")
        self.monitor.poke_events()

    def _say(self, text: str, level: str) -> None:
        self.msg.configure(text=text,
                           foreground={"ok": "#1a7f37", "warn": "#8a5a00"}.get(level, "#b3261e"))

    def cancel(self) -> None:
        new, _err = self._collect()
        dirty = new is None or bool(self._changed(new))
        if dirty and not messagebox.askyesno(t("settings.discard.title"), t("settings.discard.body"),
                                             parent=self.top, default="no"):
            return
        self.close()

    def close(self) -> None:
        self.alive = False
        self.ui.off(self._key_reload)
        self.ui.off(self._key_cams)
        self.ui.forget(self.kind)
        try:
            self.top.unbind_all("<MouseWheel>")
        except tk.TclError:
            pass
        self.vars.clear()
        try:
            self.top.destroy()
        except tk.TclError:
            pass


# ---------------------------------------------------------------------------------------------
# Help
# ---------------------------------------------------------------------------------------------

HELP_ENTRIES = [
    ("tray.status", "tray.status.desc"),
    ("tray.settings", "tray.settings.desc"),
    ("tray.probe", "tray.probe.desc"),
    ("tray.pause", "tray.pause.desc"),
    ("tray.enroll", "tray.enroll.desc"),
    ("tray.set_password", "tray.set_password.desc"),
    ("tray.open_log", "tray.open_log.desc"),
    ("tray.language", "tray.language.desc"),
    ("tray.check_update", "tray.check_update.desc"),
    ("tray.help", "tray.help.desc"),
    ("tray.quit", "tray.quit.desc"),
    ("help.custody", "help.custody.desc"),
]


class HelpWindow:
    kind = "help"

    def __init__(self, ui: UiThread):
        self.ui = ui
        self.alive = True
        self.top = top = tk.Toplevel(ui.root)
        top.title(t("help.title"))
        top.protocol("WM_DELETE_WINDOW", self.close)
        frm = ttk.Frame(top, padding=12)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text=t("help.intro"), font=("", 10, "bold")).pack(anchor="w", pady=(0, 8))
        text = tk.Text(frm, wrap="word", height=24, width=70, relief="flat",
                       background=top.cget("background"))
        sb = ttk.Scrollbar(frm, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        text.pack(side="left", fill="both", expand=True)
        text.tag_configure("h", font=("", 9, "bold"))
        for label_key, desc_key in HELP_ENTRIES:
            text.insert("end", t(label_key).rstrip("…") + "\n", "h")
            text.insert("end", t(desc_key) + "\n\n")
        text.insert("end", t("help.keyboard") + "\n")
        text.configure(state="disabled")
        ttk.Button(top, text=t("help.close"), command=self.close).pack(side="right", padx=12, pady=(0, 12))
        bind_standard_keys(top, ok=self.close, cancel=self.close)
        top.focus_set()

    def close(self) -> None:
        self.alive = False
        self.ui.forget(self.kind)
        try:
            self.top.destroy()
        except tk.TclError:
            pass


# ---------------------------------------------------------------------------------------------
# Launchers -- called from any thread; the window is built on the Tk thread (R12 single instance)
# ---------------------------------------------------------------------------------------------

def open_status(ui: UiThread, monitor: PresenceMonitor) -> None:
    ui.post(ui.show, "status", lambda u: StatusWindow(u, monitor))


def open_settings(ui: UiThread, monitor: PresenceMonitor,
                  on_saved: "Callable[[Config], None] | None" = None) -> None:
    ui.post(ui.show, "settings", lambda u: SettingsWindow(u, monitor, on_saved))


def open_help(ui: UiThread) -> None:
    ui.post(ui.show, "help", lambda u: HelpWindow(u))


__all__ = ["open_status", "open_settings", "open_help", "open_log_folder", "probe_text",
           "service_line", "strikes_text", "weakened", "field_error", "APP_DIR"]
