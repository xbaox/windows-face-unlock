"""Adaptive gallery with anti-poisoning (Stage 3 / Step 2).

Lets the gallery track *gradual* drift of the enrolled person (beard, haircut,
glasses, ageing) by occasionally appending a just-verified embedding -- while
making it hard for a spoof or another person to poison the template.

Two pieces, both usable without a camera or the GPU engine so the policy can be
unit-tested (see ``tools.adaptive_selftest``):

* ``evaluate(...)`` -- a PURE anti-poisoning gate. Given the distance of a
  candidate frame TO THE ENROLLMENT BASELINE plus liveness/screen/mode context,
  it returns an ``AdaptDecision(accept, reason, ceiling)``. It performs no I/O.

* ``AdaptiveStore`` -- a persistent, engine-scoped FIFO ring of adaptive
  embeddings kept in a SEPARATE file from the enrollment baseline
  (``embeddings.npz``). Deleting that file rolls back all adaptation without
  touching enrollment.

Anti-poisoning rests on layered guards, all enforced by ``evaluate``:
  1. opt-in master toggle (off by default);
  2. liveness must have passed (kills static photos);
  3. ANY anti-screen flag blocks adaptation (kills replay-of-self, which matches
     on identity ~0.155 but is screen-like);
  4. paranoid mode additionally requires a passed gesture;
  5. the candidate must be MUCH closer than the unlock threshold -- distance to
     the *enrollment baseline* <= threshold - adaptive_margin. The caller is
     responsible for passing the baseline distance (NOT the distance to the
     adaptive-augmented gallery), so every stored embedding stays anchored within
     the ceiling of the ORIGINAL enrollment and drift cannot hop outward;
  6. a cooldown rate-limits additions;
  7. a size cap bounds how far the gallery can move (FIFO eviction).
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import NamedTuple

import numpy as np

log = logging.getLogger(__name__)

# The engine identity. recognizer.py imports these two (Stage 9, D-79: they used to be two copies
# kept "in sync" by a comment). The store is engine-scoped so a model change can't silently mix
# incompatible embeddings.
ENGINE_TAG = "insightface-buffalo_l"
EMBED_DIM = 512


def write_npz_atomic(path: Path, **arrays) -> None:
    """Write-then-rename with the data on disk before the rename (Stage 9, F-128): the temp file
    is flushed and fsync'ed, so a power loss cannot persist the rename ahead of the bytes; a failed
    write removes its temp file instead of leaving it behind."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "wb") as fh:
            np.savez(fh, **arrays)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def finite_rows(arr: np.ndarray) -> bool:
    """True when every value is finite (Stage 9, F-126: a NaN row made every min() NaN)."""
    return bool(np.isfinite(arr).all())


class AdaptDecision(NamedTuple):
    accept: bool
    reason: str      # stable token: ok / disabled / not-live / screen / paranoid-no-gesture /
                     #               distance>ceiling / cooldown
    ceiling: float   # effective distance ceiling used (threshold - adaptive_margin)


def _is_paranoid(mode) -> bool:
    return str(mode).lower() == "paranoid"


def evaluate(distance, threshold, cfg, *, liveness_passed, is_screen,
             mode, gesture_passed, now, last_adapt_ts, adaptive_count) -> AdaptDecision:
    """Decide whether a just-verified frame may join the adaptive gallery.

    ``distance`` MUST be the candidate's distance to the ENROLLMENT BASELINE, so
    the ceiling anchors every stored embedding to the original enrollment (no
    drift-hopping via previously-added adaptive embeddings). Pure: no I/O.

    Guards are ordered cheapest / most decisive first; each negative branch names
    a stable reason token for the audit log. ``adaptive_count`` gates whether the
    cooldown applies (there's nothing to cool down from on the very first add);
    eviction itself is handled by the store.
    """
    ceiling = float(threshold) - float(cfg.adaptive_margin)
    if not getattr(cfg, "adaptive_gallery", False):
        return AdaptDecision(False, "disabled", ceiling)
    if not liveness_passed:
        return AdaptDecision(False, "not-live", ceiling)
    if is_screen:                       # ANY screen suspicion -> never adapt (kills replay-of-self)
        return AdaptDecision(False, "screen", ceiling)
    if _is_paranoid(mode) and not gesture_passed:
        return AdaptDecision(False, "paranoid-no-gesture", ceiling)
    # Written as "not <=" (Stage 9, F-126): a NaN distance compares False both ways and used to
    # slip through "> ceiling"; now it is refused.
    if ceiling <= 0.0 or not (float(distance) <= ceiling):   # MUCH closer than the unlock threshold
        return AdaptDecision(False, "distance>ceiling", ceiling)
    # Cooldown applies only once something has been stored; gate on the count (not the
    # timestamp's truthiness -- a first stamp of 0.0 is falsy and would skip the check).
    # Stage 9 (F-127): a stored stamp in the FUTURE (the clock was set back after it was taken)
    # counts as "cooled down" instead of blocking adaptation until the clock catches up.
    elapsed = float(now) - float(last_adapt_ts)
    if adaptive_count > 0 and 0.0 <= elapsed < float(cfg.adaptive_cooldown_s):
        return AdaptDecision(False, "cooldown", ceiling)
    return AdaptDecision(True, "ok", ceiling)


