from __future__ import annotations

from engines.base import BaseEngine
from engines.cache import volume_cluster_near_atm
from engines.config import engine_enabled, env_float
from engines.engine_logger import append_engine_log
from engines.market_state import MarketState


class CallSideEngine(BaseEngine):
    name = "call_side"

    def process_tick(self, market_state: MarketState):
        if not engine_enabled("call_side"):
            return self._out("NO_TRADE", 0.0, "engine_disabled", {"engine": self.name})
        r = market_state.scalping_pipeline_result
        if not r:
            return self._out("NO_TRADE", 0.0, "no_features", {})
        coi = float(r.get("call_oi") or 0.0)
        cvol = float(r.get("call_vol") or 0.0)
        pcr = float(market_state.oi_snap.get("pcr") or 0.0)
        ce_v, pe_v = market_state.cache_get(
            "ce_pe_vol_near_atm",
            lambda: volume_cluster_near_atm(market_state.chain, market_state.price),
        )
        vol_spike = ce_v >= pe_v * env_float("CALL_SIDE_VOL_RATIO", 1.25) and ce_v > 0
        oi_bias = coi >= env_float("CALL_SIDE_OI_MIN", 0.42)
        vol_bias = cvol >= env_float("CALL_SIDE_VOL_STRENGTH_MIN", 0.48)
        pcr_ok = pcr <= env_float("CALL_SIDE_PCR_MAX", 1.05) if pcr > 0 else True

        if oi_bias and vol_bias and vol_spike and pcr_ok:
            conf = min(
                90.0,
                52.0 + coi * 28.0 + cvol * 22.0 + (10.0 if vol_spike else 0.0),
            )
            out = self._out(
                "BUY_CE",
                conf,
                "ce_oi_vol_cluster_bullish",
                {"call_oi": coi, "call_vol": cvol, "pcr": pcr, "ce_chain_vol": ce_v, "pe_chain_vol": pe_v},
            )
        else:
            out = self._out(
                "NO_TRADE",
                38.0,
                "ce_edge_weak",
                {"call_oi": coi, "call_vol": cvol, "pcr": pcr, "vol_spike": vol_spike},
            )
        append_engine_log(self.name, {"symbol": market_state.symbol, **out})
        return out
