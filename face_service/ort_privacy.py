"""onnxruntime telemetry off (Stage 9, act 9b §2.11 / R17, F-264).

Microsoft's official onnxruntime builds turn Windows TraceLogging telemetry ON by default (session
and execution-provider metadata; never images or face data). Face Unlock turns it off in every
process that loads onnxruntime -- the service and the setup wizard -- before the first session.
The README's privacy section says so.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)
_done = False


def disable_ort_telemetry() -> bool:
    """Idempotent. True when onnxruntime accepted the call (or it was already done)."""
    global _done
    if _done:
        return True
    try:
        import onnxruntime as ort
        ort.disable_telemetry_events()
    except Exception as e:           # no onnxruntime here, or an old build without the call
        log.debug("onnxruntime telemetry not disabled: %r", e)
        return False
    _done = True
    log.info("onnxruntime telemetry events disabled")
    return True
