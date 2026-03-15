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

export async function fetchCandles(symbol = 'NIFTY', timeframe = '5m', limit = 200) {
  const res = await fetch(`${API_BASE}/candles?symbol=${encodeURIComponent(symbol)}&timeframe=${encodeURIComponent(timeframe)}&limit=${limit}`);
  if (!res.ok) return [];
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
