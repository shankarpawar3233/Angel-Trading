# OSI Logic Audit Pack

This document captures the **current implemented logic** of the OSI trading system for external review.
It is generated from the live code paths in `osi/` and reflects the present behavior.

---

## 1) System Overview

### End-to-End Data Flow

1. **WebSocket ingestion**  
   `osi/data/angel_ws.py` receives market ticks (index + option chain).
2. **Tick ingestion into pipeline**  
   Ticks are pushed to `OSIPipeline.tick_queue` (`osi/services/pipeline.py`).
3. **Candle construction**  
   `CandleBuilder` (`osi/services/candle_builder.py`) aggregates 1m/3m/5m candles.
4. **Intrabar path (tick-level)**  
   On every tick, `intrabar_engine` is evaluated first (`_process_intrabar_engine`).
5. **Candle path (close-level)**  
   On candle close, candle-based engines run (scalping, smart breakout, trend, mean reversion, etc.).
6. **Consensus**  
   `ConsensusEngine.combine()` computes directional scores and confidence.
7. **Signal decision + trade lifecycle**  
   `SignalManager.process_consensus()` applies dynamic scoring, controls, contract selection, SL/targets, and state transitions.
8. **Persistence + live output**  
   - JSON signal storage: `storage/signals.json`
   - optional DB snapshots/history
   - websocket payload: `/signals/live`
   - dashboard pull APIs: `/signals`, `/signals/history`, `/signals/rejected`, `/metrics`

### Engine role differences

- **`intrabar_engine`**  
  Tick-level early trigger. Uses live candle bucket + 3s momentum + debounce/cooldown. Primary early-entry source.

- **`smart_breakout_engine`**  
  Candle-based breakout/fake-breakout/liquidity-sweep classifier. Produces directional/reversal signals with explicit reasons.

- **Other engines** (`scalping`, `trend`, `mean_reversion`, `option_chain`, `ml`)  
  Supplemental directional contributors to consensus; they provide strength votes and context.

---

## 2) Engine Logic Details

## `intrabar_engine.py`

- **Purpose**: detect pre-close breakout with momentum and anti-spam controls.
- **Signal conditions**:
  - Requires `live_candle`, `prev_candle`, `vwap > 0`
  - Volatility gate: `(live.high - live.low) >= intrabar_volatility_min_range`
  - Momentum: `abs(ltp_now - ltp_lookback) >= intrabar_momentum_threshold`
  - `BUY_CE`: `index_price > prev_high`, momentum positive, `index_price > vwap`
  - `BUY_PE`: `index_price < prev_low`, momentum negative, `index_price < vwap`
  - Debounce/cooldown:
    - max 1 trigger per candle bucket
    - symbol cooldown `intrabar_cooldown_seconds`
- **Strength formula**:
  - `breakout_score = breakout_dist / live_range`
  - `mom_score = abs(momentum) / min_momentum`
  - `raw = 0.55 + 0.20*breakout_score + 0.15*mom_score`
  - `strength = clamp(raw)`, then bounded to `[0.7, 0.9]`
- **Confidence formula**:
  - `confidence = clamp_to_60_80(55 + strength*30)`
- **Returns NONE when**:
  - missing metadata, low volatility, low momentum, no breakout direction, debounce/cooldown hit.

## `smart_breakout_engine.py`

- **Purpose**: classify real breakout, fake breakout trap, and liquidity sweep reversal.
- **Core conditions**:
  - `breakout_up = current.high > previous.high`
  - `breakout_down = current.low < previous.low`
  - `body_ratio = abs(close-open) / (high-low)`; strong if `> 0.5`
  - `fake_breakout_up = upper_wick > body*1.5`
  - `fake_breakout_down = lower_wick > body*1.5`
  - `volume_spike = current.volume > avg_volume * smart_breakout_volume_spike_ratio`
  - optional intrabar momentum from `intrabar_data`
- **Decision blocks**:
  - **Real breakout up** -> `BUY_CE` (reason `real_breakout`) if breakout_up, not fake, close>vwap, volume_spike, strong_body
  - **Real breakout down** -> `BUY_PE` (reason `real_breakout`) mirrored
  - **Fake breakout up trap** -> `BUY_PE` (reason `fake_breakout`)
  - **Fake breakdown sweep** -> `BUY_CE` (reason `liquidity_sweep`)
