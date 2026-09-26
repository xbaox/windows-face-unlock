"""tools/ui_selftest.py -- Stage 9 (act 9b R12-R16) tray, windows, wizard, password, i18n, installer.

No real pipe: every pipe call below is a fake (the product pipe is never touched). Windows that are
built are withdrawn at once and destroyed; the camera is never opened.
  [1] i18n (R16): EN and RU key-for-key; only EN and RU offered; the other ten codes valid and
      English; the glossary holds (no "страйк", "кулдаун", "пайп", "вотчдог", "эмбеддинг", "luma",
      no internal component names); formal Russian prompts (D-102); the first-run language from
      the Windows display language (F-169).
  [2] one Tk thread (F-152): a worker holds only the queue and plain data; a reply for a closed
      window is dropped; closing Status while a slow status call is in flight does not crash;
      a second open raises the first window (F-173).
  [3] Settings (F-153..F-168, P-23): threshold only in Advanced, 0.28-0.35; no developer knobs;
      enum choices labelled; a bad value names the field; weakening changes are listed; nothing
      changed = nothing saved; Save writes only the changed key and says "not applied" when the
      service does not confirm (F-164).
  [4] tray state (F-172, R13): level and line per state; a transparent icon; probe texts carry
      the state (F-176); strikes with auto-lock off (F-205).
  [5] N-24: every outcome of a manual update check has its own dialog text; a lockout notice is
      recorded in the Status events even when no toast is shown (R14 fallback).
  [6] toasts (R14): the XML is escaped and the text travels in the environment, not argv.
  [7] N-10 (F-77): the service releases no credential the sign-in screen would refuse.
  [8] F-108: the stored password is none / ok / unreadable -- three states.
  [9] R12 / F-156: the logs move into logs\\ -- only the log files.
 [10] the wizard: count clamped 5-40, build timeout grows with the shots, rejection reasons
      localised, worker functions never take the window.
 [11] D-95: an unknown flag starts no tray. F-99/F-101: the password dialog refuses on custody.
 [12] installer (R14, R16, F-215) and DPI (R12, F-170): EN/RU message files with equal keys,
      {cm:} texts, the Start-menu AUMID equals the tray's, the tray manifest is PerMonitorV2.

Run:  python -m tools.ui_selftest      Exit 0 = all green.
"""
from __future__ import annotations

import os
import queue
import re
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if not os.environ.get("FACE_UNLOCK_HOME"):
    os.environ["FACE_UNLOCK_HOME"] = tempfile.mkdtemp(prefix="faceunlock_ui_")

REPO = Path(__file__).resolve().parents[1]
FAILS: list = []


def check(name, cond, got=None):
    print(("  ok    " if cond else "  FAIL  ") + name + ("" if cond or got is None else f" (got={got!r})"))
    if not cond:
        FAILS.append(name)


def test_i18n():
    print("[1] i18n")
    import face_service.i18n as I
    check("EN and RU key-for-key", set(I._EN) == set(I._RU), set(I._EN) ^ set(I._RU))
    check("only en / ru translated", set(I.TRANSLATIONS) == {"en", "ru"})
    check("only en / ru offered", [c for c, _ in I.shown_languages()] == ["en", "ru"])
    check("the other ten codes stay valid", len(I.LANG_CODES) == 12 and "vi" in I.LANG_CODES)
    I.set_language("ja")
    check("a hidden code shows English", I.t("tray.quit") == I._EN["tray.quit"])
    I.set_language("en")
    banned = ("страйк", "кулдаун", "пайп", "вотчдог", "эмбеддинг", "luma", "FaceService",
              "Credential Provider", "Моргни ", "Поверни ", "Кивни ")
    hits = [(k, b) for tbl in (I._EN, I._RU) for k, v in tbl.items() for b in banned if b.lower() in v.lower()]
    check("the glossary holds (no jargon, no internal names)", not hits, hits[:5])
    check("RU prompts are formal", I._RU["gesture.prompt.blink"] == "Моргните"
          and I._RU["gesture.prompt.nod"] == "Кивните")
    check("RU lock vs lockout differ", "пауза" in I._RU["field.lockout_seconds"].lower()
          and "блок" not in I._RU["field.lockout_seconds"].lower())
    check("counts are counters (no hard-coded plural)", "{n}" in I._RU["enroll.shots"]
          and "Снимков:" in I._RU["enroll.shots"])
    check("first-run language is en or ru", I.detect_system_language() in ("en", "ru"))
    placeholders = [k for k in I._EN
                    if set(re.findall(r"{(\w+)}", I._EN[k])) != set(re.findall(r"{(\w+)}", I._RU[k]))]
    check("EN and RU carry the same placeholders", not placeholders, placeholders)


