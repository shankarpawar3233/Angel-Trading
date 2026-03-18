import { useState, useEffect, useRef } from 'react'
import {
  Chart as ChartJS,
  CategoryScale,
  LinearScale,
  BarElement,
  PointElement,
  Title,
  Tooltip,
  Legend,
} from 'chart.js'
import { Bar } from 'react-chartjs-2'
import { fetchMarket, fetchSignals, fetchOptionChain, fetchOi, fetchMarketRegime, fetchLiquidityMap, fetchStopHunts, fetchFinalSignal, fetchSignalHistory } from './api'

ChartJS.register(CategoryScale, LinearScale, BarElement, PointElement, Title, Tooltip, Legend)

function LiveBadge() {
  return (
    <span className="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full text-xs font-medium bg-emerald-500/20 text-emerald-400 border border-emerald-500/40">
      <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse" />
      LIVE
    </span>
  )
}

function MarketStrip({ market }) {
  const entries = Object.entries(market || {})
  if (entries.length === 0) return <div className="text-slate-500 text-sm">No market data yet…</div>
  return (
    <div className="flex flex-wrap gap-6">
      {entries.map(([symbol, data]) => (
        <div key={symbol} className="flex items-baseline gap-2">
          <span className="text-slate-400 font-medium">{symbol}</span>
          <span className="font-mono text-xl font-bold text-white">
            {data?.last_price != null ? Number(data.last_price).toLocaleString('en-IN', { minimumFractionDigits: 2 }) : '–'}
          </span>
          {data?.price_source === 'angel_smartapi' && (
            <span className="text-xs text-emerald-500/90">Angel Live</span>
          )}
          {data?.price_source === 'nse_live' && (
            <span className="text-xs text-cyan-400/90">NSE Live</span>
          )}
          {data?.price_source === 'cached' && (
            <span className="text-xs text-amber-500/90" title="Price from last stored candle; live fetch unavailable">Cached</span>
          )}
        </div>
      ))}
    </div>
  )
}

function ScalpingCard({ symbol, scalping, onPlaceOrder }) {
  if (!scalping) return null
  const isCe = (scalping.trade || '').toUpperCase().includes('CE')
  return (
    <div className={`rounded-lg border p-4 ${isCe ? 'border-cyan-500/50 bg-cyan-500/5' : 'border-amber-500/50 bg-amber-500/5'}`}>
      <div className="flex items-center justify-between mb-2">
        <span className="font-mono font-semibold text-slate-200">{symbol}</span>
        <span className={`text-sm font-medium ${isCe ? 'text-cyan-400' : 'text-amber-400'}`}>
          {scalping.trade || '–'}
        </span>
      </div>
      <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-sm font-mono">
        <span className="text-slate-500">Strike</span>
        <span className="text-slate-200">{scalping.strike || '–'}</span>
        <span className="text-slate-500">Entry</span>
        <span className="text-white">{scalping.entry != null ? scalping.entry : '–'}</span>
        <span className="text-slate-500">Target</span>
        <span className="text-emerald-400">{scalping.target != null ? scalping.target : '–'}</span>
        <span className="text-slate-500">SL</span>
        <span className="text-red-400">{scalping.stoploss != null ? scalping.stoploss : '–'}</span>
      </div>
      <div className="mt-2 flex items-center justify-between">
        <span className="text-xs text-slate-400">
          Confidence: <span className="text-slate-200">{scalping.confidence != null ? Math.round(scalping.confidence) : 0}%</span>
        </span>
        {onPlaceOrder && (
          <button
            type="button"
            onClick={() => onPlaceOrder(symbol, scalping)}
            className="text-xs px-2 py-1 rounded bg-emerald-500/20 text-emerald-400 border border-emerald-500/40 hover:bg-emerald-500/30"
          >
            Place order
          </button>
        )}
      </div>
    </div>
  )
}

function HeroZeroList({ heroZero }) {
  if (!heroZero || heroZero.length === 0) return <div className="text-slate-500 text-sm">No hero-zero alerts</div>
  return (
    <ul className="space-y-2">
      {heroZero.map((h, i) => (
        <li key={i} className="font-mono text-sm text-slate-300">
          {typeof h.strike === 'number' ? `${h.strike} ${h.type || ''}` : h.strike} — Entry: {h.entry} | Target: {h.target} | Prob: {h.probability != null ? Math.round(h.probability) : '–'}%
        </li>
      ))}
    </ul>
  )
}

