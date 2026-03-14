/**
 * Chart.js visualizations: price (candlestick-style + signals), confidence histogram, feature importance.
 */

const COLORS = {
  bg: '#1e293b',
  grid: 'rgba(148, 163, 184, 0.15)',
  text: '#e2e8f0',
  green: '#22c55e',
  red: '#ef4444',
  yellow: '#facc15',
  line: '#64748b',
};

const chartOptions = {
  responsive: true,
  maintainAspectRatio: false,
  plugins: {
    legend: { labels: { color: COLORS.text } },
  },
  scales: {
    x: {
      grid: { color: COLORS.grid },
      ticks: { color: COLORS.text, maxTicksLimit: 12 },
    },
    y: {
      grid: { color: COLORS.grid },
      ticks: { color: COLORS.text },
    },
  },
};

let priceChart = null;
let histChart = null;
let featureChart = null;

function getSignalColor(signal) {
  if (signal === 'BUY') return COLORS.green;
  if (signal === 'SELL') return COLORS.red;
  return COLORS.yellow;
}

function renderPriceChart(data) {
  const canvas = document.getElementById('priceChart');
  if (!canvas || !data || !data.ohlc || data.ohlc.length === 0) return;

  const ohlc = data.ohlc;
  const labels = ohlc.map(d => d.timestamp);
  const closes = ohlc.map(d => d.close);
  const norm = (t) => String(t).replace(/:00$/, '').trim();
  const signalsByTs = (data.signals || []).reduce((acc, s) => {
    acc[norm(s.timestamp)] = s;
    return acc;
  }, {});

  const buyData = ohlc.map((d, i) => {
    const s = signalsByTs[norm(d.timestamp)];
    return s && s.signal === 'BUY' ? s.price : null;
  });
  const sellData = ohlc.map((d, i) => {
    const s = signalsByTs[norm(d.timestamp)];
    return s && s.signal === 'SELL' ? s.price : null;
  });

  if (priceChart) priceChart.destroy();
  priceChart = new Chart(canvas, {
    type: 'line',
    data: {
      labels,
      datasets: [
        {
          label: 'Close',
          data: closes,
          borderColor: COLORS.line,
          backgroundColor: 'rgba(100, 116, 139, 0.1)',
          fill: true,
          tension: 0.1,
          pointRadius: 0,
        },
        {
          label: 'BUY',
          data: buyData,
          pointRadius: 6,
          pointBackgroundColor: COLORS.green,
          pointBorderColor: COLORS.green,
          showLine: false,
        },
        {
          label: 'SELL',
          data: sellData,
          pointRadius: 6,
          pointBackgroundColor: COLORS.red,
          pointBorderColor: COLORS.red,
          showLine: false,
        },
      ],
    },
    options: {
      ...chartOptions,
      scales: {
        ...chartOptions.scales,
        y: { ...chartOptions.scales.y, position: 'right' },
      },
    },
  });
}

function buildHistogramBuckets(values, bins = 20) {
  const clean = (values || []).filter(v => v != null && !Number.isNaN(v));
  if (clean.length === 0) return { labels: [], counts: [] };
  const min = Math.min(...clean);
  const max = Math.max(...clean) || 1;
  const step = (max - min) / bins || 0.05;
  const counts = new Array(bins).fill(0);
  const labels = [];
  for (let i = 0; i < bins; i++) {
    labels.push((min + (i + 0.5) * step).toFixed(3));
  }
  clean.forEach(v => {
    let idx = Math.min(Math.floor((v - min) / step), bins - 1);
    if (idx < 0) idx = 0;
    counts[idx]++;
  });
  return { labels, counts };
}

function renderConfidenceHistogram(values) {
  const canvas = document.getElementById('confidenceHistChart');
  if (!canvas) return;
  const { labels, counts } = buildHistogramBuckets(values, 25);
  if (counts.every(c => c === 0)) return;

  if (histChart) histChart.destroy();
  histChart = new Chart(canvas, {
    type: 'bar',
    data: {
      labels,
      datasets: [{
        label: 'Count',
        data: counts,
        backgroundColor: 'rgba(59, 130, 246, 0.6)',
        borderColor: '#3b82f6',
        borderWidth: 1,
      }],
    },
    options: {
      ...chartOptions,
      plugins: {
        ...chartOptions.plugins,
        legend: { display: false },
      },
      scales: {
        ...chartOptions.scales,
        x: { ...chartOptions.scales.x, title: { display: true, text: 'Confidence', color: COLORS.text } },
      },
    },
  });
}

function renderFeatureImportance(data) {
  const canvas = document.getElementById('featureImportanceChart');
  if (!canvas || !data || !data.features || data.features.length === 0) return;

  if (featureChart) featureChart.destroy();
  featureChart = new Chart(canvas, {
    type: 'bar',
    data: {
      labels: data.features,
      datasets: [{
        label: 'Importance',
        data: data.importance || data.features.map(() => 0),
        backgroundColor: 'rgba(34, 197, 94, 0.5)',
        borderColor: COLORS.green,
        borderWidth: 1,
      }],
    },
    options: {
      ...chartOptions,
      indexAxis: 'y',
      plugins: {
        ...chartOptions.plugins,
        legend: { display: false },
      },
      scales: {
        x: { ...chartOptions.scales.x, title: { display: true, text: 'Importance', color: COLORS.text } },
        y: { ...chartOptions.scales.y, ticks: { ...chartOptions.scales.y.ticks, autoSkip: false } },
      },
    },
  });
}

window.renderPriceChart = renderPriceChart;
window.renderConfidenceHistogram = renderConfidenceHistogram;
window.renderFeatureImportance = renderFeatureImportance;
