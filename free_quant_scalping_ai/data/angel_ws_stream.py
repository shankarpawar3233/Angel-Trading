from __future__ import annotations

"""
Optional Angel SmartAPI WebSocket streamer.

Maintains an in-memory cache of latest prices for subscribed symbols.
"""

from typing import Dict

from websocket import WebSocketApp  # type: ignore[import]

from utils.logger import get_logger


logger = get_logger(__name__)

latest_prices: Dict[str, float] = {}

_ws: WebSocketApp | None = None


def _on_open(ws):
  logger.info("[ANGEL] WebSocket connected")
  # TODO: Subscribe to NIFTY/SENSEX tokens as per SmartAPI docs
  # ws.send(...) with appropriate subscription payload


def _on_message(ws, message):
  # Parse tick and update latest_prices; structure depends on Angel WS format
  try:
    # SmartAPI sends JSON; adapt when you know the exact schema
    import json
    data = json.loads(message)
    # Example placeholder logic:
    symbol = data.get("symbol")
    ltp = data.get("ltp")
    if symbol and ltp is not None:
      latest_prices[symbol] = float(ltp)
  except Exception:
    return


def _on_error(ws, error):
  logger.warning("[ANGEL] WebSocket error: %s", error)


def _on_close(ws, close_status_code, close_msg):
  logger.warning("[ANGEL] WebSocket closed: %s %s", close_status_code, close_msg)


def start_angel_ws(url: str) -> None:
  """
  Start the Angel WebSocket client in background (call from a thread / task).
  """
  global _ws
  _ws = WebSocketApp(
    url,
    on_open=_on_open,
    on_message=_on_message,
    on_error=_on_error,
    on_close=_on_close,
  )
  _ws.run_forever()