class AdaptiveStore:
    """Persistent FIFO ring of adaptive embeddings, separate from enrollment.

    File ``adaptive.npz``: ``embeddings`` (M, 512) float32, ``ts`` (M,) float64
    epoch seconds, plus engine/dim tags. ``last_adapt_ts`` is the newest ts (0 if
    empty). All in-memory ops are pure numpy; ``save``/``load``/``clear`` touch the
    one file and nothing else, so a rollback is a single file delete.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.embeddings = np.zeros((0, EMBED_DIM), dtype=np.float32)
        self.ts = np.zeros((0,), dtype=np.float64)

    @property
    def count(self) -> int:
        return int(self.embeddings.shape[0])

    @property
    def last_adapt_ts(self) -> float:
        return float(self.ts.max()) if self.ts.size else 0.0

    def load(self) -> bool:
        """Load the ring from disk. Returns True on success; on any mismatch or
        corruption logs and starts empty (adaptation is best-effort, never fatal)."""
        if not self.path.exists():
            return False
        try:
            data = np.load(self.path, allow_pickle=False)
            files = set(data.files)
            if "engine" not in files or "dim" not in files or "embeddings" not in files:
                log.warning("adaptive store %s missing keys; ignoring", self.path.name)
                return False
            if str(data["engine"]) != ENGINE_TAG or int(data["dim"]) != EMBED_DIM:
                log.warning("adaptive store %s engine/dim mismatch; ignoring", self.path.name)
                return False
            emb = data["embeddings"].astype(np.float32)
            ts = (data["ts"].astype(np.float64) if "ts" in files
                  else np.zeros((emb.shape[0],), dtype=np.float64))
            if emb.ndim != 2 or emb.shape[1] != EMBED_DIM or ts.shape[0] != emb.shape[0]:
                log.warning("adaptive store %s bad shape %s; ignoring",
                            self.path.name, tuple(emb.shape))
                return False
            if not finite_rows(emb) or not finite_rows(ts):
                log.warning("adaptive store %s holds non-finite values; ignoring", self.path.name)
                return False
            self.embeddings, self.ts = emb, ts
            return True
        except Exception as e:  # pragma: no cover - defensive
            log.warning("adaptive store load failed (%s); starting empty", e)
            return False

    def add(self, embedding, now, max_size) -> None:
        """Append one embedding stamped ``now`` and FIFO-evict down to ``max_size``."""
        v = np.asarray(embedding, dtype=np.float32).reshape(1, EMBED_DIM)
        self.embeddings = np.vstack([self.embeddings, v])
        self.ts = np.concatenate([self.ts, np.array([float(now)], dtype=np.float64)])
        cap = max(1, int(max_size))
        if self.embeddings.shape[0] > cap:
            self.embeddings = self.embeddings[-cap:]
            self.ts = self.ts[-cap:]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Stage 8b (F-45) / Stage 9 (F-128): write-then-rename, fsync'ed, no temp left behind.
        write_npz_atomic(self.path, embeddings=self.embeddings, ts=self.ts,
                         engine=np.array(ENGINE_TAG), dim=np.array(EMBED_DIM, dtype=np.int64))

    def clear(self) -> "str | None":
        """Drop all adaptive embeddings and delete the file (enrollment untouched).

        Stage 9 (F-121): the in-memory ring is emptied FIRST and a file that cannot be deleted
        (AV scan, backup, ACL) no longer raises -- the caller used to lose the rest of its own
        cleanup to a PermissionError. Returns a problem string, or None."""
        self.embeddings = np.zeros((0, EMBED_DIM), dtype=np.float32)
        self.ts = np.zeros((0,), dtype=np.float64)
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            log.warning("adaptive store %s could not be deleted: %s", self.path.name, e)
            return f"{self.path.name}: {e}"
        return None
