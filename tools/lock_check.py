"""tools/lock_check.py -- is the installed environment COMPLETE? (9d, act 9b A-6 / V-60)

The GPU variant installs requirements-gpu.lock with ``pip install --require-hashes --no-deps``:
insightface names ``onnxruntime`` as a dependency and the GPU lock carries ``onnxruntime-gpu``
instead, so pip's own resolver cannot be used for it. ``--no-deps`` also means pip no longer
checks that the lock is complete -- this does:

  * every NON-extra ``Requires-Dist`` whose environment marker is true, of every installed
    distribution, is satisfied by an installed distribution (name and version specifier).
    The one substitution allowed: ``onnxruntime`` is covered by ``onnxruntime-gpu``;
  * with ``--lock FILE``: every ``name==version`` pinned in FILE is installed at exactly that
    version.

Run after either install (the CPU lock is installed normally, with dependencies):
    python -m tools.lock_check --lock requirements.lock
    python -m tools.lock_check --lock requirements-gpu.lock
Exit 0 = complete; 1 = something is missing or mismatched (listed).
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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--lock", type=Path, default=None)
    a = ap.parse_args(argv)
    problems = missing_requirements()
    if a.lock is not None:
        problems += lock_mismatches(a.lock)
    for p in problems:
        print("  MISSING  " + p)
    if problems:
        print(f"lock_check: {len(problems)} problem(s)")
        return 1
    n = len(installed())
    print(f"lock_check OK: {n} distributions, every dependency satisfied"
          + (f", {a.lock.name} installed exactly" if a.lock else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
