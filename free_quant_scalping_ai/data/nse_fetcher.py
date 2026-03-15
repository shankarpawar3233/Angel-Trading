"""
DEPRECATED: NSE option chain scraping has been removed.

Option chain is now built entirely from Angel One SmartAPI WebSocket streaming.
See: data/angel_option_discovery.py, data/angel_option_stream.py, features/option_chain_builder.py
"""

from __future__ import annotations

import pandas as pd
from utils.logger import get_logger

logger = get_logger(__name__)


def fetch_and_store_option_chain(symbol: str) -> pd.DataFrame:
    """
    Deprecated. NSE scraping is disabled. Option chain is sourced from Angel WebSocket only.
    Returns empty DataFrame. No-op.
    """
    logger.debug("[NSE] fetch_and_store_option_chain is deprecated; use Angel option stream")
    return pd.DataFrame()
