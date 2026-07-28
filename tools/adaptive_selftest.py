"""tools/adaptive_selftest.py -- Stage 3 / Step 2 anti-poisoning proof (no camera/GPU).

Drives the REAL Recognizer.maybe_adapt + AdaptiveStore + adaptive.evaluate against
synthetic embeddings placed at controlled cosine distances from a synthetic
enrollment, and asserts the anti-poisoning policy holds:

  * a genuine mildly-drifted frame (within the ceiling of ENROLLMENT) is added, and
    that adaptation lets a further-drifted genuine frame unlock where it couldn't
    before -- WITHOUT letting that further frame itself into the gallery;
  * a spoof (screen flag), a static frame (not live), an impostor / too-far frame,
    and (in paranoid mode) a gesture-less frame are all refused, each with the
    right reason token;
  * drift cannot "hop": a frame close to a previously-added adaptive embedding but
    far from the ORIGINAL enrollment is still refused (the ceiling is anchored to
    enrollment, not the augmented gallery);
  * cooldown rate-limits additions; the size cap FIFO-evicts; the adaptive store
    round-trips to disk and clears back to the enrollment baseline (rollback).

No insightface, no camera -- just numpy geometry and the shipping policy code.
Run from the repo root:
    python -m tools.adaptive_selftest
"""
from __future__ import annotations

import math
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from face_service.config import Config
from face_service import adaptive as A
from face_service import recognizer as RC
from face_service.recognizer import Recognizer

D = 512


def _basis(i: int) -> np.ndarray:
    e = np.zeros(D, dtype=np.float32)
    e[i] = 1.0
    return e


E0 = _basis(0)  # the enrollment identity direction


def at_dist(d: float, k: int) -> np.ndarray:
    """Unit vector at cosine distance ``d`` from E0, tilted along basis axis ``k`` (k>=1)."""
    cs = 1.0 - d
    v = cs * E0 + math.sqrt(max(0.0, 1.0 - cs * cs)) * _basis(k)
    return v.astype(np.float32)


def cdist(a: np.ndarray, b: np.ndarray) -> float:
    na = a / (np.linalg.norm(a) + 1e-9)
    nb = b / (np.linalg.norm(b) + 1e-9)
    return float(1.0 - np.dot(na, nb))


def make_cfg(**over) -> Config:
    cfg = Config()
    cfg.threshold = 0.32
    cfg.adaptive_gallery = True
    cfg.adaptive_margin = 0.17          # ceiling = 0.15
    cfg.adaptive_max_size = 10
    cfg.adaptive_cooldown_s = 1800.0
    for k, v in over.items():
        setattr(cfg, k, v)
    return cfg


def fresh(cfg: Config) -> Recognizer:
    rec = Recognizer(cfg)
    rec._enroll_refs = E0.reshape(1, D).copy()   # single-vector enrollment => exact distances
    rec.clear_adaptive()                          # isolate from any prior run's file
    return rec


def matches(rec: Recognizer, v: np.ndarray) -> tuple[bool, float]:
    """Replicate analyze_frame's matcher over the current matching set (enroll + adaptive)."""
    best = min(cdist(v, r) for r in rec._refs)
    return best <= rec.cfg.threshold, best


class T:
    def __init__(self):
        self.fail = 0

    def ok(self, cond, msg):
        print(("  ok  " if cond else "  FAIL") + "  " + msg)
        if not cond:
            self.fail += 1


