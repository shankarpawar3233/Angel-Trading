import { useState, useEffect } from 'react'
import { fetchMarket, fetchSignals, fetchOptionChain, fetchOi } from './api'

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

function ScalpingCard({ symbol, scalping }) {
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
      <div className="mt-2 text-xs text-slate-400">
        Confidence: <span className="text-slate-200">{scalping.confidence != null ? Math.round(scalping.confidence) : 0}%</span>
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

function OptionChainHeatmap({ chain }) {
  if (!chain || typeof chain !== 'object') return <div className="text-slate-500 text-sm">No option chain data</div>
  const entries = Object.entries(chain).filter(([, v]) => v && (v.ltp != null || v.oi != null))
  if (entries.length === 0) return <div className="text-slate-500 text-sm">No option chain data</div>
  const maxOi = Math.max(...entries.map(([, v]) => Number(v.oi) || 0), 1)
  return (
    <div className="grid grid-cols-4 sm:grid-cols-6 gap-1 text-xs font-mono max-h-48 overflow-auto">
      {entries.slice(0, 48).map(([key, v]) => {
        const pct = maxOi ? ((Number(v.oi) || 0) / maxOi) * 100 : 0
        const isCe = key.endsWith('CE')
        return (
          <div
            key={key}
            className={`rounded p-1.5 ${isCe ? 'bg-cyan-500/20 text-cyan-300' : 'bg-amber-500/20 text-amber-300'}`}
            style={{ opacity: 0.5 + (pct / 100) * 0.5 }}
            title={`${key} LTP: ${v.ltp ?? '–'} OI: ${v.oi ?? '–'}`}
          >
            <div className="truncate">{key}</div>
            <div className="text-slate-400">{v.ltp != null ? Number(v.ltp).toFixed(1) : '–'}</div>
          </div>
        )
      })}
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

export default function App() {
  const [market, setMarket] = useState({})
  const [signals, setSignals] = useState({})
  const [optionChain, setOptionChain] = useState({})
  const [oi, setOi] = useState({})
  const [error, setError] = useState(null)
  const [lastUpdate, setLastUpdate] = useState(null)
  const [modelVersion, setModelVersion] = useState(null)

  const load = async () => {
    try {
      const [marketRes, signalsRes, chainRes, oiRes] = await Promise.all([
        fetchMarket(),
        fetchSignals(),
        fetchOptionChain().catch(() => ({ NIFTY: {} })),
        fetchOi().catch(() => ({})),
      ])
      setMarket(marketRes.market || {})
      setSignals(signalsRes.signals || {})
      setOptionChain(chainRes?.NIFTY ? { NIFTY: chainRes.NIFTY } : chainRes || {})
      setOi(oiRes || {})
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
              <ScalpingCard key={symbol} symbol={symbol} scalping={s?.scalping} />
            ))}
            {Object.keys(signals).length === 0 && !error && (
              <div className="text-slate-500 col-span-2">Waiting for signals…</div>
            )}
          </div>
        </section>

        <section className="mb-8">
          <h2 className="text-sm font-semibold text-slate-400 uppercase tracking-wider mb-3">Option chain heatmap</h2>
          <div className="rounded-lg border border-slate-700 bg-slate-900/50 p-4">
            <OptionChainHeatmap chain={optionChain?.NIFTY || optionChain} />
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
