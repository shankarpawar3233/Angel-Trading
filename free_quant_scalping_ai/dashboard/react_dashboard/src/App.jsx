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
import { fetchMarket, fetchSignals, fetchOi, fetchMarketRegime, fetchLiquidityMap, fetchStopHunts, fetchFinalSignal, fetchWsHealth, fetchPaperTrades, resetPaperTrades, fetchSignalHistory, fetchEngines } from './api'

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

function isDirectionalSignal(s) {
  const u = String(s || '').trim().toUpperCase()
  return u === 'BUY_CE' || u === 'BUY_PE'
}

/** Avoid `s?.scalping || s`: empty `{}` is truthy and yields blank trade/confidence/reason in the UI. */
function resolveScalpingPayload(s) {
  const fallback = {
    trade: 'NO_TRADE',
    signal: 'NO_TRADE',
    entry_decision: 'HOLD',
    decision_reason: 'waiting_live_data',
    confidence: 0,
    stable_count: 0,
    lock_remaining_sec: 0,
  }
  if (!s || typeof s !== 'object') return { ...fallback }
  const inner = s.scalping
  if (inner && typeof inner === 'object' && Object.keys(inner).length > 0) return inner
  if (s.trade != null || s.entry_decision != null || s.confidence != null) {
    return {
      trade: s.trade ?? s.signal ?? 'NO_TRADE',
      signal: s.signal ?? s.trade,
      entry_decision: s.entry_decision ?? 'HOLD',
      decision_reason: s.decision_reason ?? 'legacy_top_level_signal',
      confidence: s.confidence ?? 0,
      stable_count: s.stable_count ?? 0,
      lock_remaining_sec: s.lock_remaining_sec ?? 0,
      strike: s.strike,
      chain_ltp: s.chain_ltp,
      chain_ltp_age_sec: s.chain_ltp_age_sec,
      entry: s.entry,
      target: s.target,
      stoploss: s.stoploss,
      aggregate_leg_signal: s.aggregate_leg_signal,
      aggregate_leg_strike: s.aggregate_leg_strike,
      aggregate_leg_chain_ltp: s.aggregate_leg_chain_ltp,
      aggregate_leg_chain_ltp_age_sec: s.aggregate_leg_chain_ltp_age_sec,
      aggregate_leg_entry: s.aggregate_leg_entry,
      aggregate_leg_target: s.aggregate_leg_target,
      aggregate_leg_stoploss: s.aggregate_leg_stoploss,
    }
  }
  return {
    ...fallback,
    decision_reason: 'no_symbol_row_from_api_check_backend_port',
  }
}

/** Prefer live /signals engine_platform, then /engines snapshot, then /final-signal payload */
function getAggregateSignal(signalsEntry, finalEntry, enginePlatformEntry) {
  const sig =
    signalsEntry?.engine_platform?.aggregate?.signal ??
    enginePlatformEntry?.aggregate?.signal ??
    finalEntry?.platform_aggregate?.signal
  return sig != null && sig !== '' ? String(sig).trim() : ''
}

/** Multi-engine aggregate (confidence is not the same as fast scalping confidence). */
function getAggregateEnginesMeta(signalsEntry, enginePlatformEntry) {
  const agg =
    signalsEntry?.engine_platform?.aggregate ??
    enginePlatformEntry?.aggregate ??
    null
  if (!agg || typeof agg !== 'object') return null
  return {
    signal: agg.signal != null ? String(agg.signal).trim() : '',
    confidence: agg.confidence,
    reason: agg.reason != null ? String(agg.reason) : '',
    intent: agg.intent != null ? String(agg.intent) : '',
  }
}

function explainDecisionReason(reason) {
  const r = String(reason || '').trim()
  if (r === 'signal_not_directional') {
    return 'Fast scalping output is not BUY_CE/BUY_PE at this tick (stabilizer / entry gate). This is separate from the consensus leg below.'
  }
  return ''
}

/**
 * Dashboard primary trade label (aggregate-first — use outside the scalping card).
 * 1) Directional platform aggregate  2) Confirmed entry (BUY_CE/BUY_PE)  3) HOLD + raw BUY_* → CONTINUE_*
 * 4) NO_TRADE
 */
function resolvePrimaryTradeLabel({ aggregateSignal, entryDecision, rawTrade }) {
  const agg = String(aggregateSignal || '').trim().toUpperCase()
  if (isDirectionalSignal(agg)) return agg
  const ed = String(entryDecision || '').trim().toUpperCase()
  if (isDirectionalSignal(ed)) return ed
  const raw = String(rawTrade || '').trim().toUpperCase()
  if (ed === 'HOLD' && raw === 'BUY_CE') return 'CONTINUE_CE'
  if (ed === 'HOLD' && raw === 'BUY_PE') return 'CONTINUE_PE'
  return 'NO_TRADE'
}

/** Scalping card headline: fast pipeline only (never prefer platform aggregate over fast trade). */
function resolveScalpingFastBadge({ entryDecision, rawTrade }) {
  const ed = String(entryDecision || '').trim().toUpperCase()
  const raw = String(rawTrade || '').trim().toUpperCase()
  if (isDirectionalSignal(ed)) return ed
  if (ed === 'HOLD' && raw === 'BUY_CE') return 'CONTINUE_CE'
  if (ed === 'HOLD' && raw === 'BUY_PE') return 'CONTINUE_PE'
  if (isDirectionalSignal(raw)) return raw
  return 'NO_TRADE'
}

function fastBiasKey(entryDecision, rawTrade) {
  const raw = String(rawTrade || '').trim().toUpperCase()
  const ed = String(entryDecision || '').trim().toUpperCase()
  if (ed === 'BUY_CE' || (ed === 'HOLD' && raw === 'BUY_CE')) return 'BUY_CE'
  if (ed === 'BUY_PE' || (ed === 'HOLD' && raw === 'BUY_PE')) return 'BUY_PE'
  if (raw === 'BUY_CE') return 'BUY_CE'
  if (raw === 'BUY_PE') return 'BUY_PE'
  return ''
}

/** Decision row: never show bare HOLD when model still says BUY_CE/BUY_PE */
function formatDecisionRowLabel(entryDecision, rawTrade) {
  const ed = String(entryDecision || '').trim().toUpperCase()
  const raw = String(rawTrade || '').trim().toUpperCase()
  if (ed === 'HOLD' && raw === 'BUY_CE') return 'CONTINUE_CE'
  if (ed === 'HOLD' && raw === 'BUY_PE') return 'CONTINUE_PE'
  return String(entryDecision || 'HOLD')
}

function primaryTradeHeaderClass(label) {
  const u = String(label || '').toUpperCase()
  if (u === 'BUY_CE' || u === 'CONTINUE_CE') return 'text-cyan-400'
  if (u === 'BUY_PE' || u === 'CONTINUE_PE') return 'text-amber-400'
  return 'text-slate-400'
}

function primaryTradeCardBorderClass(label) {
  const u = String(label || '').toUpperCase()
  if (u === 'BUY_CE' || u === 'CONTINUE_CE') return 'border-cyan-500/50 bg-cyan-500/5'
  if (u === 'BUY_PE' || u === 'CONTINUE_PE') return 'border-amber-500/50 bg-amber-500/5'
  return 'border-slate-600/80 bg-slate-900/40'
}

function decisionRowClass(decisionLabel) {
  const u = String(decisionLabel || '').toUpperCase()
  if (u === 'HOLD') return 'text-amber-300'
  if (u === 'BUY_CE' || u === 'CONTINUE_CE') return 'text-emerald-400'
  if (u === 'BUY_PE' || u === 'CONTINUE_PE') return 'text-amber-400'
  return 'text-slate-200'
}

const ENGINE_DISPLAY_ORDER = ['scalping', 'hold', 'call_side', 'put_side', 'hero_zero']

const ENGINE_LABELS = {
  scalping: 'Scalping Engine',
  hold: 'Hold Engine',
  call_side: 'Call Side Engine',
  put_side: 'Put Side Engine',
  hero_zero: 'Hero Zero Engine',
  support_resistance: 'Support / Resistance Engine',
}

function engineDisplayLabel(key) {
  if (ENGINE_LABELS[key]) return ENGINE_LABELS[key]
  const words = String(key || '').split('_').filter(Boolean)
  if (words.length === 0) return key
  const titled = words.map((w) => w.charAt(0).toUpperCase() + w.slice(1).toLowerCase()).join(' ')
  return `${titled} Engine`
}

/** BUY_CE green, BUY_PE red, NO_TRADE (and other) gray */
function engineSignalClass(signal) {
  const s = String(signal || '').trim().toUpperCase()
  if (s === 'BUY_CE') return 'text-emerald-400'
  if (s === 'BUY_PE') return 'text-red-400'
  return 'text-slate-400'
}

