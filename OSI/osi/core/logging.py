from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone


def setup_logging() -> None:
    # Use fixed IST offset so logging works even when tzdata is unavailable.
    ist = timezone(timedelta(hours=5, minutes=30), name="IST")
    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S %Z",
    )
    formatter.converter = lambda ts: datetime.fromtimestamp(ts, tz=ist).timetuple()  # type: ignore[assignment]

    logging.basicConfig(
        level=logging.INFO,
        handlers=[logging.StreamHandler()],
        force=True,
    )
    root = logging.getLogger()
    for handler in root.handlers:
        handler.setFormatter(formatter)

