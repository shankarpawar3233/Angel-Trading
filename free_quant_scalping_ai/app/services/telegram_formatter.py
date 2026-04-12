"""
Format execution ``execution_final_signal`` payloads for Telegram (HTML for reliability).
"""

from __future__ import annotations

import html
from typing import Any, Dict


def _h(x: Any) -> str:
    return html.escape(str(x if x is not None else "–"), quote=True)


def format_entry(signal: Dict[str, Any]) -> str:
    """🚀 TRADE ENTRY — HTML body."""
    sym = _h(signal.get("symbol"))
    sig = _h(signal.get("signal"))
    strike = _h(signal.get("strike"))
    ent = _h(signal.get("entry"))
    tgt = _h(signal.get("target"))
    sl = _h(signal.get("sl"))
    conf = _h(signal.get("confidence"))
    ts = _h(signal.get("time"))
    return (
        "🚀 <b>TRADE ENTRY</b>\n"
        f"Symbol: <code>{sym}</code>\n"
        f"Signal: <code>{sig}</code>\n"
        f"Strike: <code>{strike}</code>\n"
        f"Entry: <code>{ent}</code>\n"
        f"Target: <code>{tgt}</code>\n"
        f"SL: <code>{sl}</code>\n"
        f"Confidence: <code>{conf}</code>\n"
        f"Time: <code>{ts}</code>"
    )


def format_exit(signal: Dict[str, Any]) -> str:
    """❌ TRADE EXIT — HTML body."""
    sym = _h(signal.get("symbol"))
    sig = _h(signal.get("signal"))
    ent = _h(signal.get("entry"))
    ltp = _h(signal.get("ltp"))
    pnl = signal.get("pnl")
    try:
        pnl_s = _h(round(float(pnl), 4))
    except (TypeError, ValueError):
        pnl_s = _h(pnl)
    reason = _h(signal.get("reason"))
    ts = _h(signal.get("time"))
    return (
        "❌ <b>TRADE EXIT</b>\n"
        f"Symbol: <code>{sym}</code>\n"
        f"Signal: <code>{sig}</code>\n"
        f"Entry: <code>{ent}</code>\n"
        f"Exit (LTP): <code>{ltp}</code>\n"
        f"PnL: <code>{pnl_s}</code>\n"
        f"Reason: <code>{reason}</code>\n"
        f"Time: <code>{ts}</code>"
    )


def format_hold(signal: Dict[str, Any]) -> str:
    """📈 TRADE UPDATE (hold) — HTML body."""
    sym = _h(signal.get("symbol"))
    sig = _h(signal.get("signal"))
    ltp = _h(signal.get("ltp"))
    pnl = signal.get("pnl")
    try:
        pnl_s = _h(round(float(pnl), 4))
    except (TypeError, ValueError):
        pnl_s = _h(pnl)
    sl = _h(signal.get("sl"))
    trail = _h(signal.get("trailing_sl"))
    ts = _h(signal.get("time"))
    return (
        "📈 <b>TRADE UPDATE</b>\n"
        f"Symbol: <code>{sym}</code>\n"
        f"Signal: <code>{sig}</code>\n"
        f"LTP: <code>{ltp}</code>\n"
        f"PnL: <code>{pnl_s}</code>\n"
        f"SL: <code>{sl}</code>\n"
        f"Trailing SL: <code>{trail}</code>\n"
        f"Time: <code>{ts}</code>"
    )


def format_system(msg: str) -> str:
    return f"⚙️ <b>SYSTEM</b>: {_h(msg)}"