- **Strength/confidence**:
  - Real: strength `0.8/0.84`, confidence `68/76` (higher when intrabar momentum strong)
  - Fake/sweep reversal: strength `0.83`, confidence `70`
- **Safety**:
  - min candle range gate
  - volume spike gate
  - one signal per candle id (per symbol/timeframe) in engine state
- **Returns NONE when**:
  - bad inputs, too small range, no volume spike, no valid pattern.

## `scalping_engine.py`

- **Purpose**: breakout scalping on candle-close structure (+ optional intrabar helper mode in same file).
- **Candle-close `evaluate()` conditions**:
  - hard NONE in `SIDEWAYS` regime (engine-level)
  - requires current/prev candle + valid prices
  - body filter: `body_ratio >= 0.55`
  - breakout: close beyond prev high/low
  - momentum from short history
- **Strength formula**:
  - `breakout_score = breakout_dist / candle_range`
  - `raw = 0.55*body_ratio + 0.45*breakout_score + 0.15*(abs(momentum)/volatility)`
  - `strength = clamp(raw)`, requires `>= 0.6`
- **Signal**:
  - breakout up -> `BUY_CE`
  - breakout down -> `BUY_PE`
- **Returns NONE when**:
  - sideways, missing candles, weak body, no breakout, warmup, weak strength.

## `trend_engine.py`

- **Purpose**: MA-based directional trend vote.
- **Conditions**:
  - warmup >= 30 ticks
  - `fast_ma = mean(last 8)`, `slow_ma = mean(last 25)`
  - `delta = fast_ma - slow_ma`
  - flat if `abs(delta) < scale*0.15`, where `scale = abs(slow_ma)*0.002`
- **Strength**:
  - `strength = clamp(abs(delta)/scale)`
- **Signal**:
  - `delta > 0` -> `BUY_CE`
  - `delta < 0` -> `BUY_PE`
- **Returns NONE**:
  - warmup or flat-trend condition.

## `mean_reversion_engine.py`

- **Purpose**: z-score mean reversion.
- **Conditions**:
  - warmup >= 20 ticks
  - `z = (price - mean)/std`
  - `z >= 1.2` -> `BUY_PE`
  - `z <= -1.2` -> `BUY_CE`
- **Strength**:
  - signal cases: `clamp(abs(z)/3)`
  - no-signal case: `clamp(abs(z)/4)`
- **Returns NONE**:
  - inside band (`-1.2 < z < 1.2`) or warmup.

## `option_chain_engine.py`

- **Purpose**: OI imbalance directional vote.
- **Conditions**:
  - aggregate CE/PE OI across chain
  - `imbalance = (put_oi - call_oi) / total_oi`
  - if `abs(imbalance) < 0.02` -> balanced/no signal
- **Strength**:
  - `strength = clamp(abs(imbalance)*3)`
- **Signal**:
  - imbalance > 0 -> `BUY_PE`
  - imbalance < 0 -> `BUY_CE`
- **Returns NONE**:
  - no OI data or near-balanced OI.

## `ml_engine.py`

- **Purpose**: model-probability directional vote.
- **Conditions**:
  - model must be loaded
  - predicts class probabilities over engineered features
- **Strength**:
  - max predicted probability (`clamp(proba_max)`)
- **Signal**:
  - only if predicted class in `{BUY_CE, BUY_PE}`
  - otherwise class forced to `NONE`
- **Returns NONE**:
  - model missing or non-directional predicted label.

---

## 3) Consensus Engine Logic

Source: `osi/engines/consensus_engine.py`

### Weights

- scalping: `1.0`
- smc: `1.0`
- hero_zero: `0.8`
- trend: `1.2`
- mean_reversion: `0.9`
- option_chain: `1.1`
- ml: `0.6`
- intrabar: `2.1`
- smart_breakout: `1.9`

### Regime adjustments (inside consensus only)

- TRENDING: `mean_reversion_engine` weight set to `0.0`
- SIDEWAYS: `scalping_engine` weight multiplied by `0.6`

### Directional score formulas

For each engine output:

- `contribution = row.strength * weight(row.engine)`
- if `BUY_CE`: add to `signed_score`, add to `ce_score`
- if `BUY_PE`: subtract from `signed_score`, add to `pe_score`

Strong agreement boost:

- if >=2 strong CE (`strength >= 0.6`) and 0 strong PE:
  - `signed_score += 0.35`, `ce_score += 0.35`
