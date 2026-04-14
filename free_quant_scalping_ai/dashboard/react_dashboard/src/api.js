const API_BASE = import.meta.env.VITE_API_URL || 'http://localhost:8001';

export async function fetchMarket() {
  const res = await fetch(`${API_BASE}/market`);
  if (!res.ok) throw new Error('Market fetch failed');
  return res.json();
}

export async function fetchSignals() {
  const res = await fetch(`${API_BASE}/signals`);
  if (!res.ok) throw new Error('Signals fetch failed');
  return res.json();
}

export async function fetchOptionChain() {
  const res = await fetch(`${API_BASE}/option-chain`);
  if (!res.ok) return { NIFTY: {} };
  return res.json();
}

export async function fetchOi() {
  const res = await fetch(`${API_BASE}/oi`);
  if (!res.ok) return {};
  return res.json();
}

export async function fetchMarketRegime() {
  const res = await fetch(`${API_BASE}/market-regime`);
  if (!res.ok) return { market_regime: {} };
  return res.json();
}

export async function fetchLiquidityMap() {
  const res = await fetch(`${API_BASE}/liquidity-map`);
  if (!res.ok) return { liquidity_map: {} };
  return res.json();
}

export async function fetchStopHunts() {
  const res = await fetch(`${API_BASE}/stop-hunts`);
  if (!res.ok) return { stop_hunts: {} };
  return res.json();
}

export async function fetchGammaExposure() {
  const res = await fetch(`${API_BASE}/gamma-exposure`);
  if (!res.ok) return {};
  return res.json();
}

export async function fetchMaxPain() {
  const res = await fetch(`${API_BASE}/max-pain`);
  if (!res.ok) return {};
  return res.json();
}

export async function fetchFinalSignal() {
  const res = await fetch(`${API_BASE}/final-signal`);
  if (!res.ok) return { final_signal: {} };
  return res.json();
}

export async function fetchSignalHistory(symbol = 'NIFTY', limit = 50) {
  const res = await fetch(`${API_BASE}/signal-history?symbol=${encodeURIComponent(symbol)}&limit=${limit}`);
  if (!res.ok) return { symbol: symbol, history: [] };
  return res.json();
}

export async function fetchWsHealth() {
  const res = await fetch(`${API_BASE}/ws-health`);
  if (!res.ok) return { ws_health: {} };
  return res.json();
}

export async function fetchLatency() {
  const res = await fetch(`${API_BASE}/latency`);
  if (!res.ok) {
    return {
      latency: {},
      data_status: {},
      market_open: true,
      market_status: {},
      last_index_tick_age_sec: null,
      active_compute_symbols: [],
    };
  }
  return res.json();
}

export async function fetchPaperTrades() {
  const res = await fetch(`${API_BASE}/paper-trades`);
  if (!res.ok) return { paper_trades: { active: {}, stats: {}, last_trade: null, guide: {} } };
  return res.json();
}

export async function resetPaperTrades() {
  const res = await fetch(`${API_BASE}/paper-trades/reset`, { method: 'POST' });
  if (!res.ok) throw new Error('Paper trades reset failed');
  return res.json();
}

/** SENSEX live compute + option WS for this process (may require API restart for BFO chain). */
export async function setSensexSessionEnabled(enabled) {
  const res = await fetch(`${API_BASE}/session/sensex`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ enabled: Boolean(enabled) }),
  });
  if (!res.ok) throw new Error('SENSEX session update failed');
  return res.json();
}

/** scope: today_ist | all — clears SQLite final_fast + signal_events.jsonl (see POST /signals/reset). */
export async function resetSignalHistory(scope = 'today_ist') {
  const q = new URLSearchParams({ scope: String(scope) });
  const res = await fetch(`${API_BASE}/signals/reset?${q}`, { method: 'POST' });
  if (!res.ok) throw new Error('Signal history reset failed');
  return res.json();
}

/** Multi-engine platform: per-engine signal/confidence/reason/intent per symbol */
export async function fetchEngines() {
  const res = await fetch(`${API_BASE}/engines`);
  if (!res.ok) return { engine_platform: {} };
  return res.json();
}

/** Slow analysis engines (non-execution): scalping/trend/smc/breakout/oi */
export async function fetchEngineSignals() {
  const res = await fetch(`${API_BASE}/engine-signals`);
  if (!res.ok) return { engine_signals: {}, active_compute_symbols: [] };
  return res.json();
}

/** Strategy-only signals (trend/smc/breakout) with option levels. */
export async function fetchStrategySignals() {
  const res = await fetch(`${API_BASE}/strategy-signals`);
  if (!res.ok) return { strategy_signals: {}, active_compute_symbols: [] };
  return res.json();
}

/** Isolated NIFTY expiry-day experiment (does not drive execution). */
export async function fetchHeroZeroExpiry() {
  const res = await fetch(`${API_BASE}/hero-zero-expiry`);
  if (!res.ok) return { hero_zero_expiry: {} };
  return res.json();
}

export async function fetchSystemMode() {
  const res = await fetch(`${API_BASE}/system-mode`);
  if (!res.ok) return { system_mode: {}, market_status: {} };
  return res.json();
}
