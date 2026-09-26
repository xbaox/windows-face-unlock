"""Unicode-safe image file I/O for OpenCV (Stage 9, act 9b R8 / F-116).

cv2.imread and cv2.imwrite go through the ANSI code page on Windows: on a profile path with a
single non-ASCII character (Бао, 张三, even é under cp1252) imwrite returns False and imread
None. The wizard ignored the False, counted 15 frames "saved", and the build then found no
images -- enrollment was impossible with no hint why. Here the bytes go through Python's own
(Unicode) file API instead: np.fromfile + cv2.imdecode to read, cv2.imencode + tofile to write,
and a write is checked by reading the size back.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)


def imread(path, flags=None):
    """Decode an image file into a BGR array, or None when it cannot be read or decoded."""
    import cv2
    try:
        data = np.fromfile(str(path), dtype=np.uint8)
    except (OSError, ValueError):
        return None
    if data.size == 0:
        return None
    img = cv2.imdecode(data, cv2.IMREAD_COLOR if flags is None else flags)
    return img


def imwrite(path, image, params=None) -> bool:
    """Encode ``image`` by the file's extension and write it; True only when the file on disk
    holds exactly the encoded bytes. A failed write leaves no partial file behind."""
    import cv2
    path = Path(path)
    ext = path.suffix or ".png"
    try:
        ok, buf = cv2.imencode(ext, image, params or [])
    except cv2.error as e:
        log.warning("image encode failed for %s: %s", path.name, e)
        return False
    if not ok:
        return False
    try:
        buf.tofile(str(path))
        written = os.path.getsize(path)
    except OSError as e:
        log.warning("image write failed for %s: %s", path.name, e)
        try:
            path.unlink()
        except OSError:
            pass
        return False
    if written != buf.size:
        try:
            path.unlink()
        except OSError:
            pass
        return False
    return True
