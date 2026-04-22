from __future__ import annotations

import logging
import platform

logger = logging.getLogger(__name__)


class PowerGuard:
    """
    Prevent host sleep while OSI is running (Windows only).
    This keeps processing alive when the screen is locked.
    """

    def __init__(self) -> None:
        self._active = False

    def enable(self) -> None:
        if platform.system().lower() != "windows":
            logger.info("Power guard skipped (non-Windows host)")
            return
        try:
            import ctypes

            # Keep system awake, continuous mode.
            ES_CONTINUOUS = 0x80000000
            ES_SYSTEM_REQUIRED = 0x00000001
            ES_AWAYMODE_REQUIRED = 0x00000040
            flags = ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_AWAYMODE_REQUIRED
            ctypes.windll.kernel32.SetThreadExecutionState(flags)
            self._active = True
            logger.info("Power guard enabled (system sleep prevention active)")
        except Exception as exc:
            logger.warning("Failed to enable power guard: %s", exc)

    def disable(self) -> None:
        if platform.system().lower() != "windows" or not self._active:
            return
        try:
            import ctypes

            ES_CONTINUOUS = 0x80000000
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
            logger.info("Power guard disabled")
        except Exception as exc:
            logger.warning("Failed to disable power guard: %s", exc)
