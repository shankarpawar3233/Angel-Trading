# OSI Telegram Notification Handoff

## Scope
This document explains what Telegram notifications currently do in OSI, how they are triggered, configuration requirements, dependencies, and operational limitations.

## Implementation Location
- Notifier service: `osi/services/telegram_notifier.py`
- Created and owned by pipeline: `osi/services/pipeline.py`
- Trigger points in signal lifecycle: `osi/services/signal_manager.py`
- Config flags: `osi/core/config.py`

## What Telegram Notifications Do

### 1) System startup notifications
Triggered when pipeline starts:
- `notify_system_started(active_count=...)`
- `notify_active_snapshot(active_signals)`

Message content includes:
- "OSI SYSTEM STARTED"
- number of active trades loaded after startup
- active trade snapshot (up to 10 rows, then summary line)

Optional screenshot:
- Controlled by `OSI_TELEGRAM_SCREENSHOT_ON_START`
- Screenshot source URL is `OSI_TELEGRAM_DASHBOARD_URL`

### 2) Signal created notifications
Triggered on new executed signal creation:
- `notify_signal_created(signal, lead_engine=...)`

Message includes:
- signal ID, symbol, side
- strategy + lead engine
- confidence
- entry, SL, T2
- option symbol

Optional screenshot:
- Controlled by `OSI_TELEGRAM_SCREENSHOT_ON_SIGNAL_CREATE`

### 3) Signal update notifications
Triggered for key lifecycle milestones:
- `PARTIAL_BOOK_T1`
- `SL_MOVED_TO_ENTRY`

Message includes:
- signal ID
- symbol
- event type
- event price
- current LTP

Optional screenshot:
- Controlled by `OSI_TELEGRAM_SCREENSHOT_ON_SIGNAL_UPDATE`

### 4) Signal closed notifications
Triggered when signal is closed:
- `notify_signal_closed(signal)`

Message includes:
- signal ID, symbol, side
- final status
- exit price
- PnL

Optional screenshot:
- Controlled by `OSI_TELEGRAM_SCREENSHOT_ON_SIGNAL_CLOSE`

## Delivery Model
- Non-blocking by design:
  - Notifier uses an internal queue (`maxsize=500`)
  - Background worker thread sends messages/photos
- On queue overflow:
  - Message/photo request is dropped with warning log
- On send failure:
  - Error is logged, trading loop continues (no trade flow interruption)

## Telegram Transport Details
- Uses Telegram Bot HTTP APIs:
  - `sendMessage`
  - `sendPhoto`
- Supports:
  - single main chat (`OSI_TELEGRAM_CHAT_ID`)
  - multiple subscribers (`OSI_TELEGRAM_SUBSCRIBERS`, comma-separated)
  - thread/topic messaging (`OSI_TELEGRAM_THREAD_ID`)
- Timeout:
  - text send uses `OSI_TELEGRAM_TIMEOUT_SEC`
  - photo send uses timeout + 2s

## Screenshot Path
- Screenshot capture uses Playwright Chromium (`playwright.sync_api`)
- Opens `OSI_TELEGRAM_DASHBOARD_URL` in headless browser
- Captures full-page PNG with configured viewport:
  - `OSI_TELEGRAM_SCREENSHOT_WIDTH`
  - `OSI_TELEGRAM_SCREENSHOT_HEIGHT`

If Playwright/Chromium is unavailable:
- notifier sends text fallback:
  - "Screenshot skipped: playwright/chromium not available"

## Required Configuration
Notifications are enabled only if all are valid:
- `OSI_TELEGRAM_ENABLED=true`
- `OSI_TELEGRAM_BOT_TOKEN=<bot token>`
- `OSI_TELEGRAM_CHAT_ID=<chat id>`

Optional:
- `OSI_TELEGRAM_SUBSCRIBERS`
- `OSI_TELEGRAM_THREAD_ID`
- `OSI_TELEGRAM_TIMEOUT_SEC`
- `OSI_TELEGRAM_DASHBOARD_URL`
- screenshot toggles/size fields

## Operational Guarantees
- No impact on core signal generation/execution path when Telegram fails.
- Fail-open behavior:
  - network errors, Telegram API errors, screenshot errors do not block trading.
- Startup/close/update/create notifications are best-effort.

## Known Limitations
- No delivery acknowledgment tracking persisted in OSI state.
- No retry/backoff logic beyond queue worker processing.
- Queue overflow can drop notifications under burst load.
- Screenshot depends on dashboard availability + browser resources.

## Quick Validation Checklist
1. Start OSI with Telegram env vars set.
2. Confirm startup text appears.
3. Confirm active snapshot appears.
4. Generate one signal:
   - verify "SIGNAL CREATED" message
   - verify screenshot if enabled
5. Trigger update milestone:
   - verify "SIGNAL UPDATE"
6. Close signal:
   - verify "SIGNAL CLOSED" + optional screenshot

## Summary
OSI Telegram notifications currently provide lifecycle visibility (startup, active snapshot, create, update, close) with optional dashboard screenshots, implemented in a non-blocking and trading-safe way.