function EngineSignalsSection({ enginePlatform, symbols }) {
  return (
    <section className="mb-8">
      <h2 className="text-sm font-semibold text-slate-400 uppercase tracking-wider mb-3">Engine signals</h2>
      <p className="text-xs text-slate-500 mb-3 font-mono">
        Per-engine outputs from <span className="text-slate-400">engine_platform[symbol].engines</span> (not only final aggregate).
      </p>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
        {symbols.map((symbol) => {
          const ep = enginePlatform?.[symbol] || {}
          const engines = ep.engines && typeof ep.engines === 'object' ? ep.engines : {}
          const extraKeys = Object.keys(engines)
            .filter((k) => !ENGINE_DISPLAY_ORDER.includes(k))
            .sort()
          const keysToShow = [...ENGINE_DISPLAY_ORDER, ...extraKeys]
          return (
            <div key={symbol} className="rounded-lg border border-slate-700 bg-slate-900/50 p-3">
              <div className="text-slate-500 text-xs font-mono mb-3">{symbol}</div>
              <ul className="space-y-0 divide-y divide-slate-700/50">
                {keysToShow.map((key) => {
                  const row = engines[key]
                  const o = row && typeof row === 'object' ? row : {}
                  const sig = o.signal != null ? o.signal : '–'
                  const conf = o.confidence
                  const reason = o.reason != null ? o.reason : '–'
                  const intent = o.intent
                  return (
                    <li key={key} className="py-3 first:pt-0">
                      <div className="text-[11px] uppercase tracking-wide text-slate-500 mb-1.5">{engineDisplayLabel(key)}</div>
                      <div className="font-mono text-sm flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
                        <span className="text-slate-500">Signal</span>
                        <span className={`font-semibold ${engineSignalClass(sig)}`}>{String(sig)}</span>
                        <span className="text-slate-600">|</span>
                        <span className="text-slate-500">Confidence</span>
                        <span className="text-slate-200">
                          {conf != null && conf !== '' ? `${Math.round(Number(conf))}%` : '–'}
                        </span>
                      </div>
                      <div className="text-xs font-mono mt-1.5">
                        <span className="text-slate-500">Reason </span>
                        <span className="text-slate-300 break-words" title={String(reason)}>{String(reason)}</span>
                      </div>
                      {intent != null && String(intent).trim() !== '' && (
                        <div className="text-xs font-mono mt-1">
                          <span className="text-slate-500">Intent </span>
                          <span className="text-slate-300">{String(intent)}</span>
                        </div>
                      )}
                    </li>
                  )
                })}
              </ul>
            </div>
          )
        })}
      </div>
    </section>
  )
}