class FakePipe:
    def __init__(self, delay=0.0, replies=None):
        self.delay, self.replies, self.calls = delay, replies or {}, []

    def __call__(self, req, timeout_s=30.0):
        self.calls.append(req.get("cmd"))
        if self.delay:
            time.sleep(self.delay)
        return self.replies.get(req.get("cmd"), {"ok": True})


def test_ui_thread():
    print("[2] one Tk thread")
    from presence_monitor.ui import UiThread, _bg_runner
    import inspect
    check("the worker body takes the queue, a key and plain args only",
          list(inspect.signature(_bg_runner).parameters) == ["q", "key", "work", "args"])
    ui = UiThread(name="ui-test")
    check("Tk started", ui.root is not None)
    got = queue.Queue()
    ui.post(ui.on, "k1", got.put)
    ui.run_bg(lambda: 42, reply="k1")
    check("a reply reaches its handler on the Tk thread", got.get(timeout=5) == 42)
    ui.run_bg(lambda: 1, reply="nobody")        # dropped silently
    import presence_monitor.gui as GUI
    import presence_monitor.monitor as M
    from face_service.config import Config
    fake = FakePipe(delay=1.0, replies={"status": {"ok": True, "state": "serving", "enrollment": True,
                                                   "data_dir_secure": True}})
    GUI.pipe_call = fake
    mon = M.PresenceMonitor(Config(language="en"))
    opened = queue.Queue()

    def open_and_close():
        ui.show("status", lambda u: GUI.StatusWindow(u, mon))
        w = ui._windows["status"]
        w.top.withdraw()
        ui.show("status", lambda u: (_ for _ in ()).throw(RuntimeError("second window built")))
        opened.put(ui._windows.get("status") is w)
        w.close()
    ui.post(open_and_close)
    check("a second open raises the first (no second window)", opened.get(timeout=5))
    time.sleep(1.6)                             # the slow status reply lands after the close
    done = queue.Queue()
    ui.post(lambda: done.put(True))
    check("the late reply for a closed window is dropped, Tk thread alive", done.get(timeout=5))
    return ui, mon


