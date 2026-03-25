from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict

from config.settings import PROJECT_ROOT

_LOG_DIR = PROJECT_ROOT / "logs"


def append_engine_log(engine_name: str, row: Dict[str, Any]) -> None:
    path = _LOG_DIR / f"{engine_name}.jsonl"
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"ts": time.time(), **row}, default=str, ensure_ascii=True) + "\n"
        with path.open("a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass
