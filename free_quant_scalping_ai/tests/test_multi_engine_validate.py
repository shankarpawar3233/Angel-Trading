"""
Simulated tick through multi-engine platform (no WebSocket, no DB).

Run: python tests/test_multi_engine_validate.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _fake_chain():
    return {
        "23950": {
            "CE": {"ltp": 140.0, "oi": 800_000, "volume": 3000, "change_oi": 2000},
            "PE": {"ltp": 80.0, "oi": 700_000, "volume": 2500, "change_oi": 1500},
        },
        "24000": {
            "CE": {"ltp": 120.0, "oi": 1_200_000, "volume": 8000, "change_oi": 15_000},
            "PE": {"ltp": 100.0, "oi": 1_000_000, "volume": 6000, "change_oi": 8000},
        },
        "24050": {
            "CE": {"ltp": 95.0, "oi": 900_000, "volume": 5000, "change_oi": 5000},
            "PE": {"ltp": 130.0, "oi": 1_100_000, "volume": 9000, "change_oi": 12_000},
        },
        "24100": {
            "CE": {"ltp": 70.0, "oi": 600_000, "volume": 2000, "change_oi": 1000},
            "PE": {"ltp": 160.0, "oi": 950_000, "volume": 7000, "change_oi": 9000},
        },
    }


def test_smoke():
    from engines.platform_runner import run_engine_tick

    st: dict = {"market": {"NIFTY": {"last_price": 24005.0}}, "_decision_state": {}, "_side_balance": {}}
    chain = _fake_chain()
    hist = [24000.0 + i * 1.5 for i in range(50)]
    oi_snap = {"pcr": 0.92, "ce_oi_total": 2.1e6, "pe_oi_total": 2.0e6}
    t0 = time.perf_counter()
    r = run_engine_tick(
        st,
        symbol="NIFTY",
        price=24075.0,
        chain_snapshot=chain,
        sym_hist=hist,
        oi_snap=oi_snap,
    )
    dt_ms = (time.perf_counter() - t0) * 1000.0
    assert "engines" in r and "aggregate" in r and "scalping_pipeline" in r
    for name in ("scalping", "hold", "call_side", "put_side", "hero_zero"):
        assert name in r["engines"], f"missing engine {name}"
        o = r["engines"][name]
        assert "signal" in o and "confidence" in o and "reason" in o and "metadata" in o
    agg = r["aggregate"]
    assert agg["signal"] in ("BUY_CE", "BUY_PE", "NO_TRADE")
    assert "risk" in r
    print("OK smoke test: aggregate=", agg["signal"], "conf=", agg.get("confidence"), f"dt={dt_ms:.2f}ms")


if __name__ == "__main__":
    test_smoke()
    print("All multi-engine validation checks passed.")
