from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class OSISettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="OSI_", extra="ignore")

    app_name: str = "Options Signal Intelligence"
    app_host: str = "0.0.0.0"
    app_port: int = 8010

    redis_url: str = "redis://localhost:6379/0"
    postgres_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/osi"

    min_signal_confidence: float = 40.0
    target_multiplier: float = 1.4
    sl_multiplier: float = 0.8
    partial_target_multiplier: float = 1.2
    market_exit_time_ist: str = "15:25"
    daily_loss_cap: float = 3000.0
    max_consecutive_sl: int = 3

    use_sample_data: bool = True
    sample_tick_interval_ms: int = 200

    websocket_client_code: str = ""
    angel_api_key: str = ""
    angel_client_id: str = ""
    angel_password: str = ""
    angel_totp_secret: str = ""
    angel_feed_token: str = ""
    holidays_csv: str = ""
    ws_reconnect_max_backoff_sec: int = 60
    ws_heartbeat_stale_sec: int = 20
    ws_max_tick_age_ms: int = 1200
    option_data_stale_sec: float = 1.0
    scalping_entry_mode: str = "confirmed"  # aggressive | confirmed
    scalping_intrabar_momentum_sec: int = 5
    scalping_intrabar_min_momentum: float = 2.0
    scalping_volume_spike_ratio: float = 1.2
    scalping_retest_tolerance_points: float = 3.0
    intrabar_momentum_seconds: int = 3
    intrabar_momentum_threshold: float = 2.5
    intrabar_confidence_floor: float = 50.0
    intrabar_volatility_min_range: float = 4.0
    intrabar_cooldown_seconds: int = 60
    option_momentum_lookback_seconds: int = 2
    option_momentum_min_delta: float = 0.8
    smart_breakout_min_range: float = 6.0
    smart_breakout_volume_spike_ratio: float = 1.5
    smart_breakout_momentum_threshold: float = 2.0
    min_market_activity_threshold: float = 8.0
    market_activity_lookback_candles: int = 20


settings = OSISettings()