def main(argv=None) -> int:
    t = T()
    ceiling = 0.32 - 0.17
    print(f"ceiling = threshold - adaptive_margin = {ceiling:.2f}  "
          f"(self<=0.124, replay~0.155, impostor~0.97)\n")

    # --- 1) pure evaluate(): every gate branch fires with the right reason ---
    print("[1] evaluate() gate coverage")
    cfg = make_cfg()
    base = dict(threshold=0.32, cfg=cfg, liveness_passed=True, is_screen=False,
                mode="fast", gesture_passed=False, now=10_000.0,
                last_adapt_ts=0.0, adaptive_count=0)

    def ev(**o):
        return A.evaluate(o.pop("distance", 0.10), **{**base, **o})

    t.ok(ev(distance=0.10).accept and ev(distance=0.10).reason == "ok", "in-ceiling live frame -> ok")
    t.ok(ev(cfg=make_cfg(adaptive_gallery=False)).reason == "disabled", "toggle off -> disabled")
    t.ok(ev(liveness_passed=False).reason == "not-live", "not live -> not-live")
    t.ok(ev(is_screen=True).reason == "screen", "screen flag -> screen (kills replay)")
    t.ok(ev(mode="paranoid").reason == "paranoid-no-gesture", "paranoid, no gesture -> refused")
    t.ok(ev(mode="paranoid", gesture_passed=True).accept, "paranoid + gesture -> ok")
    t.ok(ev(distance=0.20).reason == "distance>ceiling", "0.20 > ceiling -> refused")
    t.ok(ev(distance=0.90).reason == "distance>ceiling", "impostor 0.90 -> refused")
    t.ok(ev(distance=0.10, last_adapt_ts=9_500.0, adaptive_count=1).reason == "cooldown",
         "within cooldown -> cooldown")
    t.ok(ev(distance=0.10, last_adapt_ts=1_000.0, adaptive_count=1).accept, "past cooldown -> ok")

    # --- 2) genuine drift is added AND improves recognition; the far frame is NOT added ---
    print("\n[2] adaptation improves recognition, stays anchored")
    rec = fresh(make_cfg())
    F = at_dist(0.35, 1)          # genuine but drifted PAST threshold along axis 1
    pre_match, pre = matches(rec, F)
    t.ok((not pre_match) and abs(pre - 0.35) < 1e-3, f"before: F@0.35 does NOT unlock (best={pre:.3f})")
    Dm = at_dist(0.14, 1)         # mild drift on the SAME ray, within ceiling
    dec = rec.maybe_adapt(Dm, liveness_passed=True, is_screen=False, mode="fast", now=0.0)
    t.ok(dec.accept and rec._adaptive.count == 1, "mild drift D@0.14 added (store=1)")
    post_match, post = matches(rec, F)
    t.ok(post_match and post < ceiling, f"after: F@0.35 now unlocks via D (best={post:.3f})")
    # F itself must never enter the gallery: its distance to ENROLLMENT (0.35) exceeds the ceiling
    dec_f = rec.maybe_adapt(F, liveness_passed=True, is_screen=False, mode="fast", now=1e9)
    t.ok((not dec_f.accept) and dec_f.reason == "distance>ceiling" and rec._adaptive.count == 1,
         "F@0.35 refused for the gallery (enroll-distance > ceiling) -> no outward creep")

    # --- 3) drift cannot hop through a previously-added adaptive embedding ---
    print("\n[3] no drift-hopping (ceiling anchored to enrollment, not the augmented set)")
    rec = fresh(make_cfg())
    D1 = at_dist(0.14, 1)
    t.ok(rec.maybe_adapt(D1, liveness_passed=True, is_screen=False, mode="fast", now=0.0).accept,
         "D1@0.14 (from enroll) added")
    D2 = at_dist(0.28, 1)         # 0.28 from enroll, but close to D1 on the same ray
    near_d1 = cdist(D2, D1)
    dec2 = rec.maybe_adapt(D2, liveness_passed=True, is_screen=False, mode="fast", now=1e9)
    t.ok(near_d1 < ceiling, f"D2 is close to D1 (dist={near_d1:.3f} < ceiling)")
    t.ok((not dec2.accept) and dec2.reason == "distance>ceiling" and rec._adaptive.count == 1,
         "yet D2 refused: gate uses distance to ENROLLMENT (0.28), not to D1 -> no hop")

    # --- 4) spoof / static refused via the real maybe_adapt even when distance is fine ---
    print("\n[4] spoof & static refused through Recognizer.maybe_adapt")
    rec = fresh(make_cfg())
    good = at_dist(0.10, 2)
    t.ok(rec.maybe_adapt(good, liveness_passed=True, is_screen=True, mode="fast", now=0.0).reason
         == "screen" and rec._adaptive.count == 0, "close+live but SCREEN flag -> refused (replay-proof)")
    t.ok(rec.maybe_adapt(good, liveness_passed=False, is_screen=False, mode="fast", now=0.0).reason
         == "not-live" and rec._adaptive.count == 0, "close but NOT LIVE (photo) -> refused")

    # --- 5) cooldown rate-limits real additions ---
    print("\n[5] cooldown")
    rec = fresh(make_cfg(adaptive_cooldown_s=1800.0))
    t.ok(rec.maybe_adapt(at_dist(0.10, 1), liveness_passed=True, is_screen=False, mode="fast", now=0.0).accept,
         "first add at t=0 -> ok")
    t.ok(rec.maybe_adapt(at_dist(0.10, 2), liveness_passed=True, is_screen=False, mode="fast", now=60.0).reason
         == "cooldown", "second add at t=60s (<1800) -> cooldown")
    t.ok(rec.maybe_adapt(at_dist(0.10, 3), liveness_passed=True, is_screen=False, mode="fast", now=3600.0).accept,
         "third add at t=3600s -> ok")

    # --- 6) size cap FIFO-evicts, keeping the most recent ---
    print("\n[6] size cap (FIFO)")
    rec = fresh(make_cfg(adaptive_max_size=3, adaptive_cooldown_s=0.0))
    added = []
    for i in range(5):
        v = at_dist(0.10, i + 1)
        rec.maybe_adapt(v, liveness_passed=True, is_screen=False, mode="fast", now=float(i))
        added.append(v)
    t.ok(rec._adaptive.count == 3, f"after 5 adds, store capped at 3 (got {rec._adaptive.count})")
    kept_last3 = all(any(np.allclose(row, added[j], atol=1e-5) for row in rec._adaptive.embeddings)
                     for j in (2, 3, 4))
    dropped_first2 = not any(any(np.allclose(row, added[j], atol=1e-5) for row in rec._adaptive.embeddings)
                             for j in (0, 1))
    t.ok(kept_last3 and dropped_first2, "kept the last 3, evicted the oldest 2")

    # --- 7) persistence round-trip + rollback ---
    print("\n[7] persist round-trip & rollback")
    rec = fresh(make_cfg(adaptive_cooldown_s=0.0))
    for i in range(3):
        rec.maybe_adapt(at_dist(0.10, i + 1), liveness_passed=True, is_screen=False, mode="fast", now=float(i))
    saved = rec._adaptive.embeddings.copy()
    store2 = A.AdaptiveStore(rec._adaptive.path)
    t.ok(store2.load() and store2.count == 3 and np.allclose(store2.embeddings, saved),
         "adaptive.npz reloads identical embeddings")
    rec.clear_adaptive()
    t.ok(rec._adaptive.count == 0 and (not rec._adaptive.path.exists()),
         "clear_adaptive() empties the ring and deletes the file")
    t.ok(rec._enroll_refs is not None and rec._enroll_refs.shape == (1, D)
         and np.allclose(rec._refs, E0.reshape(1, D)),
         "enrollment baseline intact; matching set falls back to enrollment only")

    # --- 8) AdaptiveStore.load(): graceful degradation, never fatal (G1) ---
    print("\n[8] AdaptiveStore.load() degradation -> False, empty store, no exception")
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        # (a) missing required keys
        p = tdp / "a.npz"
        np.savez(p, foo=np.zeros(3, dtype=np.float32))
        s = A.AdaptiveStore(p)
        t.ok(s.load() is False and s.count == 0, "missing keys -> False, empty")
        # (b) engine mismatch
        p = tdp / "b.npz"
        np.savez(p, embeddings=np.zeros((2, A.EMBED_DIM), np.float32),
                 ts=np.zeros(2, np.float64), engine=np.array("someone-else"),
                 dim=np.array(A.EMBED_DIM, dtype=np.int64))
        s = A.AdaptiveStore(p)
        t.ok(s.load() is False and s.count == 0, "engine mismatch -> False, empty")
        # (c) dim mismatch
        p = tdp / "c.npz"
        np.savez(p, embeddings=np.zeros((2, 256), np.float32), ts=np.zeros(2, np.float64),
                 engine=np.array(A.ENGINE_TAG), dim=np.array(256, dtype=np.int64))
        s = A.AdaptiveStore(p)
        t.ok(s.load() is False and s.count == 0, "dim mismatch -> False, empty")
        # (d) bad embeddings shape (tag says 512, rows are 128-wide)
        p = tdp / "d.npz"
        np.savez(p, embeddings=np.zeros((2, 128), np.float32), ts=np.zeros(2, np.float64),
                 engine=np.array(A.ENGINE_TAG), dim=np.array(A.EMBED_DIM, dtype=np.int64))
        s = A.AdaptiveStore(p)
        t.ok(s.load() is False and s.count == 0, "bad embeddings shape -> False, empty")
        # (e) garbage bytes (not an npz)
        p = tdp / "e.npz"
        p.write_bytes(b"not-an-npz-file\x00\x01\x02")
        s = A.AdaptiveStore(p)
        t.ok(s.load() is False and s.count == 0, "corrupt bytes -> False, empty (no raise)")

    # --- 9) ceiling boundary is strict (>) and margin>=threshold fails closed (G2) ---
    print("\n[9] ceiling boundary + fail-closed ceiling")
    cfgb = make_cfg()                      # threshold 0.32, margin 0.17 -> ceiling 0.15
    ceil = 0.32 - 0.17
    baseb = dict(threshold=0.32, cfg=cfgb, liveness_passed=True, is_screen=False,
                 mode="fast", gesture_passed=False, now=10_000.0,
                 last_adapt_ts=0.0, adaptive_count=0)
    t.ok(A.evaluate(ceil, **baseb).accept, f"distance == ceiling ({ceil:.2f}) -> accepted (strict >)")
    t.ok(A.evaluate(ceil + 1e-6, **baseb).reason == "distance>ceiling",
         "distance == ceiling + 1e-6 -> refused")
    basez = {**baseb, "cfg": make_cfg(adaptive_margin=0.32)}   # ceiling = 0.32 - 0.32 = 0.0
    t.ok(A.evaluate(0.0, **basez).reason == "distance>ceiling",
         "margin >= threshold => ceiling <= 0 => even distance 0.0 refused (fail-closed)")

    # --- 10) toggle-read: adaptive_gallery=False does NOT merge a persisted ring (G3, guards F2) ---
    print("\n[10] toggle off => matching set is enrollment-only even if adaptive.npz exists")
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        emb_path, adp_path = tdp / "embeddings.npz", tdp / "adaptive.npz"
        np.savez(emb_path, embeddings=E0.reshape(1, D),
                 engine=np.array(RC.ENGINE_TAG), dim=np.array(RC.EMBED_DIM, dtype=np.int64))
        s = A.AdaptiveStore(adp_path)
        s.add(at_dist(0.10, 1), now=1.0, max_size=10)
        s.save()
        orig_embed = RC.EMBED_PATH
        try:
            RC.EMBED_PATH = emb_path
            rec = Recognizer(make_cfg(adaptive_gallery=False))
            rec._adaptive = A.AdaptiveStore(adp_path)
            t.ok(rec.load() and rec._refs.shape[0] == 1 and rec._adaptive.count == 0,
                 "toggle OFF: ring NOT loaded, matching set = enrollment only (1 row)")
            rec2 = Recognizer(make_cfg(adaptive_gallery=True))
            rec2._adaptive = A.AdaptiveStore(adp_path)
            t.ok(rec2.load() and rec2._refs.shape[0] == 2 and rec2._adaptive.count == 1,
                 "toggle ON: same files -> ring merged, matching set = enroll + adaptive (2 rows)")
        finally:
            RC.EMBED_PATH = orig_embed

    # --- 11) Recognizer.load() file combinations (G4) ---
    print("\n[11] load() combinations: enroll-only / orphan-adaptive / both")
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        emb_path, adp_path = tdp / "embeddings.npz", tdp / "adaptive.npz"

        def _write_enroll():
            np.savez(emb_path, embeddings=E0.reshape(1, D),
                     engine=np.array(RC.ENGINE_TAG), dim=np.array(RC.EMBED_DIM, dtype=np.int64))

        def _write_adaptive():
            st = A.AdaptiveStore(adp_path)
            st.add(at_dist(0.10, 1), now=1.0, max_size=10)
            st.save()

        orig_embed = RC.EMBED_PATH
        try:
            RC.EMBED_PATH = emb_path
            # (a) enroll only
            _write_enroll()
            if adp_path.exists():
                adp_path.unlink()
            rec = Recognizer(make_cfg())
            rec._adaptive = A.AdaptiveStore(adp_path)
            t.ok(rec.load() and rec._refs.shape[0] == 1 and rec._adaptive.count == 0,
                 "enroll only: _refs = 1 row, no adaptive")
            # (b) orphan adaptive, no enrollment -> load False, not merged, orphan cleared (F3)
            emb_path.unlink()
            _write_adaptive()
            rec = Recognizer(make_cfg())
            rec._adaptive = A.AdaptiveStore(adp_path)
            loaded = rec.load()
            t.ok((loaded is False) and rec._refs is None and (not adp_path.exists()),
                 "orphan adaptive w/o enrollment: load False, not merged, orphan file cleared (F3)")
            # (c) both present -> merged
            _write_enroll()
            _write_adaptive()
            rec = Recognizer(make_cfg())
            rec._adaptive = A.AdaptiveStore(adp_path)
            t.ok(rec.load() and rec._refs.shape[0] == 2 and rec._adaptive.count == 1,
                 "both present: _refs = enrollment + adaptive (2 rows)")
        finally:
            RC.EMBED_PATH = orig_embed

    # --- 12) service _maybe_adapt_gallery: is_screen from screen_flagged>0; paranoid denies (G5) ---
    print("\n[12] service _maybe_adapt_gallery integration")
    try:
        from face_service.service import FaceService, VerifyOutcome
    except Exception as e:   # pragma: no cover - pywin32 not present in this context
        print(f"  skip  service import unavailable ({e.__class__.__name__}); G5 skipped")
    else:
        class _AuditStub:
            def __init__(self):
                self.records = []

            def write(self, event, record):
                self.records.append((event, record))

        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)

            def _rec(cfg, name):
                r = Recognizer(cfg)
                r._enroll_refs = E0.reshape(1, D).copy()
                r._adaptive = A.AdaptiveStore(tdp / name)
                r._refresh_refs()
                return r

            def _svc(rec, cfg):
                s = FaceService.__new__(FaceService)   # bypass heavy __init__ (camera/pywin32/files)
                s.cfg = cfg
                s.recog = rec
                s._audit = _AuditStub()
                return s

            good = at_dist(0.10, 1)
            # a) any screen flag blocks (is_screen = screen_flagged > 0)
            cfg5 = make_cfg()
            rec = _rec(cfg5, "a.npz")
            svc = _svc(rec, cfg5)
            svc._maybe_adapt_gallery(VerifyOutcome(True, 0.10, True, {"screen_flagged": 2}, good))
            last = svc._audit.records[-1]
            t.ok(last[0] == "adapt" and last[1]["accept"] is False
                 and last[1]["reason"] == "screen" and rec._adaptive.count == 0,
                 "screen_flagged>0 -> is_screen True -> refused (screen)")
            # b) no screen flag -> accepted
            rec = _rec(cfg5, "b.npz")
            svc = _svc(rec, cfg5)
            svc._maybe_adapt_gallery(VerifyOutcome(True, 0.10, True, {"screen_flagged": 0}, good))
            last = svc._audit.records[-1]
            t.ok(last[1]["accept"] is True and last[1]["reason"] == "ok" and rec._adaptive.count == 1,
                 "screen_flagged==0 -> is_screen False -> accepted")
            # c) paranoid + passive (gesture_passed hardcoded False) -> refused
            cfgp = make_cfg(liveness_mode="paranoid")
            rec = _rec(cfgp, "c.npz")
            svc = _svc(rec, cfgp)
            svc._maybe_adapt_gallery(VerifyOutcome(True, 0.10, True, {"screen_flagged": 0}, good))
            last = svc._audit.records[-1]
            t.ok(last[1]["accept"] is False and last[1]["reason"] == "paranoid-no-gesture"
                 and rec._adaptive.count == 0,
                 "paranoid + passive path -> refused (no gesture)")

    # --- 13) F5: save() failure rolls the in-memory add back (memory == disk, cooldown frozen) (G9) ---
    print("\n[13] maybe_adapt: save() failure -> rollback (F5)")
    with tempfile.TemporaryDirectory() as td:
        rec = Recognizer(make_cfg())
        rec._enroll_refs = E0.reshape(1, D).copy()
        rec._adaptive = A.AdaptiveStore(Path(td) / "g9.npz")
        rec._refresh_refs()
        # seed one real accepted add so there is a genuine prior state (count=1, a real ts)
        ok0 = rec.maybe_adapt(at_dist(0.10, 1), liveness_passed=True, is_screen=False,
                              mode="fast", now=100.0).accept
        refs_before = rec._refs.copy()
        count_before = rec._adaptive.count
        ts_before = rec._adaptive.last_adapt_ts

        def _boom(*a, **k):
            raise OSError("simulated disk failure")

        rec._adaptive.save = _boom   # next add's persist will raise (cooldown already elapsed at now=1e9)
        dec = rec.maybe_adapt(at_dist(0.11, 2), liveness_passed=True, is_screen=False,
                              mode="fast", now=1e9)
        t.ok(ok0 and (not dec.accept) and dec.reason == "save-failed",
             "save() raises -> reason 'save-failed', not accepted")
        t.ok(rec._adaptive.count == count_before,
             f"count unchanged ({count_before}) -> no phantom add")
        t.ok(rec._adaptive.last_adapt_ts == ts_before,
             "last_adapt_ts unchanged -> cooldown did not advance")
        t.ok(np.array_equal(rec._refs, refs_before), "_refs unchanged -> memory == disk")

    print()
    if t.fail:
        print(f"ADAPTIVE SELFTEST FAILED: {t.fail} check(s) failed.")
        return 1
    print("ADAPTIVE SELFTEST OK: genuine drift adapts; spoof/impostor/other cannot poison the gallery.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