function GammaExpiry({ gamma, expiry }) {
  return (
    <div className="space-y-2 text-sm">
      {gamma?.gamma_walls?.length > 0 && (
        <div>
          <span className="text-slate-500">Gamma walls: </span>
          <span className="font-mono text-slate-300">{gamma.gamma_walls.map(w => w.strike).join(', ')}</span>
        </div>
      )}
      {gamma?.gamma_flip != null && (
        <div>
          <span className="text-slate-500">Gamma flip: </span>
          <span className="font-mono text-slate-300">{gamma.gamma_flip}</span>
        </div>
      )}
      {expiry && (
        <div>
          <span className="text-slate-500">Expiry: </span>
          <span className="font-mono text-slate-300">{expiry.direction} | Range: {expiry.expected_range?.join(' – ')}</span>
        </div>
      )}
      {(!gamma || (!gamma.gamma_walls?.length && gamma.gamma_flip == null)) && !expiry && (
        <div className="text-slate-500">No gamma/expiry data (option chain required)</div>
      )}
    </div>
  )
}

function OptionChainHeatmap({ optionChainData }) {
  const chain = optionChainData?.chain || optionChainData
  const analytics = optionChainData?.analytics || {}
  const heroZeroStrikes = optionChainData?.hero_zero_strikes || []
  const maxCallOi = analytics?.max_call_oi
  const maxPutOi = analytics?.max_put_oi

  if (!chain || typeof chain !== 'object') return <div className="text-slate-500 text-sm">No option chain data (Angel stream)</div>
  const strikes = Object.keys(chain).filter(k => /^\d+$/.test(k)).map(Number).sort((a, b) => a - b)
  if (strikes.length === 0) return <div className="text-slate-500 text-sm">No option chain data</div>

  const fmt = (v) => (v != null && v !== '') ? Number(v).toFixed(1) : '–'
  const fmtInt = (v) => (v != null && v !== '') ? Number(v).toLocaleString() : '–'

  return (
    <div className="space-y-2">
      {(analytics.pcr != null || maxCallOi != null || maxPutOi != null) && (
        <div className="flex flex-wrap gap-4 text-xs font-mono text-slate-400 mb-2">
          {analytics.pcr != null && <span>PCR: <span className="text-slate-200">{Number(analytics.pcr).toFixed(3)}</span></span>}
          {maxCallOi != null && <span>Max OI CE: <span className="text-cyan-400">{maxCallOi}</span></span>}
          {maxPutOi != null && <span>Max OI PE: <span className="text-amber-400">{maxPutOi}</span></span>}
        </div>
      )}
      <div className="overflow-auto max-h-64 border border-slate-700 rounded-lg">
        <table className="w-full text-xs font-mono border-collapse">
          <thead className="bg-slate-800/80 sticky top-0">
            <tr>
              <th className="text-left p-2 text-slate-400 font-semibold">Strike</th>
              <th className="text-right p-2 text-cyan-400">CE LTP</th>
              <th className="text-right p-2 text-cyan-400">CE Vol</th>
              <th className="text-right p-2 text-cyan-400">CE OI</th>
              <th className="text-right p-2 text-amber-400">PE LTP</th>
              <th className="text-right p-2 text-amber-400">PE Vol</th>
              <th className="text-right p-2 text-amber-400">PE OI</th>
            </tr>
          </thead>
          <tbody>
            {strikes.map((strike) => {
              const row = chain[String(strike)] || {}
              const ce = row.CE || {}
              const pe = row.PE || {}
              const isHero = heroZeroStrikes.includes(Number(strike))
              const isMaxCallOi = maxCallOi != null && strike === Number(maxCallOi)
              const isMaxPutOi = maxPutOi != null && strike === Number(maxPutOi)
              const highOI = isMaxCallOi || isMaxPutOi
              const rowClass = isHero
                ? 'bg-violet-500/15 border-l-2 border-violet-400'
                : highOI
                  ? 'bg-slate-700/40'
                  : ''
              return (
                <tr key={strike} className={`border-t border-slate-700/50 ${rowClass}`}>
                  <td className="p-2 text-slate-200 font-medium">
                    {strike}
                    {isHero && <span className="ml-1 text-violet-400" title="Hero-Zero">●</span>}
                  </td>
                  <td className="p-2 text-right text-cyan-300">{fmt(ce.ltp)}</td>
                  <td className="p-2 text-right text-slate-400">{fmtInt(ce.volume)}</td>
                  <td className="p-2 text-right text-slate-300">{fmtInt(ce.oi)}{isMaxCallOi && <span className="text-cyan-400 ml-0.5">↑</span>}</td>
                  <td className="p-2 text-right text-amber-300">{fmt(pe.ltp)}</td>
                  <td className="p-2 text-right text-slate-400">{fmtInt(pe.volume)}</td>
                  <td className="p-2 text-right text-slate-300">{fmtInt(pe.oi)}{isMaxPutOi && <span className="text-amber-400 ml-0.5">↑</span>}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function OiAnalysis({ oi }) {
  if (!oi || (oi.pcr == null && !oi.max_oi_call && !oi.max_oi_put)) return <div className="text-slate-500 text-sm">No OI analysis</div>
  return (
    <div className="space-y-1.5 text-sm font-mono">
      {oi.pcr != null && <div><span className="text-slate-500">PCR: </span><span className="text-slate-300">{Number(oi.pcr).toFixed(3)}</span></div>}
      {oi.max_oi_call != null && <div><span className="text-slate-500">Max OI CE: </span><span className="text-cyan-400">{oi.max_oi_call}</span></div>}
      {oi.max_oi_put != null && <div><span className="text-slate-500">Max OI PE: </span><span className="text-amber-400">{oi.max_oi_put}</span></div>}
      {oi.oi_spikes?.length > 0 && (
        <div>
          <span className="text-slate-500">OI spikes: </span>
          <span className="text-slate-300">{oi.oi_spikes.length} strike(s)</span>
        </div>
      )}
    </div>
  )
}

function GammaExposureChart({ gammaLevels }) {
  const levels = gammaLevels?.gamma_levels || []
  const wallCall = gammaLevels?.gamma_wall_call
  const wallPut = gammaLevels?.gamma_wall_put
  const flip = gammaLevels?.gamma_flip
  if (!levels.length) return <div className="text-slate-500 text-sm py-8">No gamma exposure data</div>
  const labels = levels.map((l) => String(l.strike))
  const data = levels.map((l) => l.gamma)
  const colors = data.map((g) => (g >= 0 ? 'rgba(34, 211, 238, 0.7)' : 'rgba(251, 191, 36, 0.7)'))
  const chartData = {
    labels,
    datasets: [{ label: 'Gamma', data, backgroundColor: colors, borderColor: colors.map((c) => c.replace('0.7', '1')), borderWidth: 1 }],
  }
  const options = {
    responsive: true,
    maintainAspectRatio: false,
    plugins: {
      legend: { display: false },
      tooltip: { callbacks: { label: (ctx) => `Strike ${ctx.label}: ${Number(ctx.raw).toLocaleString()}` } },
    },
    scales: {
      x: { ticks: { color: '#94a3b8', maxRotation: 45 } },
      y: { ticks: { color: '#94a3b8' } },
    },
  }
  return (
    <div className="space-y-2">
      <div className="h-48">
        <Bar data={chartData} options={options} />
      </div>
      <div className="flex flex-wrap gap-3 text-xs font-mono text-slate-400">
        {wallCall != null && <span>Resistance (call wall): <span className="text-cyan-400">{wallCall}</span></span>}
        {wallPut != null && <span>Support (put wall): <span className="text-amber-400">{wallPut}</span></span>}
        {flip != null && <span>Gamma flip: <span className="text-slate-200">{flip}</span></span>}
      </div>
    </div>
  )
}

function MaxPainIndicator({ maxPain, spot }) {
  if (maxPain == null) return <div className="text-slate-500 text-sm">No max pain data</div>
  const diff = spot != null ? spot - maxPain : null
  return (
    <div className="space-y-1">
      <div className="font-mono text-2xl font-bold text-white">{Number(maxPain).toLocaleString()}</div>
      {diff != null && (
        <div className="text-xs text-slate-400">
          Spot {diff >= 0 ? 'above' : 'below'} max pain by <span className="text-slate-200">{Math.abs(diff).toFixed(0)}</span> pts
        </div>
      )}
    </div>
  )
}

function ExpiryBiasIndicator({ bias, expectedRange }) {
  if (!bias && (!expectedRange || !expectedRange.length)) return <div className="text-slate-500 text-sm">No expiry bias</div>
  const color = bias === 'BULLISH' ? 'text-emerald-400' : bias === 'BEARISH' ? 'text-red-400' : 'text-slate-300'
  return (
    <div className="space-y-1">
      <div className={`font-mono font-semibold ${color}`}>{bias || '–'}</div>
      {expectedRange?.length >= 2 && (
        <div className="text-xs text-slate-400">
          Range: <span className="text-slate-200">{Number(expectedRange[0]).toLocaleString()} – {Number(expectedRange[1]).toLocaleString()}</span>
        </div>
      )}
    </div>
  )
}

function MarketRegimeIndicator({ regime, confidence }) {
  if (!regime) return <div className="text-slate-500 text-sm">No regime data</div>
  const colors = { TREND_UP: 'text-emerald-400', TREND_DOWN: 'text-red-400', RANGE: 'text-slate-300', VOLATILE: 'text-amber-400' }
  const color = colors[regime] || 'text-slate-300'
  return (
    <div className="space-y-1">
      <div className={`font-mono font-semibold ${color}`}>{regime}</div>
      {confidence != null && <div className="text-xs text-slate-400">Confidence: <span className="text-slate-200">{confidence}%</span></div>}
    </div>
  )
}

function LiquidityMapPanel({ liquidityMap }) {
  const heatmap = liquidityMap?.heatmap || []
  const support = liquidityMap?.support_zones || []
  const resistance = liquidityMap?.resistance_zones || []
  const stops = liquidityMap?.stop_loss_clusters || []
  if (!heatmap.length && !support.length && !resistance.length) return <div className="text-slate-500 text-sm">No liquidity map data</div>
  return (
    <div className="space-y-3 text-sm font-mono">
      {support.length > 0 && (
        <div>
          <span className="text-slate-500">Support: </span>
          <span className="text-emerald-400">{support.slice(0, 5).map((z) => z.strike).join(', ')}</span>
        </div>
      )}
      {resistance.length > 0 && (
        <div>
          <span className="text-slate-500">Resistance: </span>
          <span className="text-amber-400">{resistance.slice(0, 5).map((z) => z.strike).join(', ')}</span>
        </div>
      )}
      {stops.length > 0 && (
        <div>
          <span className="text-slate-500">Stop clusters: </span>
          <span className="text-slate-300">{stops.slice(0, 5).map((s) => s.strike).join(', ')}</span>
        </div>
      )}
      {heatmap.length > 0 && (
        <div className="text-xs text-slate-500">Top liquidity: {heatmap.slice(0, 5).map((h) => `${h.strike}(${Number(h.score).toFixed(2)})`).join(', ')}</div>
      )}
    </div>
  )
}

function StopHuntPanel({ stopHunt }) {
  if (!stopHunt?.detected) return <div className="text-slate-500 text-sm">No stop-hunt detected</div>
  const z = stopHunt.stop_hunt_zone
  if (!z) return null
  const color = z.type === 'RESISTANCE' ? 'text-amber-400' : 'text-emerald-400'
  return (
    <div className="space-y-1 font-mono text-sm">
      <div className={color}>{z.type} @ {z.level}</div>
      <div className="text-xs text-slate-400">Reversal price: {z.reversal_price}</div>
    </div>
  )
}

function FinalSignalCard({ finalSignal }) {
  if (!finalSignal || !Object.keys(finalSignal).length) return <div className="text-slate-500 text-sm">No final signal</div>
  const entries = Object.entries(finalSignal)
  return (
    <div className="space-y-2">
      {entries.map(([symbol, s]) => (
        <div key={symbol} className="rounded border border-slate-700 bg-slate-800/40 p-3 font-mono text-sm">
          <div className="flex items-center justify-between mb-2">
            <span className="text-slate-400">{symbol}</span>
            <span className="text-white font-bold">{s?.price != null ? Number(s.price).toLocaleString() : '–'}</span>
          </div>
          <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs">
            <span className="text-slate-500">Trade</span><span className={s?.trade === 'BUY_CE' ? 'text-cyan-400' : s?.trade === 'BUY_PE' ? 'text-amber-400' : 'text-slate-400'}>{s?.trade ?? '–'}</span>
            <span className="text-slate-500">Strike</span><span className="text-slate-200">{s?.strike ?? '–'}</span>
            <span className="text-slate-500">Buy / Entry</span><span className="text-white font-medium">{s?.entry != null ? Number(s.entry).toFixed(2) : '–'}</span>
            <span className="text-slate-500">Target</span><span className="text-emerald-400">{s?.target != null ? Number(s.target).toFixed(2) : '–'}</span>
            <span className="text-slate-500">Stoploss</span><span className="text-red-400">{s?.stoploss != null ? Number(s.stoploss).toFixed(2) : '–'}</span>
            <span className="text-slate-500">Confidence</span><span className="text-slate-200">{s?.confidence != null ? `${s.confidence}%` : '–'}</span>
            <span className="text-slate-500">Regime</span><span className="text-slate-200">{s?.regime ?? '–'}</span>
            <span className="text-slate-500">Gamma wall</span><span className="text-slate-200">{s?.gamma_wall ?? '–'}</span>
            <span className="text-slate-500">Max pain</span><span className="text-slate-200">{s?.max_pain ?? '–'}</span>
            <span className="text-slate-500">Flow</span><span className="text-slate-200">{s?.institutional_flow ?? '–'}</span>
          </div>
        </div>
      ))}
    </div>
  )
}

function SignalHistoryPanel({ history, symbol }) {
  const list = history || []
  if (!list.length) return <div className="text-slate-500 text-sm">No signal history yet. Signals are stored each time the system runs (every ~60s).</div>
  const fmtTime = (ts) => {
    if (!ts) return '–'
    try {
      const d = new Date(ts)
      return isNaN(d.getTime()) ? ts : d.toLocaleString(undefined, { dateStyle: 'short', timeStyle: 'medium' })
    } catch (_) { return ts }
  }
  const tradeColor = (trade) => trade === 'BUY_CE' ? 'text-cyan-400' : trade === 'BUY_PE' ? 'text-amber-400' : 'text-slate-400'
  return (
    <div className="space-y-2">
      <div className="text-xs text-slate-500 font-mono mb-2">Symbol: {symbol} · Newest first</div>
      <div className="overflow-auto max-h-80 border border-slate-700 rounded-lg">
        <table className="w-full text-xs font-mono border-collapse">
          <thead className="bg-slate-800/80 sticky top-0">
            <tr>
              <th className="text-left p-2 text-slate-400 font-semibold">Time</th>
              <th className="text-left p-2 text-slate-400">Price</th>
              <th className="text-left p-2 text-slate-400">Trade</th>
              <th className="text-left p-2 text-slate-400">Entry</th>
              <th className="text-left p-2 text-slate-400">Target</th>
              <th className="text-left p-2 text-slate-400">SL</th>
              <th className="text-left p-2 text-slate-400">Conf.</th>
              <th className="text-left p-2 text-slate-400">Regime</th>
            </tr>
          </thead>
          <tbody>
            {list.map((row, i) => (
              <tr key={i} className="border-t border-slate-700/50 hover:bg-slate-800/40">
                <td className="p-2 text-slate-400">{fmtTime(row.ts)}</td>
                <td className="p-2 text-slate-200">{row.price != null ? Number(row.price).toLocaleString(undefined, { maximumFractionDigits: 2 }) : '–'}</td>
                <td className={`p-2 ${tradeColor(row.trade)}`}>{row.trade ?? '–'}</td>
                <td className="p-2 text-white">{row.entry != null ? Number(row.entry).toFixed(2) : '–'}</td>
                <td className="p-2 text-emerald-400">{row.target != null ? Number(row.target).toFixed(2) : '–'}</td>
                <td className="p-2 text-red-400">{row.stoploss != null ? Number(row.stoploss).toFixed(2) : '–'}</td>
                <td className="p-2 text-slate-200">{row.confidence != null ? `${row.confidence}%` : '–'}</td>
                <td className="p-2 text-slate-300">{row.regime ?? '–'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function NiftyCandleChart({ candles, timeframe, onTimeframeChange }) {
  return (
    <div className="text-slate-500 text-sm">Candlestick chart disabled in fast scalping mode.</div>
  )
}

function PlacedOrders({ orders, onExit }) {
  const list = orders || []
  if (!list.length) return <div className="text-slate-500 text-sm">No placed orders yet (paper).</div>
  return (
    <div className="space-y-2">
      {list.map((o) => (
        <div key={o.id} className="rounded border border-slate-700 bg-slate-950/40 p-3 flex items-center justify-between">
          <div className="font-mono text-sm">
            <div className="text-slate-200 font-semibold">{o.symbol} {o.trade} {o.strike}</div>
            <div className="text-slate-400 text-xs">Entry: {o.entry ?? '–'} | Target: {o.target ?? '–'} | SL: {o.stoploss ?? '–'}</div>
            <div className="text-slate-500 text-xs">Placed: {o.placedAt}</div>
          </div>
          <button
            type="button"
            onClick={() => onExit(o.id)}
            className="text-xs px-2 py-1 rounded bg-red-500/15 text-red-300 border border-red-500/30 hover:bg-red-500/25"
          >
            Exit
          </button>
        </div>
      ))}
    </div>
  )
}

export default function App() {
  const [market, setMarket] = useState({})
  const [signals, setSignals] = useState({})
  const [optionChain, setOptionChain] = useState({})
  const [oi, setOi] = useState({})
  const [orders, setOrders] = useState([])
  const [error, setError] = useState(null)
  const [lastUpdate, setLastUpdate] = useState(null)
  const [modelVersion, setModelVersion] = useState(null)
  const [marketRegime, setMarketRegime] = useState({})
  const [liquidityMap, setLiquidityMap] = useState({})
  const [stopHunts, setStopHunts] = useState({})
  const [finalSignal, setFinalSignal] = useState({})
  const [signalHistory, setSignalHistory] = useState({ symbol: 'NIFTY', history: [] })

  const placeOrder = (symbol, scalping) => {
    const o = {
      id: `${Date.now()}_${Math.random().toString(16).slice(2)}`,
      symbol,
      trade: scalping?.trade || '–',
      strike: scalping?.strike || '–',
      entry: scalping?.entry,
      target: scalping?.target,
      stoploss: scalping?.stoploss,
      placedAt: new Date().toLocaleTimeString(),
    }
    setOrders((prev) => [o, ...prev].slice(0, 25))
  }

  const exitOrder = (id) => {
    setOrders((prev) => prev.filter((o) => o.id !== id))
  }

  const load = async () => {
    try {
      const [marketRes, signalsRes, chainRes, oiRes, regimeRes, liqRes, stopRes, finalRes, historyRes] = await Promise.all([
        fetchMarket(),
        fetchSignals(),
        fetchOptionChain().catch(() => ({ NIFTY: {} })),
        fetchOi().catch(() => ({})),
        fetchMarketRegime().catch(() => ({ market_regime: {} })),
        fetchLiquidityMap().catch(() => ({ liquidity_map: {} })),
        fetchStopHunts().catch(() => ({ stop_hunts: {} })),
        fetchFinalSignal().catch(() => ({ final_signal: {} })),
        fetchSignalHistory('NIFTY', 50).catch(() => ({ symbol: 'NIFTY', history: [] })),
      ])
      setMarket(marketRes.market || {})
      setSignals(signalsRes.signals || {})
      setOptionChain(chainRes?.NIFTY ? { NIFTY: chainRes.NIFTY } : chainRes || {})
      setOi(oiRes || {})
      setMarketRegime(regimeRes.market_regime || {})
      setLiquidityMap(liqRes.liquidity_map || {})
      setStopHunts(stopRes.stop_hunts || {})
      setFinalSignal(finalRes.final_signal || {})
      setSignalHistory(historyRes?.history ? { symbol: historyRes.symbol || 'NIFTY', history: historyRes.history } : { symbol: 'NIFTY', history: [] })
      setModelVersion(signalsRes.model_version || marketRes.model_version || null)
      setLastUpdate(new Date())
      setError(null)
    } catch (e) {
      setError(e.message || 'Failed to fetch')
    }
  }

  useEffect(() => {
    load()
    const t = setInterval(load, 3000)
    return () => clearInterval(t)
  }, [])

  return (
    <div className="min-h-screen bg-slate-950">
      <header className="border-b border-slate-800 bg-slate-900/80 backdrop-blur sticky top-0 z-10">
        <div className="max-w-6xl mx-auto px-4 py-3 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <h1 className="text-lg font-bold text-white font-mono">Free Quant Scalping AI</h1>
            <LiveBadge />
          </div>
          <div className="flex items-center gap-6">
            <MarketStrip market={market} />
            <div className="flex flex-col items-end gap-0.5">
              {modelVersion && (
                <span className="text-xs text-slate-500 font-mono">
                  Model: {modelVersion}
                </span>
              )}
              {lastUpdate && (
                <span className="text-xs text-slate-500 font-mono">
                  Updated {lastUpdate.toLocaleTimeString()}
                </span>
              )}
            </div>
          </div>
        </div>
      </header>

      <main className="max-w-6xl mx-auto px-4 py-6">
        {error && (
          <div className="mb-4 px-4 py-2 rounded-lg bg-red-500/10 border border-red-500/30 text-red-400 text-sm">
            {error} — Is the backend running on port 8001?
          </div>
        )}

        <section className="mb-8">
          <h2 className="text-sm font-semibold text-slate-400 uppercase tracking-wider mb-3">Scalping signals</h2>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            {Object.entries(signals).map(([symbol, s]) => (
              <ScalpingCard key={symbol} symbol={symbol} scalping={s?.scalping} onPlaceOrder={placeOrder} />
            ))}
            {Object.keys(signals).length === 0 && !error && (
              <div className="text-slate-500 col-span-2">Waiting for signals…</div>
            )}
          </div>
        </section>

        <section className="mb-8">
          <h2 className="text-sm font-semibold text-slate-400 uppercase tracking-wider mb-3">Placed orders (paper)</h2>
          <div className="rounded-lg border border-slate-700 bg-slate-900/50 p-4">
            <PlacedOrders orders={orders} onExit={exitOrder} />
          </div>
        </section>

        <section className="mb-8">
          <h2 className="text-sm font-semibold text-slate-400 uppercase tracking-wider mb-3">Final signal (quant analytics)</h2>
          <div className="rounded-lg border border-slate-700 bg-slate-900/50 p-4">
            <FinalSignalCard finalSignal={finalSignal} />
          </div>
        </section>

        <section className="mb-8">
          <h2 className="text-sm font-semibold text-slate-400 uppercase tracking-wider mb-3">Signal history</h2>
          <p className="text-xs text-slate-500 mb-2">Past signals given by the system (stored every ~60s). Newest first.</p>
          <div className="rounded-lg border border-slate-700 bg-slate-900/50 p-4">
            <SignalHistoryPanel history={signalHistory.history} symbol={signalHistory.symbol} />
          </div>
        </section>

        <section className="mb-8">
          <h2 className="text-sm font-semibold text-slate-400 uppercase tracking-wider mb-3">Market regime indicator</h2>
          <div className="rounded-lg border border-slate-700 bg-slate-900/50 p-4">
            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
              {Object.entries(marketRegime).length > 0 ? (
                Object.entries(marketRegime).map(([symbol, r]) => (
                  <div key={symbol} className="font-mono">
                    <span className="text-slate-500 text-xs block mb-1">{symbol}</span>
                    <MarketRegimeIndicator regime={r?.regime} confidence={r?.confidence} />
                  </div>
                ))
              ) : (
                Object.entries(signals).map(([symbol, s]) => (
                  <div key={symbol} className="font-mono">
                    <span className="text-slate-500 text-xs block mb-1">{symbol}</span>
                    <MarketRegimeIndicator regime={s?.regime?.regime} confidence={s?.regime?.confidence} />
                  </div>
                ))
              )}
            </div>
          </div>
        </section>

        <section className="mb-8">
          <h2 className="text-sm font-semibold text-slate-400 uppercase tracking-wider mb-3">Option chain heatmap (Angel live)</h2>
          <div className="rounded-lg border border-slate-700 bg-slate-900/50 p-4">
            <OptionChainHeatmap optionChainData={optionChain?.NIFTY || optionChain} />
          </div>
        </section>

        <section className="grid grid-cols-1 md:grid-cols-2 gap-6 mb-8">
          <div className="rounded-lg border border-slate-700 bg-slate-900/50 p-4">
            <h2 className="text-sm font-semibold text-slate-400 uppercase tracking-wider mb-3">Liquidity map</h2>
            {Object.entries(liquidityMap).length > 0 ? (
              Object.entries(liquidityMap).map(([symbol, lm]) => (
                <div key={symbol} className="mb-3">
                  <span className="text-slate-500 text-xs block mb-1">{symbol}</span>
                  <LiquidityMapPanel liquidityMap={lm} />
                </div>
              ))
            ) : (
              Object.entries(signals).map(([symbol, s]) => (
                <div key={symbol} className="mb-3">
                  <span className="text-slate-500 text-xs block mb-1">{symbol}</span>
                  <LiquidityMapPanel liquidityMap={s?.liquidity_map} />
                </div>
              ))
            )}
          </div>
          <div className="rounded-lg border border-slate-700 bg-slate-900/50 p-4">
            <h2 className="text-sm font-semibold text-slate-400 uppercase tracking-wider mb-3">Stop-hunt detection</h2>
            {Object.entries(stopHunts).length > 0 ? (
              Object.entries(stopHunts).map(([symbol, sh]) => (
                <div key={symbol} className="mb-3">
                  <span className="text-slate-500 text-xs block mb-1">{symbol}</span>
                  <StopHuntPanel stopHunt={sh} />
                </div>
              ))
            ) : (
              Object.entries(signals).map(([symbol, s]) => (
                <div key={symbol} className="mb-3">
                  <span className="text-slate-500 text-xs block mb-1">{symbol}</span>
                  <StopHuntPanel stopHunt={s?.stop_hunt} />
                </div>
              ))
            )}
          </div>
        </section>

        <section className="grid grid-cols-1 lg:grid-cols-3 gap-6 mb-8">
          <div className="rounded-lg border border-slate-700 bg-slate-900/50 p-4">
            <h2 className="text-sm font-semibold text-slate-400 uppercase tracking-wider mb-3">Gamma exposure (GEX)</h2>
            {Object.entries(signals).map(([symbol, s]) => (
              <div key={symbol}>
                <span className="text-slate-500 text-xs block mb-2">{symbol}</span>
                <GammaExposureChart gammaLevels={s?.gamma_levels} />
              </div>
            ))}
          </div>
          <div className="rounded-lg border border-slate-700 bg-slate-900/50 p-4">
            <h2 className="text-sm font-semibold text-slate-400 uppercase tracking-wider mb-3">Max pain</h2>
            {Object.entries(signals).map(([symbol, s]) => (
              <div key={symbol} className="mb-3">
                <span className="text-slate-500 text-xs block mb-1">{symbol}</span>
                <MaxPainIndicator maxPain={s?.max_pain} spot={market[symbol]?.last_price} />
              </div>
            ))}
          </div>
          <div className="rounded-lg border border-slate-700 bg-slate-900/50 p-4">
            <h2 className="text-sm font-semibold text-slate-400 uppercase tracking-wider mb-3">Expiry bias</h2>
            {Object.entries(signals).map(([symbol, s]) => (
              <div key={symbol} className="mb-3">
                <span className="text-slate-500 text-xs block mb-1">{symbol}</span>
                <ExpiryBiasIndicator bias={s?.expiry_bias} expectedRange={s?.expiry_bias_range} />
              </div>
            ))}
          </div>
        </section>

        <section className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6">
          <div className="rounded-lg border border-slate-700 bg-slate-900/50 p-4">
            <h2 className="text-sm font-semibold text-slate-400 uppercase tracking-wider mb-3">Institutional flow</h2>
            {Object.entries(signals).map(([symbol, s]) => (
              <div key={symbol} className="font-mono text-sm text-slate-300 mb-1">
                {symbol}: {s?.institutional_flow?.description ?? '–'}
                {s?.institutional_flow?.put_call_ratio != null && (
                  <span className="text-slate-500 ml-1">PCR: {Number(s.institutional_flow.put_call_ratio).toFixed(2)}</span>
                )}
              </div>
            ))}
          </div>
          <div className="rounded-lg border border-slate-700 bg-slate-900/50 p-4">
            <h2 className="text-sm font-semibold text-slate-400 uppercase tracking-wider mb-3">Hero-Zero opportunities</h2>
            {Object.entries(signals).map(([symbol, s]) => (
              <div key={symbol}>
                <span className="text-slate-500 text-xs">{symbol}: </span>
                <HeroZeroList heroZero={s?.hero_zero} />
              </div>
            ))}
          </div>
          <div className="rounded-lg border border-slate-700 bg-slate-900/50 p-4">
            <h2 className="text-sm font-semibold text-slate-400 uppercase tracking-wider mb-3">OI analysis</h2>
            {Object.keys(oi).length > 0 ? (
              Object.entries(oi).map(([sym, data]) => (
                <div key={sym} className="mb-2">
                  <span className="text-slate-500 text-xs">{sym}: </span>
                  <OiAnalysis oi={data} />
                </div>
              ))
            ) : (
              Object.entries(signals).map(([symbol, s]) => (
                <div key={symbol}>
                  <span className="text-slate-500 text-xs">{symbol}: </span>
                  <OiAnalysis oi={s?.oi_analysis} />
                </div>
              ))
            )}
          </div>
          <div className="rounded-lg border border-slate-700 bg-slate-900/50 p-4">
            <h2 className="text-sm font-semibold text-slate-400 uppercase tracking-wider mb-3">Gamma, Expiry & Liquidity</h2>
            {Object.entries(signals).map(([symbol, s]) => (
              <div key={symbol} className="mb-3">
                <span className="text-slate-500 text-xs block mb-1">{symbol}:</span>
                <GammaExpiry gamma={s?.gamma} expiry={s?.expiry} />
                {s?.liquidity_sweep?.detected && (
                  <div className="mt-1 text-xs font-mono text-amber-400">
                    Sweep: {s.liquidity_sweep.direction} ({Math.round(s.liquidity_sweep.confidence)}%)
                  </div>
                )}
              </div>
            ))}
          </div>
        </section>

        <section className="mt-6 rounded-lg border border-slate-700 bg-slate-900/50 p-4">
          <h2 className="text-sm font-semibold text-slate-400 uppercase tracking-wider mb-3">ML / RL</h2>
          <div className="flex flex-wrap gap-6">
            {Object.entries(signals).map(([symbol, s]) => (
              <div key={symbol} className="font-mono text-sm">
                <span className="text-slate-500">{symbol}:</span>{' '}
                <span className="text-slate-300">ML {s?.ml?.label ?? '–'}</span>
                <span className="text-slate-600 mx-1">|</span>
                <span className="text-slate-300">RL {s?.rl?.action ?? '–'}</span>
              </div>
            ))}
          </div>
        </section>
      </main>
    </div>
  )
}
