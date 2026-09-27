"""tools/lock_check.py -- is the installed environment COMPLETE? (9d, act 9b A-6 / V-60)

The GPU variant installs requirements-gpu.lock with ``pip install --require-hashes --no-deps``:
insightface names ``onnxruntime`` as a dependency and the GPU lock carries ``onnxruntime-gpu``
instead, so pip's own resolver cannot be used for it. ``--no-deps`` also means pip no longer
checks that the lock is complete -- this does:

  * every NON-extra ``Requires-Dist`` whose environment marker is true, of every installed
    distribution, is satisfied by an installed distribution (name and version specifier).
    The one substitution allowed: ``onnxruntime`` is covered by ``onnxruntime-gpu``;
  * with ``--lock FILE``: every ``name==version`` pinned in FILE is installed at exactly that
    version, and (9d-r2, W-32) nothing ELSE is installed -- except pip and the build tools of
    installer/requirements-build.txt;
  * (9d-r2, W-32) ``onnxruntime`` and ``onnxruntime-gpu`` are never installed together: they
    share the ``onnxruntime`` import package, and whichever was installed last wins silently.

Run after either install (the CPU lock is installed normally, with dependencies), from any
directory (9d-r2, W-31: setup.ps1 runs it by its path):
    python tools/lock_check.py --lock requirements.lock
    python tools/lock_check.py --lock requirements-gpu.lock
Exit 0 = complete; 1 = something is missing or mismatched (listed).

9e-0 (X-02): every listed line says what it is --
    MISSING   a pinned or required distribution is not installed;
    EXTRA     an installed distribution the lock does not pin (pip and the build tools excepted);
    CONFLICT  installed, but at another version than pinned / required, or onnxruntime together
              with onnxruntime-gpu (or a requirement that cannot be read).
"""
from __future__ import annotations

import argparse
import re
import sys
from importlib import metadata
from pathlib import Path

from packaging.markers import default_environment
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

# requirement name -> installed distributions that satisfy it as well
COVERED_BY = {"onnxruntime": ("onnxruntime-gpu",)}
# 9d-r2 (W-32): installed next to a lock without being in it -- pip, and the build tools
ALWAYS_ALLOWED = ("pip",)
BUILD_REQUIREMENTS = Path(__file__).resolve().parents[1] / "installer" / "requirements-build.txt"


def installed() -> "dict[str, str]":
    out: dict = {}
    for d in metadata.distributions():
        name = d.metadata.get("Name")
        if name:
            out[canonicalize_name(name)] = d.version
    return out


def missing_requirements(dists=None, have: "dict[str, str] | None" = None) -> "list[str]":
    """Unsatisfied non-extra requirements of the installed distributions."""
    have = installed() if have is None else have
    env = default_environment()
    env["extra"] = ""
    problems = []
    for d in (metadata.distributions() if dists is None else dists):
        owner = d.metadata.get("Name") or "?"
        for raw in d.requires or []:
            try:
                req = Requirement(raw)
            except InvalidRequirement:
                problems.append(f"{owner}: unreadable requirement {raw!r}")
                continue
            if req.marker is not None and not req.marker.evaluate(env):
                continue                           # an extra, or another platform / Python
            name = canonicalize_name(req.name)
            candidates = [name] + [canonicalize_name(c) for c in COVERED_BY.get(name, ())]
            # the requirement itself or its substitute, under the same version specifier
            ok = any(have.get(c) is not None
                     and (not req.specifier or req.specifier.contains(have[c], prereleases=True))
                     for c in candidates)
            if not ok:
                got = have.get(name)
                problems.append(f"{owner} requires {req} -- "
                                + (f"installed {got}" if got else "not installed"))
    return sorted(set(problems))


def lock_pins(lock: Path) -> "dict[str, str]":
    pins = {}
    for line in lock.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s\\;]+)", line.strip())
        if m:
            pins[canonicalize_name(m.group(1))] = m.group(2)
    return pins


def lock_mismatches(lock: Path, have: "dict[str, str] | None" = None) -> "list[str]":
    have = installed() if have is None else have
    out = []
    for name, ver in lock_pins(lock).items():
        got = have.get(name)
        if got != ver:
            out.append(f"{name}=={ver} pinned in {lock.name} -- " + (f"installed {got}" if got
                                                                       else "not installed"))
    return out


def both_runtimes(have: "dict[str, str] | None" = None) -> "list[str]":
    """9d-r2 (W-32): onnxruntime and onnxruntime-gpu installed together."""
    have = installed() if have is None else have
    if have.get("onnxruntime") is not None and have.get("onnxruntime-gpu") is not None:
        return [f"onnxruntime {have['onnxruntime']} and onnxruntime-gpu {have['onnxruntime-gpu']} "
                "are both installed -- one variant per environment (act 9b A-6)"]
    return []


def not_in_lock(lock: Path, have: "dict[str, str] | None" = None,
                allowed_files=(BUILD_REQUIREMENTS,)) -> "list[str]":
    """9d-r2 (W-32): installed distributions that the lock does not pin (pip and the packages of
    ``allowed_files`` excepted)."""
    have = installed() if have is None else have
    allowed = set(lock_pins(lock)) | {canonicalize_name(n) for n in ALWAYS_ALLOWED}
    for f in allowed_files:
        if Path(f).is_file():
            allowed |= set(lock_pins(Path(f)))
    return [f"{name} {ver} is installed but not pinned in {lock.name}"
            for name, ver in sorted(have.items()) if name not in allowed]


TAGS = ("MISSING", "EXTRA", "CONFLICT")


def _absent_or_conflict(text: str) -> str:
    """9e-0 (X-02): a requirement / pin line is MISSING when nothing is installed under the name,
    CONFLICT when something is, at another version (or the requirement cannot be read)."""
    return "MISSING" if text.endswith("-- not installed") else "CONFLICT"


def tagged_problems(lock: "Path | None" = None,
                    have: "dict[str, str] | None" = None) -> "list[tuple[str, str]]":
    """9e-0 (X-02): every problem with its honest tag (TAGS)."""
    reqs = missing_requirements() if have is None else missing_requirements(have=have)
    have = installed() if have is None else have
    out = [(_absent_or_conflict(p), p) for p in reqs]
    out += [("CONFLICT", p) for p in both_runtimes(have)]
    if lock is not None:
        out += [(_absent_or_conflict(p), p) for p in lock_mismatches(lock, have)]
        out += [("EXTRA", p) for p in not_in_lock(lock, have)]
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--lock", type=Path, default=None)
    a = ap.parse_args(argv)
    problems = tagged_problems(a.lock)
    for tag, p in problems:
        print(f"  {tag:<9} {p}")
    if problems:
        counts = ", ".join(f"{sum(1 for t, _ in problems if t == tag)} {tag}" for tag in TAGS
                           if any(t == tag for t, _ in problems))
        print(f"lock_check: {len(problems)} problem(s): {counts}")
        return 1
    n = len(installed())
    print(f"lock_check OK: {n} distributions, every dependency satisfied"
          + (f", {a.lock.name} installed exactly" if a.lock else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
