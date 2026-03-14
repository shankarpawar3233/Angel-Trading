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