- mirrored for PE:
  - `signed_score -= 0.35`, `pe_score += 0.35`

Conflict penalty (both bull and bear present):

- `signed_score *= 0.65`
- `ce_score *= 0.75`
- `pe_score *= 0.75`

Confidence:

- `dominance = abs(ce_score - pe_score)`
- `total_weight = sum(effective_weights)`
- `confidence = min(100, dominance / total_weight * 100)`
- ML dampener: if `ml_engine.strength < 0.5`, cap confidence at `70`

Final signal:

- `BUY_CE` if `ce_score > pe_score`
- `BUY_PE` if `pe_score > ce_score`
- tie-break: sign of `signed_score`

---

## 4) Signal Manager Logic

Source: `osi/services/signal_manager.py`

### Signal creation conditions

Flow inside `process_consensus()`:

1. update lifecycle on current tick
2. hard risk gates:
   - daily loss cap
   - max consecutive SL
   - trading start time >= 09:20 IST
3. dynamic confidence build (not binary):
   - start from consensus confidence
   - `+10` if primary trigger engine is `intrabar_engine` or `smart_breakout_engine`
   - `+ min(12, directional_strength * 12)`
   - VWAP alignment: `+6` aligned, `-6` misaligned
   - regime boost:
     - sideways + mean reversion lead: `+4`
     - trending + trend/smart_breakout/intrabar lead: `+4`
   - clamp to `[0,100]`
4. soft/hard accept checks:
   - reject NONE signal
   - reject low dynamic confidence only if:
     - `confidence < min_conf` and
     - not strong_any (`strength > 0.7`) and
     - not primary trigger
5. position control:
   - duplicate active side blocked
   - opposite side:
     - replace old if new confidence >= old + 5
     - otherwise reject
   - cooldown per symbol
   - max active per symbol
   - flip-flop guard (rapid opposite with small confidence gap)
6. option contract selection
7. option momentum filter:
   - if unconfirmed: apply soft `-8 confidence`
   - hard reject only if confidence still weak and not primary trigger
8. create `SignalRecord`, persist active JSON, emit logs

### Cooldown logic

- `cooldown_seconds = 120`
- based on `last_generated_at[symbol]`

### Opposite signal handling

- If opposite signal while active exists:
  - close existing signal if `new_conf >= existing_conf + 5`
  - else reject (`opposite_ignored`)

### Dynamic confidence threshold

- effective minimum:
  - `min_conf = max(30, settings.min_signal_confidence - 8)`

### Entry / SL / Target

- Entry from selected option LTP
- Target multiplier:
  - maps confidence range 40..90 to `1.15..1.25`
- `target_2 = entry * target_mult`
- `target_1 = entry + 0.5*(target_2-entry)`
- SL:
  - preferred index-reference mapping from candle high/low risk transform
  - fallback `entry * 0.9`

### Trade lifecycle

- `ACTIVE` on creation
- Partial at T1:
  - book 50% qty, update realized PnL
  - move SL to entry
- Exit transitions:
  - T2 reached -> `TARGET_HIT`
  - SL breach -> `SL_HIT`
  - market time exit -> `CLOSED`
  - replacement by opposite stronger signal -> `CLOSED`
- On close:
  - remove from active
  - append to history (max 200)
  - update metrics
  - persist JSON

---

## 5) Intrabar vs Candle Flow

- **Intrabar flow**
  - Runs on every incoming tick before candle-close pass.
  - Uses live 1m bucket + previous 1m candle.
  - If valid, constructs single-engine consensus and can create trade immediately.

- **Candle flow**
  - Runs only on closed candles from `CandleBuilder`.
  - Evaluates candle engines + ML (+ recent cached intrabar output for up to 5 seconds).

- **Priority**
  - Intrabar and smart_breakout are primary by design:
    - higher consensus weights
    - explicit override in pipeline:
      - smart breakout override if confidence >= 65
      - intrabar override if confidence >= 60
    - signal manager also boosts primary-trigger confidence.

---

## 6) Filters and Their Types

### Hard filters (blockers)

- Risk blocked (daily loss cap / SL streak)
- Pre-09:20 IST trade-time block
- `consensus.signal == NONE`
- Low dynamic confidence when non-primary and non-strong
- Duplicate active same side
- Opposite ignored (insufficient confidence advantage)
- Cooldown active
- Max active-per-symbol reached
- Flip-flop guard
- Option contract unavailable
- Option momentum unconfirmed **and** still weak confidence for non-primary