def test_settings(ui, mon):
    print("[3] Settings")
    import presence_monitor.gui as GUI
    from face_service.config import Config, CONFIG_PATH
    names = [f[0] for f in GUI.SETTINGS_FIELDS]
    check("threshold only in Advanced, 0.28-0.35",
          "threshold" not in [f[0] for f in GUI.BASIC_FIELDS]
          and dict((f[0], f[2]) for f in GUI.ADVANCED_FIELDS)["threshold"][:2] == (0.28, 0.35))
    check("no developer knobs", not set(names) & {"verify_frames", "verify_required", "camera_index",
                                                 "persistent_camera", "debug_dump_frames",
                                                 "camera_black_luma", "warmup_on_start"})
    old = Config()
    new = Config(threshold=0.34, anti_screen=False, lockout_seconds=60, liveness_mode="fast")
    w = GUI.weakened(old, new)
    check("weakening changes are listed", {"field.threshold", "field.anti_screen", "field.lockout_seconds",
                                           "field.liveness_mode"} <= set(w), w)
    check("a stricter change is not", GUI.weakened(old, Config(threshold=0.30)) == [])
    msg = GUI.field_error("presence_interval_s", "int", (10, 3600, 10, "%.0f"))
    from face_service.i18n import t
    check("a range error names the field as shown", t("field.presence_interval_s") in msg and "3600" in msg, msg)
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text("# mine\nlanguage = \"en\"\nthreshold = 0.32\n", encoding="utf-8")
    fake = FakePipe(replies={"reload_config": None})
    GUI.pipe_call = fake
    out = queue.Queue()

    def run():
        ui.show("settings", lambda u: GUI.SettingsWindow(u, mon))
        s = ui._windows["settings"]
        s.top.withdraw()
        labels = s.vars["liveness_mode"][2]
        combo_vals = [v for v in s.top.nametowidget(str(s.top)).winfo_children()]
        s.vars["presence_interval_s"][2].set("abc")
        cfg, err = s._collect()
        out.put(("bad", cfg, err))
        s.vars["presence_interval_s"][2].set("60")
        s.save()
        out.put(("same", s.msg.cget("text")))
        s.vars["presence_absent_strikes"][2].set("3")
        s.save()
        out.put(("saved", s.msg.cget("text"), labels.get(), len(combo_vals)))
    ui.post(run)
    k, cfg, err = out.get(timeout=10)
    check("a bad number is refused, naming the field", cfg is None and t("field.presence_interval_s") in err, err)
    k, text = out.get(timeout=10)
    check("nothing changed -> nothing saved", text == t("settings.nothing_changed"), text)
    k, text, label, _n = out.get(timeout=10)
    check("the enum shows a label, not the code", label not in ("paranoid", "fast"), label)
    time.sleep(0.5)
    body = CONFIG_PATH.read_text(encoding="utf-8")
    check("only the changed key written, comments kept", "presence_absent_strikes = 3" in body
          and body.startswith("# mine") and "auto_lock" not in body, body)
    done = queue.Queue()
    ui.post(lambda: done.put(ui._windows["settings"].msg.cget("text")))
    check("no confirmation from the service -> 'not applied' (F-164)",
          done.get(timeout=5) == t("settings.saved_not_applied"))
    ui.post(lambda: ui._windows["settings"].close())


def test_tray_state():
    print("[4] tray state")
    from presence_monitor.tray import icon_image, tray_state
    from presence_monitor.gui import probe_text, strikes_text
    from face_service.i18n import t
    ok = {"ok": True, "state": "serving", "enrollment": True}
    check("ready", tray_state(ok, {"auto_lock": False})[0] == "ok")
    check("service down -> off", tray_state(None, {})[0] == "off")
    check("refusing custody -> off, with the reason",
          tray_state({"state": "refusing", "why": "custody"}, {}) == ("off", t("tray.state.refusing", why=t("why.custody"))))
    check("password rejected -> attention",
          tray_state({**ok, "password_rejected": True}, {})[1] == t("tray.state.password_rejected"))
    check("no enrollment -> attention", tray_state({**ok, "enrollment": False}, {})[0] == "attention")
    check("camera cannot see -> off", tray_state(ok, {"last_result": "unknown", "last_why": "busy"})[0] == "off")
    img = icon_image("ok")
    check("the icon is transparent (no white square)", img.mode == "RGBA" and img.getpixel((0, 0))[3] == 0)
    check("probe text carries unknown + why", "busy" in probe_text({"ok": True, "state": "unknown", "why": "busy"}))
    check("probe text: uncertain", probe_text({"ok": True, "state": "uncertain"}) == t("probe.uncertain"))
    check("strikes with auto-lock off say so", strikes_text({"auto_lock": False}) == t("status.val.auto_lock_off"))


