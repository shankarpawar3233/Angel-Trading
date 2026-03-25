from __future__ import annotations

from engines.base import BaseEngine
from engines.cache import volume_cluster_near_atm
from engines.config import engine_enabled, env_float
from engines.engine_logger import append_engine_log
from engines.market_state import MarketState


class PutSideEngine(BaseEngine):
    name = "put_side"

    def process_tick(self, market_state: MarketState):
        if not engine_enabled("put_side"):
            return self._out("NO_TRADE", 0.0, "engine_disabled", {"engine": self.name})
        r = market_state.scalping_pipeline_result
        if not r:
            return self._out("NO_TRADE", 0.0, "no_features", {})
        poi = float(r.get("put_oi") or 0.0)
        pvol = float(r.get("put_vol") or 0.0)
        pcr = float(market_state.oi_snap.get("pcr") or 0.0)
        ce_v, pe_v = market_state.cache_get(
            "ce_pe_vol_near_atm",
            lambda: volume_cluster_near_atm(market_state.chain, market_state.price),
        )
        vol_spike = pe_v >= ce_v * env_float("PUT_SIDE_VOL_RATIO", 1.25) and pe_v > 0
        oi_bias = poi >= env_float("PUT_SIDE_OI_MIN", 0.42)
        vol_bias = pvol >= env_float("PUT_SIDE_VOL_STRENGTH_MIN", 0.48)
        pcr_ok = pcr >= env_float("PUT_SIDE_PCR_MIN", 0.95) if pcr > 0 else True

        if oi_bias and vol_bias and vol_spike and pcr_ok:
            conf = min(
                90.0,
                52.0 + poi * 28.0 + pvol * 22.0 + (10.0 if vol_spike else 0.0),
            )
            out = self._out(
                "BUY_PE",
                conf,
                "pe_oi_vol_cluster_bearish",
                {"put_oi": poi, "put_vol": pvol, "pcr": pcr, "ce_chain_vol": ce_v, "pe_chain_vol": pe_v},
            )
        else:
            out = self._out(
                "NO_TRADE",
                38.0,
                "pe_edge_weak",
                {"put_oi": poi, "put_vol": pvol, "pcr": pcr, "vol_spike": vol_spike},
            )
        append_engine_log(self.name, {"symbol": market_state.symbol, **out})
        return out
