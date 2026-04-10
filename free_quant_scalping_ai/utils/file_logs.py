"""
Project-wide file logging: errors.log (ERROR+) once per process.
"""

from __future__ import annotations

import logging
from pathlib import Path

from config.settings import PROJECT_ROOT

_ERRORS_PATH = PROJECT_ROOT / "logs" / "errors.log"
_attached = False


def attach_error_file_handler() -> None:
    global _attached
    if _attached:
        return
    _attached = True
    try:
        _ERRORS_PATH.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(_ERRORS_PATH, encoding="utf-8")
        fh.setLevel(logging.ERROR)
        fh.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
        root = logging.getLogger()
        root.addHandler(fh)
    except OSError:
        pass
