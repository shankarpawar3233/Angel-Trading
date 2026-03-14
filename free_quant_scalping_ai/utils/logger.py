import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from config.settings import LOG_DIR


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)

    log_file = Path(LOG_DIR) / "app.log"
    handler = RotatingFileHandler(log_file, maxBytes=5_000_000, backupCount=3)
    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    handler.setFormatter(formatter)

    console = logging.StreamHandler()
    console.setFormatter(formatter)

    logger.addHandler(handler)
    logger.addHandler(console)

    return logger

