"""Hard isolation for the selftests (Stage 9, act 9b R20; F-257, F-258, D-136).

Every selftest calls ONE of these before its first ``face_service`` / ``presence_monitor`` import
(tools/isolation_selftest.py checks that statically):

  * ``isolate(prefix)`` -- use the inherited ``FACE_UNLOCK_HOME`` if one is set (a runner's per-test
    home), otherwise create a fresh home under the temporary directory and remove it at exit;
  * ``own_root(prefix)`` -- always a fresh root of the test's own under the temporary directory, the
    home at ``<root>\\home``; the root is removed at exit.

Either REFUSES to run (exit 2, nothing touched) when the home is the real data directory
(``%USERPROFILE%\\.face-unlock``), contains it or lies inside it, or is not inside the temporary
directory; and when ``face_service.config`` was already imported with a different data directory.
A home the test did not create is never removed. Stdlib only: this module must be importable before
anything of the product is.
"""
from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

REFUSED_EXIT = 2


def real_data_dir() -> Path:
    """The per-user data directory the product uses (config.py's default)."""
    return Path.home() / ".face-unlock"


def _within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _norm(p: Path) -> Path:
    return Path(os.path.normcase(os.path.realpath(str(p))))


def check_home(home: "str | Path") -> "str | None":
    """Why ``home`` must not be used by a selftest, or None when it is safe."""
    h = _norm(Path(home))
    real = _norm(real_data_dir())
    if h == real or _within(h, real) or _within(real, h):
        return f"{home} is, contains or lies inside the real data directory {real_data_dir()}"
    tmp = _norm(Path(tempfile.gettempdir()))
    if h == tmp or not _within(h, tmp):
        return f"{home} is not inside the temporary directory {tempfile.gettempdir()}"
    return None


def _refuse(why: str) -> "None":
    print(f"REFUSED (selftest isolation): {why}. Nothing was touched.", file=sys.stderr)
    raise SystemExit(REFUSED_EXIT)


def _check_config_module(home: Path) -> None:
    cfg = sys.modules.get("face_service.config")
    if cfg is None:
        return
    if _norm(Path(cfg.APP_DIR)) != _norm(home):
        _refuse(f"face_service.config was imported before isolation (APP_DIR={cfg.APP_DIR})")


def _remove_at_exit(path: Path) -> None:
    atexit.register(shutil.rmtree, str(path), True)


def isolate(prefix: str) -> Path:
    """The inherited home when it is safe, else a fresh one of this process's own."""
    inherited = os.environ.get("FACE_UNLOCK_HOME")
    if inherited:
        why = check_home(inherited)
        if why:
            _refuse(f"FACE_UNLOCK_HOME: {why}")
        home = Path(inherited)
        home.mkdir(parents=True, exist_ok=True)
    else:
        home = Path(tempfile.mkdtemp(prefix=prefix))
        why = check_home(home)
        if why:
            _refuse(why)
        os.environ["FACE_UNLOCK_HOME"] = str(home)
        _remove_at_exit(home)
    _check_config_module(home)
    return home


def own_root(prefix: str) -> Path:
    """A fresh root of the test's own; the home is ``<root>/home`` (not created)."""
    root = Path(tempfile.mkdtemp(prefix=prefix))
    why = check_home(root / "home")
    if why:
        _refuse(why)
    os.environ["FACE_UNLOCK_HOME"] = str(root / "home")
    _remove_at_exit(root)
    _check_config_module(root / "home")
    return root