function ScalpingCard({ symbol, scalping, heroZero, onPlaceOrder, aggregateSignal, aggregateEngines }) {
  if (!scalping) return null
  const rawTrade = scalping.trade || scalping.signal
  const entryDecision = scalping.entry_decision || 'HOLD'
  const primaryLabel = resolveScalpingFastBadge({ entryDecision, rawTrade })
  const aggU = String(aggregateSignal || '').trim().toUpperCase()
  const fastK = fastBiasKey(entryDecision, rawTrade)
  const aggregateDiffers =
    isDirectionalSignal(aggU) && fastK !== aggU
  const fastIsDirectional = isDirectionalSignal(String(rawTrade || '').trim().toUpperCase())
  const decisionLabel = formatDecisionRowLabel(entryDecision, rawTrade)
  const decisionReason = scalping.decision_reason || 'n/a'
  const reasonHint = explainDecisionReason(decisionReason)
  const stableCount = scalping.stable_count != null ? scalping.stable_count : 0
  const lockRemaining = scalping.lock_remaining_sec != null ? scalping.lock_remaining_sec : 0
  const aggLeg = scalping.aggregate_leg_strike
    ? {
        strike: scalping.aggregate_leg_strike,
        signal: scalping.aggregate_leg_signal,
        ltp: scalping.aggregate_leg_chain_ltp,
        ltpAgeSec: scalping.aggregate_leg_chain_ltp_age_sec,
        entry: scalping.aggregate_leg_entry,
        target: scalping.aggregate_leg_target,
        sl: scalping.aggregate_leg_stoploss,
      }
    : null
  const aggConf =
    aggregateEngines?.confidence != null && aggregateEngines.confidence !== ''
      ? Math.round(Number(aggregateEngines.confidence))
      : null
  return (
    <div className={`rounded-lg border p-3 ${primaryTradeCardBorderClass(primaryLabel)}`}>
      <div className="flex items-center justify-between mb-2">
        <span className="font-mono font-semibold text-slate-200">{symbol}</span>
        <span className={`text-sm font-medium ${primaryTradeHeaderClass(primaryLabel)}`}>
          {primaryLabel}
        </span>
      </div>
      {aggregateDiffers && (
        <div className="text-[10px] font-mono text-amber-400/90 mb-2 leading-snug space-y-1">
          <p>
            <span className="text-amber-300/80">Engines (aggregate):</span>{' '}
            <span className="font-semibold text-amber-300">{aggU}</span>
            {aggConf != null && (
              <span className="text-slate-500"> · {aggConf}%</span>
            )}
          </p>
          <p className="text-slate-400">
            {primaryLabel === 'NO_TRADE'
              ? 'Fast scalping is not directional (entry gate / stabilizer). The first grid stays empty until fast says BUY_CE or BUY_PE. Consensus below is the multi-engine side + hypothetical strike — reference only, not a fast “go” signal.'
              : 'Headline and first grid follow the fast scalping path; aggregate can differ when engines disagree with the stabilizer.'}
          </p>
        </div>
      )}
      <div className="text-[10px] uppercase tracking-wide text-slate-500 mb-1 font-mono">Fast scalping leg</div>
      <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-sm font-mono">
        <span className="text-slate-500">Strike</span>
        <span className="text-slate-200">{scalping.strike || '–'}</span>
        <span className="text-slate-500">Option LTP</span>
        <span className="text-slate-200">
          {scalping.chain_ltp != null && scalping.chain_ltp !== ''
            ? Number(scalping.chain_ltp).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
            : '–'}
        </span>
        <span className="text-slate-500">LTP age</span>
        <span className={(scalping.chain_ltp_age_sec != null && Number(scalping.chain_ltp_age_sec) > 2.0) ? 'text-amber-400' : 'text-slate-300'}>
          {scalping.chain_ltp_age_sec != null ? `${Number(scalping.chain_ltp_age_sec).toFixed(1)}s` : '–'}
        </span>
        <span className="text-slate-500">Entry</span>
        <span className="text-white">{scalping.entry != null ? scalping.entry : '–'}</span>
        <span className="text-slate-500">Target</span>
        <span className="text-emerald-400">{scalping.target != null ? scalping.target : '–'}</span>
        <span className="text-slate-500">SL</span>
        <span className="text-red-400">{scalping.stoploss != null ? scalping.stoploss : '–'}</span>
      </div>
      {aggLeg && (
        <div className="mt-2 pt-2 border-t border-slate-700/70">
          <div className="text-[10px] uppercase tracking-wide text-slate-500 mb-0.5 font-mono">
            Consensus leg ({String(aggLeg.signal || 'aggregate').trim()})
          </div>
          <p className="text-[10px] text-slate-500 mb-1 leading-snug">
            From platform aggregate + live chain. Use as context; fast scalping must still align for a timed entry.
          </p>
          <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-sm font-mono">
            <span className="text-slate-500">Strike</span>
            <span className="text-slate-200">{aggLeg.strike || '–'}</span>
            <span className="text-slate-500">Option LTP</span>
            <span className="text-slate-200">
              {aggLeg.ltp != null && aggLeg.ltp !== ''
                ? Number(aggLeg.ltp).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
                : '–'}
            </span>
            <span className="text-slate-500">LTP age</span>
            <span className={(aggLeg.ltpAgeSec != null && Number(aggLeg.ltpAgeSec) > 2.0) ? 'text-amber-400' : 'text-slate-300'}>
              {aggLeg.ltpAgeSec != null ? `${Number(aggLeg.ltpAgeSec).toFixed(1)}s` : '–'}
            </span>
            <span className="text-slate-500">Entry</span>
            <span className="text-white">{aggLeg.entry != null ? aggLeg.entry : '–'}</span>
            <span className="text-slate-500">Target</span>
            <span className="text-emerald-400">{aggLeg.target != null ? aggLeg.target : '–'}</span>
            <span className="text-slate-500">SL</span>
            <span className="text-red-400">{aggLeg.sl != null ? aggLeg.sl : '–'}</span>
          </div>
        </div>
      )}
      <div className="mt-2 text-xs font-mono grid grid-cols-2 gap-y-1">
        <span className="text-slate-500">Decision</span>
        <span className={decisionRowClass(decisionLabel)}>{decisionLabel}</span>
        <span className="text-slate-500">Stable count</span>
        <span className="text-slate-200">{stableCount}</span>
        <span className="text-slate-500">Reason</span>
        <span className="text-slate-300 truncate" title={reasonHint || decisionReason}>
          {decisionReason}
        </span>
        {lockRemaining > 0 && (
          <>
            <span className="text-slate-500">Lock</span>
            <span className="text-amber-400">{lockRemaining}s</span>
          </>
        )}
      </div>
      <div className="mt-2 space-y-1">
        <div className="text-xs text-slate-400">
          Fast scalping confidence:{' '}
          <span className="text-slate-200">{scalping.confidence != null ? Math.round(scalping.confidence) : 0}%</span>
          {!fastIsDirectional && (
            <span className="text-slate-500"> (model score; not an entry trigger while trade is NO_TRADE)</span>
          )}
        </div>
        {aggConf != null && aggregateDiffers && (
          <div className="text-xs text-slate-400">
            Aggregate (engines): <span className="text-slate-200">{aggConf}%</span>
            <span className="text-slate-500"> · separate from fast above</span>
          </div>
        )}
        {reasonHint && (
          <p className="text-[10px] text-slate-500 leading-snug">{reasonHint}</p>
        )}
      </div>
      <div className="mt-2 flex flex-wrap items-center justify-between gap-2">
        <span className="text-[10px] text-slate-500 max-w-[14rem]">
          {fastIsDirectional
            ? 'Place order uses the fast leg row.'
            : 'Fast path is not directional — use a live order only if you accept consensus risk.'}
        </span>
        {onPlaceOrder && (
          <div className="flex flex-wrap gap-1.5 justify-end">
            <button
              type="button"
              disabled={!fastIsDirectional}
              title={
                fastIsDirectional
                  ? 'Add stub order from fast scalping leg'
                  : 'Enable when fast trade is BUY_CE or BUY_PE'
              }
              onClick={() => onPlaceOrder(symbol, scalping, { useConsensus: false })}
              className={`text-xs px-2 py-1 rounded border ${
                fastIsDirectional
                  ? 'bg-emerald-500/20 text-emerald-400 border-emerald-500/40 hover:bg-emerald-500/30'
                  : 'bg-slate-800 text-slate-500 border-slate-600 cursor-not-allowed'
              }`}
            >
              Place order (fast)
            </button>
            {aggLeg && (
              <button
                type="button"
                title="Draft using consensus strike / entry / target / SL (not fast-confirmed)"
                onClick={() => onPlaceOrder(symbol, scalping, { useConsensus: true })}
                className="text-xs px-2 py-1 rounded bg-slate-700/80 text-slate-200 border border-slate-600 hover:bg-slate-600/80"
              >
                Draft (consensus)
              </button>
            )}
          </div>
        )}
      </div>
      <div className="mt-3 pt-3 border-t border-slate-700/50">
        <div className="text-[11px] uppercase tracking-wide text-slate-500 mb-1">Hero-zero</div>
        <HeroZeroList heroZero={heroZero} />
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
          <span>{typeof h.strike === 'number' ? `${h.strike} ${h.type || ''}` : h.strike}</span>
          <span className="text-slate-500"> — Entry: </span>
          <span className="text-slate-200">{h.entry ?? '–'}</span>
          <span className="text-slate-500"> | Target: </span>
          <span className="text-emerald-400">{h.target ?? '–'}</span>
          <span className="text-slate-500"> | SL: </span>
          <span className="text-red-400">{h.stoploss ?? '–'}</span>
          <span className="text-slate-500"> | Prob: </span>
          <span className="text-slate-300">{h.probability != null ? Math.round(h.probability) : '–'}%</span>
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
    <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
      {entries.map(([symbol, s]) => {
        const agg = getAggregateSignal(null, s, null)
        const rawT = s?.trade ?? s?.signal
        const primary = resolvePrimaryTradeLabel({
          aggregateSignal: agg,
          entryDecision: s?.entry_decision,
          rawTrade: rawT,
        })
        const decLabel = formatDecisionRowLabel(s?.entry_decision, rawT)
        return (
        <div key={symbol} className="rounded border border-slate-700 bg-slate-800/40 p-3 font-mono text-sm">
          <div className="flex items-center justify-between mb-2">
            <span className="text-slate-400">{symbol}</span>
            <span className="text-white font-bold">{s?.price != null ? Number(s.price).toLocaleString() : '–'}</span>
          </div>
          <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs">
            <span className="text-slate-500">Trade</span><span className={primaryTradeHeaderClass(primary)}>{primary}</span>
            <span className="text-slate-500">Decision</span><span className={decisionRowClass(decLabel)}>{decLabel}</span>
            <span className="text-slate-500">Reason</span><span className="text-slate-200" title={s?.decision_reason}>{s?.decision_reason ?? '–'}</span>
            <span className="text-slate-500">Stable</span><span className="text-slate-200">{s?.stable_count ?? '–'}</span>
            <span className="text-slate-500">Strike</span><span className="text-slate-200">{s?.strike ?? '–'}</span>
            <span className="text-slate-500">Buy / Entry</span><span className="text-white font-medium">{s?.entry != null ? Number(s.entry).toFixed(2) : '–'}</span>
            <span className="text-slate-500">Target</span><span className="text-emerald-400">{s?.target != null ? Number(s.target).toFixed(2) : '–'}</span>
            <span className="text-slate-500">Stoploss</span><span className="text-red-400">{s?.stoploss != null ? Number(s.stoploss).toFixed(2) : '–'}</span>
            <span className="text-slate-500">Lock</span><span className="text-amber-400">{s?.lock_remaining_sec != null ? `${s.lock_remaining_sec}s` : '–'}</span>
            <span className="text-slate-500">Confidence</span><span className="text-slate-200">{s?.confidence != null ? `${s.confidence}%` : '–'}</span>
            <span className="text-slate-500">Regime</span><span className="text-slate-200">{s?.regime ?? '–'}</span>
            <span className="text-slate-500">Gamma wall</span><span className="text-slate-200">{s?.gamma_wall ?? '–'}</span>
            <span className="text-slate-500">Max pain</span><span className="text-slate-200">{s?.max_pain ?? '–'}</span>
            <span className="text-slate-500">Flow</span><span className="text-slate-200">{s?.institutional_flow ?? '–'}</span>
          </div>
        </div>
        )
      })}
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
            <div className="text-slate-200 font-semibold">
              {o.symbol} {o.trade} {o.strike}
              {o.source === 'consensus' && (
                <span className="ml-2 text-[10px] font-normal text-amber-400/90">(consensus draft)</span>
              )}
            </div>
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

function lastTradeResultClass(result) {
  const r = String(result || '').toUpperCase()
  if (r === 'FULL_TARGET' || r === 'TARGET') return 'text-emerald-400'
  if (r === 'TRAIL_EXIT') return 'text-cyan-400'
  if (r === 'LOSS') return 'text-red-400'
  if (r === 'REVERSAL') return 'text-amber-400'
  return 'text-slate-300'
}

/** CE/PE from API or parsed from strike/leg text (older payloads). */
function inferLastTradeSide(lt) {
  if (!lt) return null
  const t = String(lt.type || lt.side || '').toUpperCase()
  if (t === 'CE' || t === 'PE') return t
  const s = `${lt.strike || ''} ${lt.leg || ''}`
  if (/\bCE\b/i.test(s) || / CE$/i.test(String(lt.strike || '').trim())) return 'CE'
  if (/\bPE\b/i.test(s) || / PE$/i.test(String(lt.strike || '').trim())) return 'PE'
  return null
}

function PaperTradesPanel({ paperTrades, onResetPaper }) {
  const pt = paperTrades || {}
  const stats = pt.stats || {}
  const active = pt.active || {}
  const lastTrade = pt.last_trade
  const lastClosedSide = lastTrade ? inferLastTradeSide(lastTrade) : null
  const guide = pt.guide || {}
  const activeEntries = Object.entries(active)
  const totalPnl = stats.total_pnl != null ? Number(stats.total_pnl) : 0
  const closedCount = stats.total_trades ?? 0
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="text-xs text-slate-500">Log is cleared on reset; counters go to zero until new closes.</span>
        {onResetPaper && (
          <button
            type="button"
            onClick={onResetPaper}
            className="text-xs px-3 py-1.5 rounded border border-amber-500/50 text-amber-300 bg-amber-500/10 hover:bg-amber-500/20 font-mono"
          >
            Reset paper trades
          </button>
        )}
      </div>
      <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-2 text-xs font-mono">
        <div className="rounded border border-slate-700 p-2"><div className="text-slate-500">Closed trades</div><div className="text-slate-200">{stats.total_trades ?? 0}</div></div>
        <div className="rounded border border-slate-700 p-2"><div className="text-slate-500">Wins</div><div className="text-emerald-400">{stats.wins ?? 0}</div></div>
        <div className="rounded border border-slate-700 p-2"><div className="text-slate-500">Losses</div><div className="text-red-400">{stats.losses ?? 0}</div></div>
        <div className="rounded border border-slate-700 p-2 lg:col-span-1">
          <div className="text-slate-500">Total PnL (prem pts)</div>
          <div className={totalPnl >= 0 ? 'text-emerald-400 font-semibold' : 'text-red-400 font-semibold'}>
            {stats.total_pnl != null ? Number(stats.total_pnl).toFixed(2) : '0.00'}
          </div>
        </div>
        <div className="rounded border border-slate-700 p-2"><div className="text-slate-500">Win rate</div><div className="text-cyan-300">{stats.win_rate != null ? `${Number(stats.win_rate).toFixed(1)}%` : '0.0%'}</div></div>
        <div className="rounded border border-slate-700 p-2"><div className="text-slate-500">Health</div><div className="text-amber-300">{stats.system_health || 'IDLE'}</div></div>
      </div>
      <div>
        <h3 className="text-xs font-semibold text-slate-400 uppercase tracking-wider mb-2">Active trades</h3>
        {activeEntries.length > 0 ? (
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            {activeEntries.map(([sym, a]) => (
              <div key={sym} className="rounded border border-cyan-500/25 bg-slate-950/40 p-3 font-mono text-xs">
                <div className="text-slate-300 mb-2 font-semibold text-sm">
                  {sym}{' '}
                  <span className="text-cyan-400">{a.strike_label || a.strike || '–'}</span>{' '}
                  <span className="text-slate-500 font-normal">({a.signal || '–'})</span>
                </div>
                <div className="grid grid-cols-2 gap-y-1">
                  <span className="text-slate-500">Index spot</span>
                  <span className="text-amber-200">{a.index_price != null ? Number(a.index_price).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : '–'}</span>
                  <span className="text-slate-500">Live prem (LTP)</span>
                  <span className="text-emerald-300">{a.last_prem != null ? Number(a.last_prem).toFixed(2) : '–'}</span>
                  <span className="text-slate-500">Entry</span><span className="text-slate-200">{a.entry ?? '–'}</span>
                  <span className="text-slate-500">Target</span><span className="text-emerald-400">{a.target ?? '–'}</span>
                  <span className="text-slate-500">SL</span><span className="text-red-400">{a.stoploss ?? '–'}</span>
                  <span className="text-slate-500">PnL live</span>
                  <span className={Number(a.pnl_live) >= 0 ? 'text-emerald-400' : 'text-red-400'}>{a.pnl_live != null ? Number(a.pnl_live).toFixed(2) : '–'}</span>
                  <span className="text-slate-500">PnL %</span><span className="text-slate-200">{a.pnl_percent != null ? `${Number(a.pnl_percent).toFixed(2)}%` : '–'}</span>
                  <span className="text-slate-500">Dist target</span><span className="text-slate-200">{a.distance_to_target != null ? Number(a.distance_to_target).toFixed(2) : '–'}</span>
                  <span className="text-slate-500">Dist SL</span><span className="text-slate-200">{a.distance_to_sl != null ? Number(a.distance_to_sl).toFixed(2) : '–'}</span>
                </div>
              </div>
            ))}
          </div>
        ) : (
          <div className="text-slate-500 text-sm">No active paper trades</div>
        )}
      </div>
      <div className="rounded-lg border-2 border-violet-500/40 bg-violet-500/5 p-4">
        <h3 className="text-xs font-semibold text-violet-300 uppercase tracking-wider mb-3">Last closed trade</h3>
        {lastTrade ? (
          <div className="space-y-3">
            <div className="font-mono">
              <div className="text-slate-500 text-xs mb-1">Option leg (strike · CE/PE)</div>
              <div className="text-lg text-white font-semibold tracking-tight">
                {lastTrade.leg
                  || [lastTrade.symbol, lastTrade.strike].filter(Boolean).join(' ')
                  || lastTrade.symbol
                  || '–'}
              </div>
              {lastClosedSide && (
                <div className="mt-2 inline-flex flex-wrap items-center gap-2 rounded-md border border-slate-600 bg-slate-900/80 px-3 py-1.5">
                  <span className="text-slate-500 text-xs uppercase">This trade was</span>
                  <span className={`text-base font-bold ${lastClosedSide === 'CE' ? 'text-cyan-400' : 'text-amber-400'}`}>{lastClosedSide}</span>
                  <span className="text-slate-500 text-xs">({lastClosedSide === 'CE' ? 'Call' : 'Put'} option)</span>
                </div>
              )}
              {!lastTrade.leg && (lastTrade.type || lastTrade.strike) && (
                <div className="text-sm text-slate-400 mt-1">
                  {lastTrade.type && <span className="text-cyan-400 font-medium">{lastTrade.type}</span>}
                  {lastTrade.type && lastTrade.strike ? <span className="text-slate-600 mx-1">·</span> : null}
                  {lastTrade.strike && <span>{lastTrade.strike}</span>}
                </div>
              )}
            </div>
            <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-3 lg:grid-cols-6 gap-3 font-mono text-sm">
              <div>
                <div className="text-slate-500 text-xs mb-0.5">Symbol</div>
                <div className="text-white font-semibold">{lastTrade.symbol ?? '–'}</div>
              </div>
              <div>
                <div className="text-slate-500 text-xs mb-0.5">Side (CE / PE)</div>
                <div
                  className={`text-xl font-bold ${
                    lastClosedSide === 'CE' ? 'text-cyan-400' : lastClosedSide === 'PE' ? 'text-amber-400' : 'text-slate-500'
                  }`}
                >
                  {lastClosedSide || '–'}
                </div>
              </div>
              <div>
                <div className="text-slate-500 text-xs mb-0.5">Result</div>
                <div className={`font-semibold ${lastTradeResultClass(lastTrade.result)}`}>{lastTrade.result ?? '–'}</div>
              </div>
              {lastTrade.exit_reason && (
                <div>
                  <div className="text-slate-500 text-xs mb-0.5">Exit</div>
                  <div className="text-slate-300">{lastTrade.exit_reason}</div>
                </div>
              )}
              <div>
                <div className="text-slate-500 text-xs mb-0.5">Entry (prem)</div>
                <div className="text-slate-200">{lastTrade.entry != null ? Number(lastTrade.entry).toFixed(2) : '–'}</div>
              </div>
              <div>
                <div className="text-slate-500 text-xs mb-0.5">Exit (prem)</div>
                <div className="text-slate-200">{lastTrade.exit != null ? Number(lastTrade.exit).toFixed(2) : '–'}</div>
              </div>
              <div>
                <div className="text-slate-500 text-xs mb-0.5">PnL</div>
                <div className={Number(lastTrade.pnl) >= 0 ? 'text-emerald-400 font-semibold' : 'text-red-400 font-semibold'}>
                  {lastTrade.pnl != null ? Number(lastTrade.pnl).toFixed(2) : '–'}
                </div>
              </div>
            </div>
          </div>
        ) : (
          <p className="text-slate-500 text-sm">
            {closedCount > 0
              ? 'No last closed trade in memory (stats are from log bootstrap). Open a new trade or reset to sync display.'
              : 'No closed paper trades yet — stats will appear after the first exit.'}
          </p>
        )}
      </div>
      {Object.keys(guide).length > 0 && (
        <div className="text-xs text-slate-500 border-t border-slate-700 pt-2">
          {Object.entries(guide).map(([k, v]) => (
            <div key={k}><span className="text-slate-400">{k}:</span> {v}</div>
          ))}
        </div>
      )}
    </div>
  )
}

function getWsDecisionGuide(coverage = {}) {
  const ltp = Number(coverage.ltp_pct ?? 0)
  const oi = Number(coverage.oi_pct ?? 0)
  const volume = Number(coverage.volume_pct ?? 0)
  const oiChange = Number(coverage.oi_change_pct ?? 0)

  const livePriceReliable = ltp >= 90
  const structureReliable = oi >= 80
  const flowWeak = volume < 40
  const changeUseful = oiChange >= 70

  let quality = 'LOW'
  if (livePriceReliable && structureReliable) quality = 'HIGH'
  else if (ltp >= 70 && oi >= 60) quality = 'MEDIUM'

  let impact = 'Analytics trust is limited; avoid aggressive trades.'
  let decision = 'Use small size or wait for better feed quality.'

  if (quality === 'HIGH' && flowWeak && changeUseful) {
    impact = 'Price/OI signals are reliable, but low volume can cause fake moves and slippage.'
    decision = 'Take only high-confidence setups, use tighter risk, and prefer pullback entries.'
  } else if (quality === 'HIGH' && volume >= 40) {
    impact = 'Strong data quality across price, OI and volume supports conviction.'
    decision = 'Normal position sizing is acceptable with standard risk rules.'
  } else if (quality === 'MEDIUM') {
    impact = 'Moderate reliability; some indicators may lag or miss strike-level shifts.'
    decision = 'Reduce size and demand multi-signal confirmation before entry.'
  }

  return { quality, impact, decision }
}

function getOiDecisionGuide(oi = {}) {
  const pcr = oi?.pcr != null ? Number(oi.pcr) : null
  const maxCall = oi?.max_oi_call != null ? Number(oi.max_oi_call) : null
  const maxPut = oi?.max_oi_put != null ? Number(oi.max_oi_put) : null

  let bias = 'NEUTRAL'
  if (pcr != null) {
    if (pcr < 0.8) bias = 'BEARISH'
    else if (pcr > 1.2) bias = 'BULLISH'
  }

  let structure = 'No clear OI wall structure.'
  if (maxCall != null && maxPut != null) {
    if (maxCall > maxPut) {
      structure = `Put wall ${maxPut} below and call wall ${maxCall} above suggest a tradable range.`
    } else if (maxPut > maxCall) {
      structure = `OI is stacked unusually (put wall above call wall); expect unstable/transition regime.`
    } else {
      structure = `Both OI walls near ${maxCall}; expect pinning around this zone.`
    }
  }

  let decision = 'Wait for price-action confirmation before directional entries.'
  if (bias === 'BEARISH') {
    decision = 'Prefer PE setups near resistance/call-wall rejection; avoid aggressive CE chasing.'
  } else if (bias === 'BULLISH') {
    decision = 'Prefer CE setups near support/put-wall holds; avoid fresh PE shorts near support.'
  } else if (maxCall != null && maxPut != null) {
    decision = `Range behavior likely between ${Math.min(maxPut, maxCall)}-${Math.max(maxPut, maxCall)}; scalp edges, book faster.`
  }

  return { bias, structure, decision }
}

function getMlRlDecisionGuide(signal = {}) {
  const mlRaw = String(signal?.ml?.label ?? '').toUpperCase()
  const rlRaw = String(signal?.rl?.action ?? '').toUpperCase()
  const ml = mlRaw || 'NO_TRADE'
  const rl = rlRaw || 'NONE'

  let impact = 'Model guidance is weak; rely more on price structure and risk rules.'
  let decision = 'Avoid blind execution. Wait for multi-signal confirmation.'

  if (ml === 'BUY_PE' && (rl === 'NONE' || rl === 'HOLD' || rl === '-')) {
    impact = 'Directional bearish hint from ML, but RL gives no execution support.'
    decision = 'Treat as watchlist short setup: execute only after regime/OI/price-action confirms.'
  } else if (ml === 'BUY_CE' && (rl === 'NONE' || rl === 'HOLD' || rl === '-')) {
    impact = 'Directional bullish hint from ML, but RL does not confirm timing.'
    decision = 'Prefer pullback CE entries with strict SL; avoid momentum chase without confirmation.'
  } else if (ml === 'NO_TRADE') {
    impact = 'Model expects low edge in current conditions.'
    decision = 'Stay selective; preserve capital and wait for clearer setup.'
  } else if ((ml === 'BUY_PE' && rl.includes('SELL')) || (ml === 'BUY_CE' && rl.includes('BUY'))) {
    impact = 'ML and RL alignment improves conviction and timing quality.'
    decision = 'Normal sizing is acceptable if WS health and risk filters are healthy.'
  }

  return { ml, rl: rl === 'NONE' ? '-' : rl, impact, decision }
}

function SignalsHistoryTable({ rows }) {
  const list = Array.isArray(rows) ? rows : []
  if (list.length === 0) {
    return <div className="text-slate-500 text-sm">No signal history in database yet (persists when signals meet store rules).</div>
  }
  const fmtTime = (ts) => {
    if (!ts) return '–'
    try {
      const d = new Date(ts)
      return Number.isNaN(d.getTime()) ? String(ts) : d.toLocaleString()
    } catch {
      return String(ts)
    }
  }
  return (
    <div className="overflow-auto max-h-96 border border-slate-700 rounded-lg">
      <table className="w-full text-xs font-mono border-collapse">
        <thead className="bg-slate-800/80 sticky top-0">
          <tr>
            <th className="text-left p-2 text-slate-400 font-semibold">Time</th>
            <th className="text-left p-2 text-slate-400 font-semibold">Sym</th>
            <th className="text-left p-2 text-slate-400 font-semibold">Signal</th>
            <th className="text-left p-2 text-slate-400 font-semibold">Decision</th>
            <th className="text-left p-2 text-slate-400 font-semibold">Reason</th>
            <th className="text-right p-2 text-slate-400 font-semibold">Conf</th>
            <th className="text-right p-2 text-slate-400 font-semibold">Price</th>
            <th className="text-left p-2 text-slate-400 font-semibold">Strike</th>
            <th className="text-right p-2 text-slate-400 font-semibold">Entry</th>
            <th className="text-right p-2 text-slate-400 font-semibold">Tgt</th>
            <th className="text-right p-2 text-slate-400 font-semibold">SL</th>
          </tr>
        </thead>
        <tbody>
          {list.map((r, i) => (
            <tr key={`${r.ts}-${i}`} className="border-t border-slate-700/50">
              <td className="p-2 text-slate-400 whitespace-nowrap">{fmtTime(r.ts)}</td>
              <td className="p-2 text-slate-300">{r.symbol ?? '–'}</td>
              <td className={`p-2 ${String(r.signal || '').includes('CE') ? 'text-cyan-400' : String(r.signal || '').includes('PE') ? 'text-amber-400' : 'text-slate-400'}`}>{r.signal ?? '–'}</td>
              <td className={`p-2 ${decisionRowClass(formatDecisionRowLabel(r.entry_decision, r.signal))}`}>{formatDecisionRowLabel(r.entry_decision, r.signal)}</td>
              <td className="p-2 text-slate-500 max-w-[200px] truncate" title={r.decision_reason}>{r.decision_reason ?? '–'}</td>
              <td className="p-2 text-right text-slate-200">{r.confidence != null ? `${Math.round(r.confidence)}%` : '–'}</td>
              <td className="p-2 text-right text-slate-200">{r.price != null ? Number(r.price).toFixed(2) : '–'}</td>
              <td className="p-2 text-slate-300">{r.strike ?? '–'}</td>
              <td className="p-2 text-right text-slate-200">{r.entry != null ? Number(r.entry).toFixed(2) : '–'}</td>
              <td className="p-2 text-right text-emerald-400">{r.target != null ? Number(r.target).toFixed(2) : '–'}</td>
              <td className="p-2 text-right text-red-400">{r.stoploss != null ? Number(r.stoploss).toFixed(2) : '–'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function DashboardGuideTable() {
  const guideRows = [
    {
      feature: 'Scalping signals',
      meaning: 'Primary CE/PE trade call with entry, target, SL, confidence and execution gate.',
      usefulness: 'Fast directional decision support and trade planning.',
      impact: 'High direct impact on entries, exits, and risk control.',
      example: 'Example: BUY_CE @ 118, Target 152, SL 96, Confidence 82% -> look for CE momentum entry.',
    },
    {
      feature: 'Placed orders (paper)',
      meaning: 'Local paper-trade list of orders triggered from the dashboard.',
      usefulness: 'Tracks active ideas and manual monitoring workflow.',
      impact: 'Operational impact on discipline and execution tracking.',
      example: 'Example: If 3 paper trades open together, reduce new entries to avoid overexposure.',
    },
    {
      feature: 'Final signal (quant analytics)',
      meaning: 'Consolidated signal combining multiple analytics into one decision snapshot.',
      usefulness: 'Quick confirmation layer before taking a trade.',
      impact: 'Reduces conflicting decisions and improves consistency.',
      example: 'Example: Final signal = BUY_PE + HOLD removed + stable_count rising -> stronger execution confidence.',
    },
    {
      feature: 'Market regime indicator',
      meaning: 'Market state (trend/range/volatile) with confidence.',
      usefulness: 'Helps choose strategy style and aggressiveness.',
      impact: 'Affects win rate by aligning trades with regime.',
      example: 'Example: TREND_DOWN (78%) -> prefer PE pullback entries over CE mean-reversion trades.',
    },
    {
      feature: 'Option chain heatmap',
      meaning: 'Live CE/PE LTP, OI, volume by strike with key concentrations.',
      usefulness: 'Identifies strike-level positioning and pressure zones.',
      impact: 'Improves strike selection and support/resistance context.',
      example: 'Example: Max PE OI at 23200 and price above it -> 23200 can act as support zone.',
    },
    {
      feature: 'WS health',
      meaning: 'Data coverage quality for LTP/OI/volume stream.',
      usefulness: 'Validates if analytics are based on sufficient live data.',
      impact: 'Poor health lowers trust; good health increases confidence.',
      example: 'Example: LTP 95%, OI 92% = reliable; LTP 35% = avoid aggressive entries.',
    },
    {
      feature: 'OI analysis',
      meaning: 'PCR, max OI strikes, and OI spike summaries.',
      usefulness: 'Shows positioning bias and likely defended levels.',
      impact: 'Guides directional bias and stop placement.',
      example: 'Example: PCR 1.35 with rising PE OI -> bullish bias unless price breaks support.',
    },
    {
      feature: 'Liquidity map',
      meaning: 'Detected liquidity pockets, support/resistance zones, stop clusters.',
      usefulness: 'Highlights likely reaction areas and trap zones.',
      impact: 'Improves entry timing and avoids poor-chase entries.',
      example: 'Example: Resistance cluster near 23300 -> avoid fresh CE longs right below 23300.',
    },
    {
      feature: 'Stop-hunt detection',
      meaning: 'Potential stop sweep and reversal detection.',
      usefulness: 'Warns against entering into fake breakouts.',
      impact: 'Can reduce whipsaw losses and improve timing.',
      example: 'Example: Resistance stop-hunt detected -> wait for confirmation candle before PE short-cover.',
    },
    {
      feature: 'Gamma exposure / Max pain / Expiry bias',
      meaning: 'Dealer positioning and expiry-related magnet levels.',
      usefulness: 'Context for intraday pinning, expansion, and volatility behavior.',
      impact: 'Helps set realistic targets and trade duration.',
      example: 'Example: Spot near max pain into expiry -> expect chop/pinning, use smaller targets.',
    },
    {
      feature: 'Institutional flow',
      meaning: 'Derived flow sentiment and put-call positioning clues.',
      usefulness: 'Adds macro bias confirmation to intraday setup.',
      impact: 'Improves conviction when aligned with other signals.',
      example: 'Example: Bearish flow + bearish regime + PE signal alignment -> higher-probability short setup.',
    },
    {
      feature: 'ML / RL',
      meaning: 'Model-recommended label/action from ML and RL layers.',
      usefulness: 'Secondary decision intelligence signal.',
      impact: 'Useful as confirmation, not standalone execution trigger.',
      example: 'Example: ML=BUY_CE, RL=HOLD -> delay entry until price action confirms direction.',
    },
  ]

  return (
    <section className="mb-6 rounded-lg border border-slate-700 bg-slate-900/50 p-3">
      <h2 className="text-sm font-semibold text-slate-400 uppercase tracking-wider mb-2">Dashboard guide</h2>
      <p className="text-xs text-slate-500 mb-3">
        What each block means, why it is useful, and how it impacts trading decisions.
      </p>
      <div className="overflow-auto max-h-80 border border-slate-700 rounded-lg">
        <table className="w-full text-xs border-collapse">
          <thead className="bg-slate-800/80 sticky top-0">
            <tr>
              <th className="text-left p-2 text-slate-400 font-semibold">Section</th>
              <th className="text-left p-2 text-slate-400 font-semibold">Meaning</th>
              <th className="text-left p-2 text-slate-400 font-semibold">Usefulness</th>
              <th className="text-left p-2 text-slate-400 font-semibold">Impact</th>
              <th className="text-left p-2 text-slate-400 font-semibold">Example</th>
            </tr>
          </thead>
          <tbody>
            {guideRows.map((row) => (
              <tr key={row.feature} className="border-t border-slate-700/50 align-top">
                <td className="p-2 text-slate-200 font-medium">{row.feature}</td>
                <td className="p-2 text-slate-300">{row.meaning}</td>
                <td className="p-2 text-slate-300">{row.usefulness}</td>
                <td className="p-2 text-slate-300">{row.impact}</td>
                <td className="p-2 text-slate-300">{row.example}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  )
}

function ExecutionAlertStrip({ alerts }) {
  const rows = Object.entries(alerts || {}).filter(([, v]) => v && v.status)
  if (rows.length === 0) return null
  return (
    <section className="mb-4 space-y-2">
      <h2 className="text-xs font-semibold text-slate-400 uppercase tracking-wider">Execution engine (entry / exit)</h2>
      {rows.map(([sym, a]) => {
        const st = String(a.status || '').toUpperCase()
        const border =
          st === 'CONFIRMED'
            ? 'border-emerald-500/50 bg-emerald-500/10'
            : st === 'EXIT'
              ? 'border-amber-500/50 bg-amber-500/10'
              : st === 'NO_TRADE'
                ? 'border-slate-500/60 bg-slate-800/40'
                : 'border-slate-600 bg-slate-900/50'
        const stage = a.stage != null && String(a.stage).trim() !== '' ? String(a.stage).toUpperCase() : null
        return (
          <div key={sym} className={`rounded-lg border p-3 font-mono text-sm ${border}`}>
            <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
              <span className="text-slate-500">{sym}</span>
              <span className="font-semibold text-white">{st}</span>
              {stage ? (
                <span className="text-xs uppercase tracking-wide text-violet-300/90 border border-violet-500/40 rounded px-1.5 py-0.5">
                  {stage}
                </span>
              ) : null}
              <span className="text-cyan-300">{a.signal}</span>
              <span className="text-slate-400">{a.strike}</span>
            </div>
            <div className="mt-1 text-xs text-slate-300 flex flex-wrap gap-x-4 gap-y-0.5">
              <span>entry {a.entry ?? '–'}</span>
              <span>ltp {a.ltp ?? '–'}</span>
              <span className="text-emerald-400">tgt {a.target ?? '–'}</span>
              <span className="text-red-400">sl {a.sl ?? '–'}</span>
              <span>trail {a.trailing_sl ?? '–'}</span>
              <span>pnl {a.pnl != null ? a.pnl : '–'}</span>
              <span>conf {a.confidence != null ? `${Math.round(a.confidence)}%` : '–'}</span>
            </div>
            <div className="mt-1 text-[11px] text-slate-500 break-words">{a.reason}</div>
            <div className="mt-0.5 text-[10px] text-slate-600">{a.time}</div>
          </div>
        )
      })}
    </section>
  )
}

export default function App() {
  const [market, setMarket] = useState({})
  const [signals, setSignals] = useState({})
  const [oi, setOi] = useState({})
  const [orders, setOrders] = useState([])
  const [error, setError] = useState(null)
  const [lastUpdate, setLastUpdate] = useState(null)
  const [modelVersion, setModelVersion] = useState(null)
  const [marketRegime, setMarketRegime] = useState({})
  const [liquidityMap, setLiquidityMap] = useState({})
  const [stopHunts, setStopHunts] = useState({})
  const [finalSignal, setFinalSignal] = useState({})
  const [wsHealth, setWsHealth] = useState({})
  const [paperTrades, setPaperTrades] = useState({})
  const [signalHistoryRows, setSignalHistoryRows] = useState([])
  const [enginePlatform, setEnginePlatform] = useState({})
  const [executionAlerts, setExecutionAlerts] = useState({})
  const symbolOrder = ['NIFTY', 'SENSEX']
  const signalEntries = Object.entries(signals || {})
  const hasFinalSignal = Object.keys(finalSignal || {}).length > 0
  const hasMarketRegimeSection =
    Object.keys(marketRegime || {}).length > 0 ||
    signalEntries.some(([, s]) => s?.regime?.regime)
  const hasWsHealthSection = ['NIFTY', 'SENSEX'].some((sym) => {
    const h = wsHealth?.[sym]?.coverage || {}
    return (h.strikes ?? 0) > 0 || (h.legs ?? 0) > 0
  })
  const liquidityEntries =
    Object.entries(liquidityMap || {}).length > 0
      ? Object.entries(liquidityMap || {}).filter(([, lm]) =>
          (lm?.heatmap?.length ?? 0) > 0 ||
          (lm?.support_zones?.length ?? 0) > 0 ||
          (lm?.resistance_zones?.length ?? 0) > 0
        )
      : signalEntries
          .map(([symbol, s]) => [symbol, s?.liquidity_map])
          .filter(([, lm]) =>
            (lm?.heatmap?.length ?? 0) > 0 ||
            (lm?.support_zones?.length ?? 0) > 0 ||
            (lm?.resistance_zones?.length ?? 0) > 0
          )
  const stopHuntEntries =
    Object.entries(stopHunts || {}).length > 0
      ? Object.entries(stopHunts || {}).filter(([, sh]) => Boolean(sh?.detected))
      : signalEntries
          .map(([symbol, s]) => [symbol, s?.stop_hunt])
          .filter(([, sh]) => Boolean(sh?.detected))
  const gexEntries = signalEntries.filter(([, s]) => (s?.gamma_levels?.gamma_levels?.length ?? 0) > 0)
  const maxPainEntries = signalEntries.filter(([, s]) => s?.max_pain != null)
  const expiryEntries = signalEntries.filter(
    ([, s]) => Boolean(s?.expiry_bias) || (s?.expiry_bias_range?.length ?? 0) >= 2
  )
  const institutionalFlowEntries = signalEntries.filter(
    ([, s]) => Boolean(s?.institutional_flow?.description) || s?.institutional_flow?.put_call_ratio != null
  )
  const oiEntries =
    Object.keys(oi || {}).length > 0
      ? Object.entries(oi || {}).filter(
          ([, data]) =>
            data?.pcr != null || data?.max_oi_call != null || data?.max_oi_put != null || (data?.oi_spikes?.length ?? 0) > 0
        )
      : signalEntries
          .map(([symbol, s]) => [symbol, s?.oi_analysis])
          .filter(
            ([, data]) =>
              data?.pcr != null || data?.max_oi_call != null || data?.max_oi_put != null || (data?.oi_spikes?.length ?? 0) > 0
          )
  const gammaExpiryEntries = signalEntries.filter(
    ([, s]) =>
      (s?.gamma?.gamma_walls?.length ?? 0) > 0 ||
      s?.gamma?.gamma_flip != null ||
      Boolean(s?.expiry) ||
      Boolean(s?.liquidity_sweep?.detected)
  )
  const mlRlEntries = signalEntries.filter(([, s]) => Boolean(s?.ml?.label) || Boolean(s?.rl?.action))

  const placeOrder = (symbol, scalping, opts = {}) => {
    const useConsensus = opts.useConsensus === true
    const o = {
      id: `${Date.now()}_${Math.random().toString(16).slice(2)}`,
      symbol,
      trade: useConsensus
        ? scalping?.aggregate_leg_signal || scalping?.trade || '–'
        : scalping?.trade || '–',
      strike: useConsensus ? scalping?.aggregate_leg_strike || '–' : scalping?.strike || '–',
      entry: useConsensus ? scalping?.aggregate_leg_entry : scalping?.entry,
      target: useConsensus ? scalping?.aggregate_leg_target : scalping?.target,
      stoploss: useConsensus ? scalping?.aggregate_leg_stoploss : scalping?.stoploss,
      placedAt: new Date().toLocaleTimeString(),
      source: useConsensus ? 'consensus' : 'fast',
    }
    setOrders((prev) => [o, ...prev].slice(0, 25))
  }

  const exitOrder = (id) => {
    setOrders((prev) => prev.filter((o) => o.id !== id))
  }

  const handleResetPaperTrades = async () => {
    if (
      !window.confirm(
        'Reset all paper trades? This deletes history in logs/paper_trades.jsonl and zeros stats on the server.',
      )
    ) {
      return
    }
    try {
      const data = await resetPaperTrades()
      setPaperTrades(data.paper_trades || {})
      setError(null)
    } catch (e) {
      setError(e.message || 'Paper trades reset failed')
    }
  }

  const POLL_FAST_MS = Number(import.meta.env.VITE_POLL_FAST_MS) || 1000
  const POLL_SLOW_MS = Number(import.meta.env.VITE_POLL_SLOW_MS) || 5000

  const loadFast = async () => {
    try {
      const [marketRes, signalsRes, enginesRes, wsHealthRes, paperRes] = await Promise.all([
        fetchMarket(),
        fetchSignals(),
        fetchEngines().catch(() => ({ engine_platform: {} })),
        fetchWsHealth().catch(() => ({ ws_health: {} })),
        fetchPaperTrades().catch(() => ({ paper_trades: {} })),
      ])
      setMarket(marketRes.market || {})
      setSignals(signalsRes.signals || {})
      setExecutionAlerts(signalsRes.execution_final_signal || {})
      setEnginePlatform(enginesRes.engine_platform || {})
      setWsHealth(wsHealthRes?.ws_health || {})
      setPaperTrades(paperRes?.paper_trades || {})
      setModelVersion(signalsRes.model_version || marketRes.model_version || null)
      setLastUpdate(new Date())
      setError(null)
    } catch (e) {
      setError(e.message || 'Failed to fetch')
    }
  }

  const loadSlow = async () => {
    try {
      const [oiRes, regimeRes, liqRes, stopRes, finalRes, histN, histS] = await Promise.all([
        fetchOi().catch(() => ({})),
        fetchMarketRegime().catch(() => ({ market_regime: {} })),
        fetchLiquidityMap().catch(() => ({ liquidity_map: {} })),
        fetchStopHunts().catch(() => ({ stop_hunts: {} })),
        fetchFinalSignal().catch(() => ({ final_signal: {} })),
        fetchSignalHistory('NIFTY', 100).catch(() => ({ history: [] })),
        fetchSignalHistory('SENSEX', 100).catch(() => ({ history: [] })),
      ])
      setOi(oiRes || {})
      setMarketRegime(regimeRes.market_regime || {})
      setLiquidityMap(liqRes.liquidity_map || {})
      setStopHunts(stopRes.stop_hunts || {})
      setFinalSignal(finalRes.final_signal || {})
      const hn = histN?.history || []
      const hs = histS?.history || []
      const merged = [...hn, ...hs].sort((a, b) => {
        const ta = new Date(a.ts || 0).getTime()
        const tb = new Date(b.ts || 0).getTime()
        return tb - ta
      })
      setSignalHistoryRows(merged.slice(0, 120))
      setError(null)
    } catch (e) {
      setError(e.message || 'Failed to fetch')
    }
  }

  useEffect(() => {
    ;(async () => {
      await Promise.all([loadFast(), loadSlow()])
    })()
    const tFast = setInterval(loadFast, POLL_FAST_MS)
    const tSlow = setInterval(loadSlow, POLL_SLOW_MS)
    return () => {
      clearInterval(tFast)
      clearInterval(tSlow)
    }
  }, [])

  return (
    <div className="min-h-screen bg-slate-950 text-sm">
      <header className="border-b border-slate-800 bg-slate-900/80 backdrop-blur sticky top-0 z-10">
        <div className="w-full max-w-[98vw] mx-auto px-2 py-2 flex items-center justify-between">
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

      <main className="w-full max-w-[98vw] mx-auto px-2 py-3">
        {error && (
          <div className="mb-4 px-4 py-2 rounded-lg bg-red-500/10 border border-red-500/30 text-red-400 text-sm">
            {error} — Is the backend running on port 8001?
          </div>
        )}

        <ExecutionAlertStrip alerts={executionAlerts} />

        <section className="mb-8">
          <h2 className="text-sm font-semibold text-slate-400 uppercase tracking-wider mb-3">Scalping signals</h2>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            {symbolOrder.map((symbol) => {
              const s = signals?.[symbol] || {}
              const scalping = resolveScalpingPayload(s)
              const aggregateSignal = getAggregateSignal(s, finalSignal?.[symbol], enginePlatform?.[symbol])
              const aggregateEngines = getAggregateEnginesMeta(s, enginePlatform?.[symbol])
              return (
                <ScalpingCard
                  key={symbol}
                  symbol={symbol}
                  scalping={scalping}
                  heroZero={s?.hero_zero || []}
                  onPlaceOrder={placeOrder}
                  aggregateSignal={aggregateSignal}
                  aggregateEngines={aggregateEngines}
                />
              )
            })}
          </div>
        </section>

        <EngineSignalsSection enginePlatform={enginePlatform} symbols={symbolOrder} />

        <section className="mb-8">
          <h2 className="text-sm font-semibold text-slate-400 uppercase tracking-wider mb-3">Placed orders (paper)</h2>
          <div className="rounded-lg border border-slate-700 bg-slate-900/50 p-3">
            <PlacedOrders orders={orders} onExit={exitOrder} />
          </div>
        </section>

        <section className="mb-8">
          <h2 className="text-sm font-semibold text-slate-400 uppercase tracking-wider mb-3">Paper trading simulator</h2>
          <p className="text-xs text-slate-500 font-mono mb-2 max-w-4xl">
            Auto-trades when <span className="text-slate-400">aggregate.signal</span> is BUY_CE/BUY_PE,{' '}
            <span className="text-slate-400">aggregate.confidence ≥ 65</span>, and{' '}
            <span className="text-slate-400">risk.passed</span> (not blocked). Entry from aggregate side + chain LTP; exits: target or stop-loss only. Log:{' '}
            <span className="text-slate-400">logs/paper_trades.jsonl</span>
          </p>
          <div className="rounded-lg border border-slate-700 bg-slate-900/50 p-3">
            <PaperTradesPanel paperTrades={paperTrades} onResetPaper={handleResetPaperTrades} />
          </div>
        </section>

        {hasFinalSignal && (
          <section className="mb-8">
            <h2 className="text-sm font-semibold text-slate-400 uppercase tracking-wider mb-3">Final signal (quant analytics)</h2>
            <div className="rounded-lg border border-slate-700 bg-slate-900/50 p-3">
              <FinalSignalCard finalSignal={finalSignal} />
            </div>
          </section>
        )}

        {hasMarketRegimeSection && (
          <section className="mb-8">
            <h2 className="text-sm font-semibold text-slate-400 uppercase tracking-wider mb-3">Market regime indicator</h2>
            <div className="rounded-lg border border-slate-700 bg-slate-900/50 p-3">
              <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
                {Object.entries(marketRegime).length > 0 ? (
                  Object.entries(marketRegime)
                    .filter(([, r]) => r?.regime)
                    .map(([symbol, r]) => (
                      <div key={symbol} className="font-mono">
                        <span className="text-slate-500 text-xs block mb-1">{symbol}</span>
                        <MarketRegimeIndicator regime={r?.regime} confidence={r?.confidence} />
                      </div>
                    ))
                ) : (
                  signalEntries
                    .filter(([, s]) => s?.regime?.regime)
                    .map(([symbol, s]) => (
                      <div key={symbol} className="font-mono">
                        <span className="text-slate-500 text-xs block mb-1">{symbol}</span>
                        <MarketRegimeIndicator regime={s?.regime?.regime} confidence={s?.regime?.confidence} />
                      </div>
                    ))
                )}
              </div>
            </div>
          </section>
        )}

        {(hasWsHealthSection || oiEntries.length > 0) && (
          <section className="grid grid-cols-1 xl:grid-cols-2 gap-3 mb-8">
            {hasWsHealthSection && (
              <div className="rounded-lg border border-slate-700 bg-slate-900/50 p-3">
                <h2 className="text-sm font-semibold text-slate-400 uppercase tracking-wider mb-3">WS health (ltp / oi / volume coverage)</h2>
                <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                  {['NIFTY', 'SENSEX']
                    .filter((sym) => {
                      const h = wsHealth?.[sym]?.coverage || {}
                      return (h.strikes ?? 0) > 0 || (h.legs ?? 0) > 0
                    })
                    .map((sym) => {
                      const h = wsHealth?.[sym]?.coverage || {}
                      const wsGuide = getWsDecisionGuide(h)
                      return (
                        <div key={sym} className="rounded-lg border border-slate-700 bg-slate-900/50 p-3 font-mono text-xs">
                          <div className="text-slate-400 mb-2">{sym} @ {wsHealth?.[sym]?.index_price != null ? Number(wsHealth[sym].index_price).toFixed(2) : '–'}</div>
                          <div className="grid grid-cols-2 gap-y-1">
                            <span className="text-slate-500">Strikes</span><span className="text-slate-200">{h.strikes ?? 0}</span>
                            <span className="text-slate-500">Legs</span><span className="text-slate-200">{h.legs ?? 0}</span>
                            <span className="text-slate-500">LTP</span><span className="text-emerald-400">{h.ltp_pct ?? 0}%</span>
                            <span className="text-slate-500">OI</span><span className="text-cyan-400">{h.oi_pct ?? 0}%</span>
                            <span className="text-slate-500">Volume</span><span className="text-amber-400">{h.volume_pct ?? 0}%</span>
                            <span className="text-slate-500">OI change</span><span className="text-violet-400">{h.oi_change_pct ?? 0}%</span>
                          </div>
                          <div className="mt-2 pt-2 border-t border-slate-700/60 space-y-1">
                            <div className="text-slate-500">
                              Quality: <span className={wsGuide.quality === 'HIGH' ? 'text-emerald-400' : wsGuide.quality === 'MEDIUM' ? 'text-amber-300' : 'text-red-400'}>{wsGuide.quality}</span>
                            </div>
                            <div className="text-slate-400">
                              Impact: <span className="text-slate-300">{wsGuide.impact}</span>
                            </div>
                            <div className="text-slate-400">
                              Decision: <span className="text-cyan-300">{wsGuide.decision}</span>
                            </div>
                          </div>
                        </div>
                      )
                    })}
                </div>
              </div>
            )}
            {oiEntries.length > 0 && (
              <div className="rounded-lg border border-slate-700 bg-slate-900/50 p-3">
                <h2 className="text-sm font-semibold text-slate-400 uppercase tracking-wider mb-3">OI analysis</h2>
                {oiEntries.map(([sym, data]) => {
                  const oiGuide = getOiDecisionGuide(data)
                  return (
                  <div key={sym} className="mb-3">
                    <span className="text-slate-500 text-xs">{sym}: </span>
                    <OiAnalysis oi={data} />
                    <div className="mt-1 pt-1 border-t border-slate-700/60 text-xs space-y-1">
                      <div className="text-slate-400">
                        Bias: <span className={oiGuide.bias === 'BULLISH' ? 'text-emerald-400' : oiGuide.bias === 'BEARISH' ? 'text-red-400' : 'text-amber-300'}>{oiGuide.bias}</span>
                      </div>
                      <div className="text-slate-400">
                        Impact: <span className="text-slate-300">{oiGuide.structure}</span>
                      </div>
                      <div className="text-slate-400">
                        Decision: <span className="text-cyan-300">{oiGuide.decision}</span>
                      </div>
                    </div>
                  </div>
                )})}
              </div>
            )}
          </section>
        )}

        {(liquidityEntries.length > 0 || stopHuntEntries.length > 0) && (
          <section className="grid grid-cols-1 md:grid-cols-2 gap-4 mb-8">
            {liquidityEntries.length > 0 && (
              <div className="rounded-lg border border-slate-700 bg-slate-900/50 p-3">
                <h2 className="text-sm font-semibold text-slate-400 uppercase tracking-wider mb-3">Liquidity map</h2>
                {liquidityEntries.map(([symbol, lm]) => (
                  <div key={symbol} className="mb-3">
                    <span className="text-slate-500 text-xs block mb-1">{symbol}</span>
                    <LiquidityMapPanel liquidityMap={lm} />
                  </div>
                ))}
              </div>
            )}
            {stopHuntEntries.length > 0 && (
              <div className="rounded-lg border border-slate-700 bg-slate-900/50 p-3">
                <h2 className="text-sm font-semibold text-slate-400 uppercase tracking-wider mb-3">Stop-hunt detection</h2>
                {stopHuntEntries.map(([symbol, sh]) => (
                  <div key={symbol} className="mb-3">
                    <span className="text-slate-500 text-xs block mb-1">{symbol}</span>
                    <StopHuntPanel stopHunt={sh} />
                  </div>
                ))}
              </div>
            )}
          </section>
        )}

        {(gexEntries.length > 0 || maxPainEntries.length > 0 || expiryEntries.length > 0) && (
          <section className="grid grid-cols-1 lg:grid-cols-3 gap-4 mb-8">
            {gexEntries.length > 0 && (
              <div className="rounded-lg border border-slate-700 bg-slate-900/50 p-3">
                <h2 className="text-sm font-semibold text-slate-400 uppercase tracking-wider mb-3">Gamma exposure (GEX)</h2>
                {gexEntries.map(([symbol, s]) => (
                  <div key={symbol}>
                    <span className="text-slate-500 text-xs block mb-2">{symbol}</span>
                    <GammaExposureChart gammaLevels={s?.gamma_levels} />
                  </div>
                ))}
              </div>
            )}
            {maxPainEntries.length > 0 && (
              <div className="rounded-lg border border-slate-700 bg-slate-900/50 p-3">
                <h2 className="text-sm font-semibold text-slate-400 uppercase tracking-wider mb-3">Max pain</h2>
                {maxPainEntries.map(([symbol, s]) => (
                  <div key={symbol} className="mb-3">
                    <span className="text-slate-500 text-xs block mb-1">{symbol}</span>
                    <MaxPainIndicator maxPain={s?.max_pain} spot={market[symbol]?.last_price} />
                  </div>
                ))}
              </div>
            )}
            {expiryEntries.length > 0 && (
              <div className="rounded-lg border border-slate-700 bg-slate-900/50 p-3">
                <h2 className="text-sm font-semibold text-slate-400 uppercase tracking-wider mb-3">Expiry bias</h2>
                {expiryEntries.map(([symbol, s]) => (
                  <div key={symbol} className="mb-3">
                    <span className="text-slate-500 text-xs block mb-1">{symbol}</span>
                    <ExpiryBiasIndicator bias={s?.expiry_bias} expectedRange={s?.expiry_bias_range} />
                  </div>
                ))}
              </div>
            )}
          </section>
        )}

        {(institutionalFlowEntries.length > 0 || gammaExpiryEntries.length > 0) && (
          <section className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
            {institutionalFlowEntries.length > 0 && (
              <div className="rounded-lg border border-slate-700 bg-slate-900/50 p-3">
                <h2 className="text-sm font-semibold text-slate-400 uppercase tracking-wider mb-3">Institutional flow</h2>
                {institutionalFlowEntries.map(([symbol, s]) => (
                  <div key={symbol} className="font-mono text-sm text-slate-300 mb-1">
                    {symbol}: {s?.institutional_flow?.description ?? '–'}
                    {s?.institutional_flow?.put_call_ratio != null && (
                      <span className="text-slate-500 ml-1">PCR: {Number(s.institutional_flow.put_call_ratio).toFixed(2)}</span>
                    )}
                  </div>
                ))}
              </div>
            )}
            {gammaExpiryEntries.length > 0 && (
              <div className="rounded-lg border border-slate-700 bg-slate-900/50 p-3">
                <h2 className="text-sm font-semibold text-slate-400 uppercase tracking-wider mb-3">Gamma, Expiry & Liquidity</h2>
                {gammaExpiryEntries.map(([symbol, s]) => (
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
            )}
          </section>
        )}

        {mlRlEntries.length > 0 && (
          <section className="mt-6 rounded-lg border border-slate-700 bg-slate-900/50 p-3">
            <h2 className="text-sm font-semibold text-slate-400 uppercase tracking-wider mb-3">ML / RL</h2>
            <div className="flex flex-wrap gap-6">
              {mlRlEntries.map(([symbol, s]) => {
                const mlGuide = getMlRlDecisionGuide(s)
                return (
                <div key={symbol} className="font-mono text-sm min-w-[320px] max-w-[520px] rounded border border-slate-700/60 bg-slate-900/40 p-2">
                  <div>
                    <span className="text-slate-500">{symbol}:</span>{' '}
                    <span className="text-slate-300">ML {mlGuide.ml}</span>
                    <span className="text-slate-600 mx-1">|</span>
                    <span className="text-slate-300">RL {mlGuide.rl}</span>
                  </div>
                  <div className="mt-1 text-xs text-slate-400">
                    Impact: <span className="text-slate-300">{mlGuide.impact}</span>
                  </div>
                  <div className="text-xs text-slate-400">
                    Decision: <span className="text-cyan-300">{mlGuide.decision}</span>
                  </div>
                </div>
              )})}
            </div>
          </section>
        )}
        <section className="mt-8 mb-6 rounded-lg border border-slate-700 bg-slate-900/50 p-3">
          <h2 className="text-sm font-semibold text-slate-400 uppercase tracking-wider mb-2">Signal history (stored)</h2>
          <p className="text-xs text-slate-500 mb-3">
            Recent persisted fast signals for NIFTY and SENSEX (newest first). Source: <span className="font-mono">/signal-history</span>.
          </p>
          <SignalsHistoryTable rows={signalHistoryRows} />
        </section>
      </main>
    </div>
  )
}
