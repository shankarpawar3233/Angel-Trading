"""
Test API outputs from free_quant_scalping_ai backend.

Run with: python tests/test_api_outputs.py
Or: .\.venv\Scripts\python.exe tests\test_api_outputs.py

Uses PORT from env or defaults to 8001 for local testing.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict

try:
    import requests
except ImportError:
    print("Install requests: pip install requests")
    sys.exit(1)

PORT = int(os.environ.get("PORT", "8001"))
BASE = f"http://127.0.0.1:{PORT}"


def get(path: str) -> tuple[int, Any]:
    try:
        r = requests.get(f"{BASE}{path}", timeout=10)
        return r.status_code, r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text
    except requests.exceptions.ConnectionError:
        return 0, None
    except Exception as e:
        return -1, str(e)


def test_endpoint(name: str, path: str, required_keys: list[str] | None = None) -> bool:
    code, data = get(path)
    ok = code == 200
    status = "OK" if ok else f"FAIL ({code})"
    print(f"  {path:<30} {status}")
    if not ok:
        print(f"    -> {data}")
        return False
    if isinstance(data, dict):
        for k in required_keys or []:
            if k not in data:
                print(f"    -> missing key: {k}")
                ok = False
    return ok


def main():
    print("=" * 60)
    print("Testing free_quant_scalping_ai API outputs")
    print(f"Base URL: {BASE}")
    print("=" * 60)

    # Quick connectivity
    code, _ = get("/market")
    if code != 200:
        print("\nServer not reachable. Start with: python main.py (or PORT=8001 python main.py)")
        sys.exit(1)

    results = []

    # Core endpoints
    print("\n--- Core ---")
    results.append(test_endpoint("market", "/market", ["market"]))
    results.append(test_endpoint("signals", "/signals", ["signals"]))

    # Quant analytics endpoints
    print("\n--- Quant analytics ---")
    results.append(test_endpoint("market-regime", "/market-regime", ["market_regime"]))
    results.append(test_endpoint("liquidity-map", "/liquidity-map", ["liquidity_map"]))
    results.append(test_endpoint("stop-hunts", "/stop-hunts", ["stop_hunts"]))
    results.append(test_endpoint("final-signal", "/final-signal", ["final_signal"]))
    results.append(test_endpoint("gamma-exposure", "/gamma-exposure"))
    results.append(test_endpoint("max-pain", "/max-pain"))
    results.append(test_endpoint("hero-zero", "/hero-zero"))
    results.append(test_endpoint("hero-zero-expiry", "/hero-zero-expiry", ["hero_zero_expiry"]))
    results.append(test_endpoint("oi", "/oi"))
    results.append(test_endpoint("expiry-bias", "/expiry-bias"))

    # Option chain & flow
    print("\n--- Option chain & flow ---")
    results.append(test_endpoint("option-chain", "/option-chain"))
    results.append(test_endpoint("scalping", "/scalping"))
    results.append(test_endpoint("institutional-flow", "/institutional-flow"))
    results.append(test_endpoint("gamma", "/gamma"))
    results.append(test_endpoint("expiry", "/expiry"))

    # Sample outputs (first symbol only)
    print("\n--- Sample outputs (structure check) ---")
    _, market = get("/market")
    if isinstance(market, dict) and market.get("market"):
        sym = next(iter(market["market"]), None)
        if sym:
            m = market["market"][sym]
            print(f"  /market[{sym}]: last_price={m.get('last_price')} source={m.get('price_source')}")

    _, final_signal = get("/final-signal")
    if isinstance(final_signal, dict) and final_signal.get("final_signal"):
        sym = next(iter(final_signal["final_signal"]), None)
        if sym:
            s = final_signal["final_signal"][sym]
            print(f"  /final-signal[{sym}]: price={s.get('price')} regime={s.get('regime')} trade={s.get('trade')} confidence={s.get('confidence')}")
            required = ["symbol", "price", "regime", "trade", "confidence"]
            for k in required:
                present = k in s
                print(f"    key '{k}': {'present' if present else 'MISSING'}")

    _, regime = get("/market-regime")
    if isinstance(regime, dict) and regime.get("market_regime"):
        sym = next(iter(regime["market_regime"]), None)
        if sym:
            r = regime["market_regime"][sym]
            print(f"  /market-regime[{sym}]: regime={r.get('regime')} confidence={r.get('confidence')}")

    _, liq = get("/liquidity-map")
    if isinstance(liq, dict) and liq.get("liquidity_map"):
        sym = next(iter(liq["liquidity_map"]), None)
        if sym:
            lm = liq["liquidity_map"][sym]
            heat = (lm or {}).get("heatmap", [])
            supp = (lm or {}).get("support_zones", [])
            res = (lm or {}).get("resistance_zones", [])
            print(f"  /liquidity-map[{sym}]: heatmap_len={len(heat)} support_zones={len(supp)} resistance_zones={len(res)}")

    _, stops = get("/stop-hunts")
    if isinstance(stops, dict) and stops.get("stop_hunts"):
        sym = next(iter(stops["stop_hunts"]), None)
        if sym:
            sh = stops["stop_hunts"][sym]
            print(f"  /stop-hunts[{sym}]: detected={sh.get('detected')} zone={sh.get('stop_hunt_zone')}")

    passed = sum(results)
    total = len(results)
    print("\n" + "=" * 60)
    print(f"Result: {passed}/{total} endpoints OK")
    print("=" * 60)

    if "--dump" in sys.argv or "-d" in sys.argv:
        print("\n--- Dump /final-signal ---")
        _, data = get("/final-signal")
        print(json.dumps(data, indent=2, default=str))

    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