def test_notify_fallback():
    print("[5] N-24")
    from presence_monitor.tray import update_result_text
    from presence_monitor.updater import ReleaseInfo
    texts = set()
    for st in ("no-release", "rate-limited", "proxy-auth", "network", "bad-response", "http 502",
               "no-asset:v9.9.9"):
        kind, text = update_result_text(None, st, "0.1.1")
        texts.add(text)
        check(f"{st}: an info dialog", kind == "info" and text)
    check("one text per outcome", len(texts) == 7)
    newer = ReleaseInfo("v9.9.9", "9.9.9", "", "WindowsFaceUnlock-Setup-9.9.9.exe")
    check("newer: a question", update_result_text(newer, "ok", "0.1.1")[0] == "ask")
    check("current: up to date", "0.1.1" in update_result_text(ReleaseInfo("v0.1.1", "0.1.1", "", "x"), "ok", "0.1.1")[1])
    import presence_monitor.monitor as M
    from face_service.config import Config
    m = M.PresenceMonitor(Config(language="en"))
    m.on_event = None
    m._notify("notify_lockout", "paused for 300 s")
    check("an event is kept for Status even without a toast", m.snapshot()["events"][0][1] == "paused for 300 s")


def test_toast():
    print("[6] toasts")
    from presence_monitor import toast
    x = toast.toast_xml("A & B", "<script>")
    check("XML escaped", "&amp;" in x and "&lt;script&gt;" in x)
    seen = {}
    ok = toast.show_toast("T", "secret text", _run=lambda argv, **kw: seen.update(argv=argv, env=kw.get("env")))
    check("started", ok)
    check("the text is not in argv", "secret text" not in " ".join(seen["argv"]))
    check("the text travels base64 in the environment", "FU_TOAST_XML" in seen["env"])


def test_release_caps():
    print("[7] N-10")
    import face_service.service as S
    svc = S.FaceService.__new__(S.FaceService)
    cases = [({"u": "a", "p": "", "d": "."}, False), ({"u": "a", "p": "x" * 1025, "d": "."}, False),
             ({"u": "u" * 257, "p": "x", "d": "."}, False), ({"u": "a", "p": "x", "d": "d" * 257}, False),
             ({"u": "a", "p": "x\x00y", "d": "."}, False), ({"u": "a", "p": "x" * 1024, "d": "."}, True),
             ({"u": "ann@contoso.com", "p": "x", "d": ""}, True)]
    real = S.load_password
    try:
        for rec, want in cases:
            S.load_password = lambda rec=rec: dict(rec)
            got = svc._release_credentials()
            check(f"{list(rec)} lengths {[len(v) for v in rec.values()]} -> {'released' if want else 'no-credentials'}",
                  (got is not None) == want)
    finally:
        S.load_password = real


def test_password_state():
    print("[8] F-108")
    from face_service import credentials as C
    C.clear_password()
    check("none", C.password_state() == ("none", ""))
    C.save_password("alice", "pw", ".")
    check("ok", C.password_state() == ("ok", "alice"))
    C.CREDS_PATH.write_bytes(b"v2:garbage")
    check("damaged -> unreadable", C.password_state()[0] == "unreadable")
    C.CREDS_PATH.write_bytes(b"\x01\x00\x00\x00old")
    check("v1 blob -> unreadable", C.password_state() == ("unreadable", "old format"))
    C.clear_password()


def test_logs():
    print("[9] logs folder")
    from face_service.logging_setup import migrate_logs
    with tempfile.TemporaryDirectory() as td:
        app = Path(td)
        for n in ("service.log", "service.log.1", "presence.log", "watchdog.log.2", "notes.txt",
                  "service.log.bak", "credentials.bin"):
            (app / n).write_text("x", encoding="utf-8")
        moved = migrate_logs(app, app / "logs")
        left = sorted(p.name for p in app.iterdir() if p.is_file())
        check("the four logs moved", moved == 4 and sorted(p.name for p in (app / "logs").iterdir())
              == ["presence.log", "service.log", "service.log.1", "watchdog.log.2"], moved)
        check("nothing else moved", left == ["credentials.bin", "notes.txt", "service.log.bak"], left)
    from face_service.config import LOG_DIR, LOG_PATH
    check("LOG_PATH is inside logs\\", LOG_PATH.parent == LOG_DIR and LOG_DIR.name == "logs")


