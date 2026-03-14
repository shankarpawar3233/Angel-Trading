/**
 * NIFTY AI Prediction Dashboard - API client and DOM updates.
 * Auto-refresh every 5 minutes.
 */

const API_BASE = '';
const REFRESH_INTERVAL_MS = 5 * 60 * 1000; // 5 minutes

async function fetchJson(path) {
  const res = await fetch(API_BASE + path);
  if (!res.ok) throw new Error(res.statusText);
  return res.json();
}

function updateLatestPrediction(data) {
  const spotEl = document.getElementById('spotPrice');
  const signalEl = document.getElementById('signalEl');
  const fillEl = document.getElementById('confidenceFill');
  const valueEl = document.getElementById('confidenceValue');
  const updateEl = document.getElementById('lastUpdate');

  if (!data || data.timestamp == null) {
    spotEl.textContent = '--';
    signalEl.textContent = 'HOLD';
    signalEl.className = 'signal hold';
    if (fillEl) fillEl.style.width = '0%';
    if (valueEl) valueEl.textContent = '0%';
    if (updateEl) updateEl.textContent = 'No data';
    return;
  }

  spotEl.textContent = data.spot_price != null ? Number(data.spot_price).toLocaleString('en-IN', { maximumFractionDigits: 2 }) : '--';
  const pred = (data.prediction || 'HOLD').toUpperCase();
  signalEl.textContent = pred;
  signalEl.className = 'signal ' + (pred === 'BUY' ? 'buy' : pred === 'SELL' ? 'sell' : 'hold');

  const conf = Number(data.confidence) || 0;
  const pct = Math.round(conf * 100);
  if (fillEl) {
    fillEl.style.width = pct + '%';
    fillEl.className = 'confidence-fill ' + (pred === 'BUY' ? 'buy' : pred === 'SELL' ? 'sell' : 'hold');
  }
  if (valueEl) valueEl.textContent = pct + '%';
  if (updateEl) updateEl.textContent = 'Updated: ' + (data.timestamp || '');
}

function updateOptionsSuggestion(data) {
  const atmEl = document.getElementById('suggestedOptionAtm');
  const itmEl = document.getElementById('suggestedOptionItm');
  const otmEl = document.getElementById('suggestedOptionOtm');
  const confEl = document.getElementById('optionConfidence');
  if (!data) {
    if (atmEl) atmEl.textContent = 'ATM: --';
    if (itmEl) itmEl.textContent = 'ITM: --';
    if (otmEl) otmEl.textContent = 'OTM: --';
    if (confEl) confEl.textContent = 'Confidence: --';
    return;
  }
  const pred = (data.prediction || 'HOLD').toUpperCase();
  const atm = data.suggested_option_atm || null;
  const itm = data.suggested_option_itm || null;
  const otm = data.suggested_option_otm || null;

  if (atmEl) atmEl.textContent = 'ATM: ' + (atm || '--');
  if (itmEl) itmEl.textContent = 'ITM: ' + (itm || '--');
  if (otmEl) otmEl.textContent = 'OTM: ' + (otm || '--');

  if (confEl) {
    const pct = Math.round((data.confidence || 0) * 100);
    confEl.textContent = 'Confidence: ' + pct + '%';
  }
}

function updateSignalHistory(list) {
  const tbody = document.getElementById('signalTableBody');
  if (!tbody) return;
  if (!list || list.length === 0) {
    tbody.innerHTML = '<tr><td colspan="5" class="loading">No history</td></tr>';
    return;
  }
  tbody.innerHTML = list.slice(0, 100).map(row => {
    const pred = (row.prediction || 'HOLD').toUpperCase();
    const badgeClass = pred === 'BUY' ? 'buy' : pred === 'SELL' ? 'sell' : 'hold';
    const confPct = row.confidence != null ? Math.round(row.confidence * 100) : '--';
    return `<tr>
      <td>${row.timestamp || '--'}</td>
      <td>${row.spot_price != null ? Number(row.spot_price).toFixed(2) : '--'}</td>
      <td><span class="signal-badge ${badgeClass}">${pred}</span></td>
      <td>${confPct}%</td>
      <td>${row.suggested_option || '--'}</td>
    </tr>`;
  }).join('');
}

function updateModelStats(data) {
  const accEl = document.getElementById('modelAccuracy');
  const precEl = document.getElementById('modelPrecision');
  if (accEl && data.accuracy != null) accEl.textContent = (data.accuracy * 100).toFixed(2) + '%';
  else if (accEl) accEl.textContent = '--';
  if (precEl && data.precision != null) precEl.textContent = (data.precision * 100).toFixed(2) + '%';
  else if (precEl) precEl.textContent = '--';
  if (typeof window.renderConfidenceHistogram === 'function' && data.confidence_values && data.confidence_values.length)
    window.renderConfidenceHistogram(data.confidence_values);
}

async function refreshAll() {
  try {
    const [latest, history, modelStats, chartData] = await Promise.all([
      fetchJson('/api/latest_prediction'),
      fetchJson('/api/prediction_history'),
      fetchJson('/api/model_stats'),
      fetchJson('/api/chart_data?limit=500'),
    ]);
    updateLatestPrediction(latest);
    updateOptionsSuggestion(latest);
    updateSignalHistory(history.predictions || []);
    updateModelStats(modelStats);
    if (typeof window.renderPriceChart === 'function') window.renderPriceChart(chartData);
    if (typeof window.renderFeatureImportance === 'function') {
      const fi = await fetchJson('/api/feature_importance');
      window.renderFeatureImportance(fi);
    }
  } catch (e) {
    console.error('Dashboard refresh failed:', e);
    const tbody = document.getElementById('signalTableBody');
    if (tbody) tbody.innerHTML = '<tr><td colspan="5" class="error">Failed to load data. Is the API running?</td></tr>';
  }
}

function startAutoRefresh() {
  refreshAll();
  setInterval(refreshAll, REFRESH_INTERVAL_MS);
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', startAutoRefresh);
} else {
  startAutoRefresh();
}
