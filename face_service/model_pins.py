"""The one place the recognition and detection models are pinned (Stage 9, act 9b R7).

Decision 9-02: InsightFace buffalo_l is NOT redistributed. The installer downloads the official
archive -- the same URL the insightface library itself uses -- after the user accepts the
InsightFace terms, checks it against the pins below and unpacks exactly the five files into
``{app}\\models\\buffalo_l``. The service refuses face functions ("no-models") unless that
directory holds exactly those five files with exactly these hashes. installer/build.py, the
installer generator and the service all read the pins from HERE; nothing else may carry them.

YuNet (the presence-mode detector) is small, MIT-licensed and ships in the repo; it is pinned too.
No heavy imports: safe for build tooling and any process.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

PACK_NAME = "buffalo_l"

# Source of the archive: insightface.utils.storage.BASE_REPO_URL + "/buffalo_l.zip" (insightface
# 0.7.x, pinned in requirements). Bytes and SHA-256 of the archive as published.
BUFFALO_ZIP_URL = "https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_l.zip"
BUFFALO_ZIP_BYTES = 288_621_354
BUFFALO_ZIP_SHA256 = "80ffe37d8a5940d59a7384c201a2a38d4741f2f3c51eef46ebb28218a7b0ca2f"

# The five files of the pack: name -> (bytes, sha256). Four are load-bearing (detection,
# recognition, 2d106 landmarks for the blink EAR, 3d68 for head pose); genderage.onnx is unused
# but FaceAnalysis builds a session for every *.onnx in the directory, so it must be present.
BUFFALO_FILES: dict[str, tuple[int, str]] = {
    "det_10g.onnx":   (16_923_827,
                       "5838f7fe053675b1c7a08b633df49e7af5495cee0493c7dcf6697200b85b5b91"),
    "w600k_r50.onnx": (174_383_860,
                       "4c06341c33c2ca1f86781dab0e829f88ad5b64be9fba56e56bc9ebdefc619e43"),
    "2d106det.onnx":  (5_030_888,
                       "f001b856447c413801ef5c42091ed0cd516fcd21f2d6b79635b1e733a7109dbf"),
    "1k3d68.onnx":    (143_607_619,
                       "df5c06b8a0c12e422b2ed8947b8869faa4105387f199c477af038aa01f9a45cc"),
    "genderage.onnx": (1_322_532,
                       "4fde69b1c810857b88c64a335084f1c3fe8f01246c9a191b48c7bb756d6652fb"),
}

# YuNet 2023mar from the OpenCV Zoo (MIT; models/LICENSE-yunet).
YUNET_FILE = "face_detection_yunet_2023mar.onnx"
YUNET_BYTES = 232_589
YUNET_SHA256 = "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def check_pack(directory: Path, *, hashes: bool = True) -> "list[str]":
    """Problems with a buffalo_l directory; [] when it is exactly the pinned pack.

    Exactly: the five files, nothing else (an extra *.onnx would be loaded by FaceAnalysis), each
    with its pinned size and -- unless ``hashes`` is False -- its pinned SHA-256."""
    problems: list[str] = []
    directory = Path(directory)
    if not directory.is_dir():
        return [f"model directory missing: {directory.name}"]
    present = {p.name for p in directory.iterdir()}
    for extra in sorted(present - set(BUFFALO_FILES)):
        problems.append(f"unexpected file in the model directory: {extra}")
    for name, (size, digest) in BUFFALO_FILES.items():
        p = directory / name
        if not p.is_file():
            problems.append(f"missing: {name}")
            continue
        if p.stat().st_size != size:
            problems.append(f"{name}: {p.stat().st_size} bytes, pinned {size}")
            continue
        if hashes and sha256_file(p) != digest:
            problems.append(f"{name}: SHA-256 does not match the pin")
    return problems


def check_yunet(path: Path) -> "list[str]":
    if not Path(path).is_file():
        return [f"missing: {YUNET_FILE}"]
    if Path(path).stat().st_size != YUNET_BYTES or sha256_file(Path(path)) != YUNET_SHA256:
        return [f"{YUNET_FILE}: does not match the pin"]
    return []