def test_wizard():
    print("[10] wizard")
    from presence_monitor import enroll_gui as E
    import inspect
    check("count clamped 5-40", (E.clamp_count("1"), E.clamp_count("5000"), E.clamp_count("x"),
                                 E.clamp_count("12")) == (5, 40, 15, 12))
    check("build timeout grows with shots, capped", E.build_timeout_s(5) < E.build_timeout_s(40) <= 600)
    from face_service.i18n import set_language
    set_language("ru")
    h = E.humanize_reason("enrollment rejected: ... Dropped: det<0.5 x2, blur<80 x1. Re-capture")
    set_language("en")
    check("rejection reasons localised (det = hard to make out, not 'recognised')",
          h and "распознано" not in h and "различимо" in h, h)
    for fn in (E.camera_worker, E.build_worker, E.readiness_worker, E.calibrate_worker,
               E.service_wait_worker, E.release_and_check_worker, E.wipe_worker):
        src = inspect.getsource(fn)
        check(f"{fn.__name__} never takes the window", "self" not in inspect.signature(fn).parameters
              and "EnrollWindow" not in src and "root" not in src)


def test_misc():
    print("[11] entry points, custody")
    from presence_monitor.__main__ import main as router
    check("an unknown flag starts nothing", router(["--help"]) == 2)
    from presence_monitor import password_gui as P
    import presence_monitor.monitor as M
    real = M.pipe_call
    q = queue.Queue()
    try:
        M.pipe_call = lambda req, timeout_s=0: {"ok": True, "data_dir_secure": False}
        P._custody_worker(q)
        check("custody failed -> the password dialog refuses", q.get(timeout=2) == ("custody", True))
        M.pipe_call = lambda req, timeout_s=0: None
        P._custody_worker(q)
        check("service not reachable -> not blocked", q.get(timeout=2) == ("custody", False))
    finally:
        M.pipe_call = real


def test_installer():
    print("[12] installer, DPI")
    en = (REPO / "installer" / "lang" / "en.isl").read_text(encoding="utf-8-sig")
    ru = (REPO / "installer" / "lang" / "ru.isl").read_text(encoding="utf-8-sig")
    keys = lambda s: {ln.split("=", 1)[0] for ln in s.splitlines() if "=" in ln and not ln.startswith(";")}
    check("EN / RU installer messages key-for-key", keys(en) == keys(ru) and len(keys(en)) > 15)
    iss = (REPO / "installer" / "installer.iss").read_text(encoding="utf-8")
    check("both languages declared", 'Name: "russian"' in iss and "lang\\ru.isl" in iss)
    used = set(re.findall(r"\{cm:(\w+)\}", iss)) | set(re.findall(r"CustomMessage\('(\w+)'\)", iss))
    check("every custom text used exists", used and used <= keys(en), used - keys(en))
    from presence_monitor.toast import APP_ID
    check("the Start-menu shortcut carries the tray's AUMID", f'AppUserModelID: "{APP_ID}"' in iss)
    spec = (REPO / "installer" / "windows_face_unlock.spec").read_text(encoding="utf-8")
    check("the tray exe declares PerMonitorV2", "PerMonitorV2" in spec and "manifest=TRAY_MANIFEST" in spec)


def main() -> int:
    test_i18n()
    ui, mon = test_ui_thread()
    test_settings(ui, mon)
    test_tray_state()
    test_notify_fallback()
    test_toast()
    test_release_caps()
    test_password_state()
    test_logs()
    test_wizard()
    test_misc()
    test_installer()
    ui.stop()
    print()
    if FAILS:
        print(f"UI SELFTEST FAILED: {len(FAILS)} check(s): {FAILS}")
        return 1
    print("UI SELFTEST OK: EN/RU key-for-key with one glossary; one Tk thread, workers hold no "
          "window; Settings bounded, labelled, honest; tray state, toasts with a fallback; "
          "credential caps; password tri-state; logs folder; wizard helpers; installer EN/RU.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