### Soft filters / confidence modifiers

- VWAP alignment (`+6`) / misalignment (`-6`)
- Primary trigger boost (`+10`)
- Directional strength boost (`+ up to 12`)
- Regime contextual boost (`+4` in defined matching cases)
- Option momentum soft penalty (`-8` before fallback rejection check)

### Engine-internal filters

- Intrabar: volatility floor, momentum floor, debounce/cooldown
- Smart breakout: min range, volume spike, wick/body structure
- Scalping: body ratio, breakout validity, strength threshold
- Others: warmups and internal signal criteria

---

## 7) Final Signal Decision Tree

1. Tick arrives -> intrabar path runs.
2. If intrabar emits directional signal:
   - build temporary consensus
   - run full signal-manager logic
   - may create trade immediately.
3. Candle closes -> candle engines + ML execute.
4. Consensus computes directional weighted scores and base confidence.
5. Pipeline applies high-confidence overrides from smart_breakout/intrabar.
6. Signal manager recalculates **dynamic confidence** with boosters/penalties.
7. Hard controls checked (risk/time/position/cooldown/flip-flop/contract).
8. Option momentum confirmation path (soft penalty then conditional rejection).
9. If accepted -> create signal record and persist active.
10. Lifecycle updates per tick until close -> move to history + persist.
11. Rejected decisions are stored with reason + notes.

---

## 8) Sample Output Examples

### A) Real breakout -> signal created

```json
{
  "engine": "smart_breakout_engine",
  "signal": "BUY_CE",
  "strength": 0.84,
  "confidence": 76,
  "reason": "real_breakout"
}
```

Possible decision note:

```text
allowed:primary_trigger=smart_breakout_engine|directional_strength=0.840|vwap_aligned|regime_trending_breakout_boost
```

### B) Fake breakout -> rejected (example rejection path)

Engine can emit reversal signal:

```json
{
  "engine": "smart_breakout_engine",
  "signal": "BUY_PE",
  "strength": 0.83,
  "confidence": 70,
  "reason": "fake_breakout"
}
```

But final decision can still be rejected by controls:

```json
{
  "timestamp": "2026-04-21T08:16:00+00:00",
  "symbol": "NIFTY",
  "signal": "BUY_PE",
  "confidence": 63.5,
  "reason": "duplicate_active",
  "notes": ["primary_trigger=smart_breakout_engine", "directional_strength=0.830", "vwap_misaligned"]
}
```

### C) Liquidity sweep -> reverse signal

```json
{
  "engine": "smart_breakout_engine",
  "signal": "BUY_CE",
  "strength": 0.83,
  "confidence": 70,
  "reason": "liquidity_sweep"
}
```

---

## 9) Debug + Metrics + APIs

### Logging highlights

- Accepted:
  - `SIGNAL CREATED ...`
  - `SIGNAL ALLOWED ... notes=...`
  - `SMART BREAKOUT: type=... reason=...`
- Rejected:
  - `SIGNAL BLOCKED ...`
  - `SIGNAL BLOCKED DETAIL ... reason=... notes=...`
- Lifecycle:
  - `SIGNAL UPDATED ...`
  - `SIGNAL CLOSED ...`

### Available APIs

- `GET /signals` (dashboard cards)
- `GET /signals/active`
- `GET /signals/history`
- `GET /signals/rejected`
- `GET /history` (legacy aggregate/fallback)
- `GET /metrics`
- `WS /signals/live`

---

## 10) Important Source Paths

- `osi/services/pipeline.py`
- `osi/services/signal_manager.py`
- `osi/engines/intrabar_engine.py`
- `osi/engines/smart_breakout_engine.py`
- `osi/engines/scalping_engine.py`
- `osi/engines/trend_engine.py`
- `osi/engines/mean_reversion_engine.py`
- `osi/engines/option_chain_engine.py`
- `osi/engines/ml_engine.py`
- `osi/engines/consensus_engine.py`
- `osi/engines/market_regime_engine.py`
- `osi/api/routes.py`

---

## 11) Review Notes

- This audit reflects current implementation, including recent refactor where VWAP/regime/trend are no longer hard global blockers and now act mainly as confidence modifiers.
- Engine-level internal gates still apply inside each engine.
- Rejected signal tracking is persisted and exposed for review (`/signals/rejected`).

