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

/** Multi-engine platform: per-engine signal/confidence/reason/intent per symbol */
export async function fetchEngines() {
  const res = await fetch(`${API_BASE}/engines`);
  if (!res.ok) return { engine_platform: {} };
  return res.json();
}
