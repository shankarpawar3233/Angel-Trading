# OSI Logic Handoff (ChatGPT Format)

Purpose: fast external verification of current OSI trading logic with strict checklist blocks.

---

## A) Scope Snapshot

- System: OSI (NIFTY/SENSEX options signal engine)
- Primary files:
  - `osi/services/pipeline.py`
  - `osi/services/signal_manager.py`
  - `osi/engines/intrabar_engine.py`
  - `osi/engines/smart_breakout_engine.py`
  - `osi/engines/consensus_engine.py`
- APIs:
  - `/signals`, `/signals/active`, `/signals/history`, `/signals/rejected`, `/metrics`, `WS /signals/live`

---

## B) End-to-End Flow (Condensed)

1. Angel WS tick -> pipeline queue  
2. Tick path runs `intrabar_engine` (early trigger)  
3. Candle close path runs candle engines + ML  
4. Consensus scoring -> possible smart/intrabar override  
5. Signal manager builds dynamic confidence + controls  
6. Trade created/updated/closed -> JSON storage + WS/dashboard payload

---

## C) Primary Trigger Rules

- `intrabar_engine` and `smart_breakout_engine` are primary in two ways:
  - Higher weights in consensus (`2.1`, `1.9`)
  - Pipeline override if confidence threshold met:
    - smart breakout >= 65
    - intrabar >= 60
- Signal manager adds +10 dynamic confidence if lead engine is primary.

---

## D) Core Formulas (Verify Exactly)

### Consensus

- `contribution = strength * engine_weight`
- CE score: sum CE contributions
- PE score: sum PE contributions
- Signed score: CE contributes `+`, PE contributes `-`
- Confidence:
  - `dominance = abs(ce_score - pe_score)`
  - `confidence = min(100, dominance / total_weight * 100)`
- Conflict penalty if both sides present:
  - `signed *= 0.65`, `ce *= 0.75`, `pe *= 0.75`
- Agreement boost if >=2 strong same-side engines:
  - add/subtract `0.35`

### Dynamic Confidence (Signal Manager)

- Start from consensus confidence
- `+10` if primary lead engine (intrabar/smart_breakout)
- `+ min(12, directional_strength*12)`
- VWAP aligned `+6`, misaligned `-6`
- Regime contextual boosts `+4` (matching cases)
- Option momentum unconfirmed: soft `-8`
- Clamp to `[0,100]`

---

## E) Engine Validation Checklist

## `intrabar_engine.py`
- [ ] Requires `live_candle`, `prev_candle`, valid `vwap`
- [ ] Volatility floor enforced
- [ ] Momentum threshold enforced
- [ ] CE condition: breakout up + momentum up + price > vwap
- [ ] PE condition: breakout down + momentum down + price < vwap
- [ ] Debounce: max 1 per candle bucket
- [ ] Cooldown per symbol
- [ ] Strength bounded ~0.7-0.9, confidence ~60-80

## `smart_breakout_engine.py`
- [ ] Detects breakout_up/down from current vs previous candle
- [ ] Strong body check (`body/range > 0.5`)
- [ ] Wick trap checks (`wick > body*1.5`)
- [ ] Volume spike required
- [ ] Real breakout -> directional CE/PE
- [ ] Fake breakout -> reversal
- [ ] Liquidity sweep -> reversal
- [ ] One signal per candle safety

## `scalping_engine.py`
- [ ] Candle breakout + body ratio + momentum score
- [ ] Strength gate around `>= 0.6`
- [ ] Returns NONE on weak/flat/missing states

## `trend_engine.py`
- [ ] Fast/slow MA delta logic
- [ ] Flat-trend suppression

## `mean_reversion_engine.py`
- [ ] Z-score thresholds around +/-1.2
- [ ] Mean reversion direction mapping

## `option_chain_engine.py`
- [ ] OI imbalance calculation
- [ ] Balanced/no-OI suppression

## `ml_engine.py`
- [ ] Model-loaded check
- [ ] Direction only if class in {BUY_CE, BUY_PE}

---

## F) Signal Manager Checklist

- [ ] Risk hard blocks: daily loss cap, SL streak, pre-09:20 IST
- [ ] Dynamic confidence used (not static binary only)
- [ ] Duplicate active same-side blocked
- [ ] Opposite replacement only if new confidence >= old + 5
- [ ] Cooldown + max active per symbol enforced
- [ ] Flip-flop guard active
- [ ] Option contract selection from chain
- [ ] Option momentum as soft penalty then conditional block
- [ ] SL/Target mapping:
  - [ ] fallback SL = entry * 0.9
  - [ ] target multiplier mapped from confidence (15%-25% band)
- [ ] Lifecycle transitions:
  - [ ] ACTIVE
  - [ ] TARGET_HIT
  - [ ] SL_HIT
  - [ ] CLOSED (time/replace)

---

## G) Storage + Traceability Checklist

- [ ] `storage/signals.json` exists
- [ ] Tracks:
  - [ ] `active_signals`
  - [ ] `history` (rotated max 200)
  - [ ] `rejected_signals` (with reason + notes)
- [ ] Logs include:
  - [ ] SIGNAL CREATED
  - [ ] SIGNAL UPDATED
  - [ ] SIGNAL CLOSED
  - [ ] SIGNAL BLOCKED DETAIL

---

## H) API Validation Checklist

- [ ] `GET /signals` returns cards
- [ ] `GET /signals/active` returns active list
- [ ] `GET /signals/history` returns trade history
- [ ] `GET /signals/rejected` returns blocked decisions
- [ ] `GET /metrics` includes runtime/performance stats
- [ ] `WS /signals/live` includes:
  - [ ] `active_signals`
  - [ ] `history`
  - [ ] `rejected_signals`
  - [ ] `decision_note`

---

## I) Three Quick Scenario Checks

## 1) Real breakout -> accepted
- Setup: high volume + strong body + breakout + momentum support
- Expected:
  - smart breakout emits directional signal
  - dynamic confidence boosted
  - signal created and written to active

## 2) Fake breakout -> blocked/handled
- Setup: breakout with rejection wick + guard conflict (e.g., duplicate active/cooldown)
- Expected:
  - reversal signal candidate generated
  - if controls fail, appears in `rejected_signals` with reason

## 3) Liquidity sweep -> reverse entry
- Setup: downside sweep with strong lower rejection
- Expected:
  - `BUY_CE` from smart breakout (`liquidity_sweep`)
  - accepted if controls pass

---

## J) Reviewer Decision Block

- [ ] Logic is internally consistent end-to-end
- [ ] Primary trigger precedence works as designed
- [ ] Confidence pipeline is dynamic and auditable
- [ ] Rejections are explainable and persisted
- [ ] Lifecycle/PnL flow is deterministic
- [ ] API surface is sufficient for live monitoring and audit

Reviewer notes:

- Strengths:
- Risks:
- Recommended fixes:

